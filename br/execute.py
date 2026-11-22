"""Fork one child per job and collect per-test verdicts."""
import json
import os
import select
import signal
import sys
import time

from . import child


def run_guarded(jobs, parallel, deadline, preload, log=lambda *a: None, warm=(), adapt=False):
    """Run the jobs in a helper process and stream each finished job back.

    The helper loads the heavy libraries and forks the test children, so a crash in
    native code or a hang there cannot take this process down: whatever did not come
    back by the deadline is simply left to the unverified selection.
    """
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(r)
            out = os.fdopen(w, "w", buffering=1)
            if os.environ.get("BR_LAZY", "1") == "1":
                # numpy and pandas load only in a child whose test really uses them.
                from . import lazymods

                lazymods.install()
            t_pre = time.process_time()
            paths = {job["state_path"] for job in jobs if job.get("state")}
            for path in paths:
                # Read once here: every comparison child is forked with it in memory.
                try:
                    from . import state

                    child.PINNED[path] = state.load(path)
                except Exception:  # noqa: BLE001 - each child reads it for itself
                    pass
            for name in preload:
                try:
                    __import__(name)
                except Exception:  # noqa: BLE001 - the tests that need it will say so
                    pass
            log(f"LEDGER preload {time.process_time() - t_pre:.3f}")
            if warm and jobs:
                # Load unpatched sqlglot once; a child whose patch touches none of the
                # loaded modules starts from here, the others load their own copy.
                try:
                    root = jobs[0]["root"]
                    os.environ["SKIP_INTEGRATION"] = "0"
                    os.chdir(root)
                    if root not in sys.path:
                        sys.path.insert(0, root)
                    before = time.process_time()
                    for name in warm:
                        __import__(name)
                    took = time.process_time() - before
                    log(f"warm import took {took:.3f}s")
                    if os.environ.get("BR_PRELOAD_TESTS", "0") == "1":
                        # Test files several children need are loaded once here. A child
                        # whose patch touches a loaded module swaps code or starts clean,
                        # so a shared copy is never used with stale sqlglot.
                        before = time.process_time()
                        uses = {}
                        dialect_jobs = 0
                        for job in jobs:
                            if job.get("state"):
                                continue
                            paths = {t[1].split("::", 1)[0] for t in job["tests"]}
                            for path in paths:
                                uses[path] = uses.get(path, 0) + 1
                            dialect_jobs += any(p.startswith("tests/dialects/") for p in paths)
                        # Not the two files that open duckdb and pandas: a process forked
                        # after those are set up spins its helper threads.
                        heavy = ("tests/test_optimizer.py", "tests/test_executor.py")
                        wanted = sorted(p for p, n in uses.items() if n >= 2 and p not in heavy)
                        if dialect_jobs >= 2:
                            wanted.insert(0, "tests/dialects/test_dialect.py")
                        for path in wanted:
                            try:
                                __import__(path[:-3].replace("/", "."))
                            except Exception:  # noqa: BLE001
                                pass
                        log(f"LEDGER test-preload {time.process_time() - before:.3f} {len(wanted)}")
                    if os.environ.get("BR_SWAP", "0") == "1":
                        before = time.process_time()
                        from . import swap

                        swap.build_index(root)
                        log(f"LEDGER swap-index {time.process_time() - before:.3f} {len(swap.INDEX)}")
                except Exception:  # noqa: BLE001
                    pass

            def done(key, verdicts, stats):
                out.write(json.dumps([key, {str(n): v for n, v in verdicts.items()}, stats]) + "\n")

            run_jobs(jobs, parallel=parallel, log=log, on_done=done, adapt=adapt)
            out.flush()
            log(f"LEDGER helper-own {time.process_time():.3f} jobs {len(jobs)}")
        except BaseException:  # noqa: BLE001
            code = 1
        finally:
            os._exit(code)
    os.close(w)
    results = {"_stats": {}}
    buf = b""
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            log("verification stopped at the deadline")
            break
        ready, _, _ = select.select([r], [], [], min(remaining, 1.0))
        if not ready:
            continue
        chunk = os.read(r, 1 << 20)
        if not chunk:
            break
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for line in lines:
            if line:
                key, verdicts, stats = json.loads(line)
                results[key] = {(int(n) if n.isdigit() else n): tuple(v)
                                for n, v in verdicts.items()}
                if stats:
                    results["_stats"][key] = tuple(stats)
    os.close(r)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    return results


def run_jobs(jobs, parallel=2, log=lambda *a: None, on_done=None, adapt=False):
    """jobs: list of job dicts (see child.run) with a "key". Returns key -> {number: (status, cpu)}.

    status: 0 passed, 1 failed, 3 not run. A child that dies or hangs fails the
    test it was on; the tests after it are reported as not run.
    """
    results = {job["key"]: {} for job in jobs}
    stats = results.setdefault("_stats", {})
    pending = list(jobs)[::-1]
    # With `adapt`, the price of a CPU second is learnt from the tests themselves:
    # what whole, passing tests cost here against what they cost where the run costs
    # were measured. The first jobs go by the estimate they were planned with.
    spent = expected = 0.0
    active = {}  # read fd -> state
    while pending or active:
        # `parallel` may be a function of the time: one child at a time while there is
        # plenty of it, two when the deadline nears.
        width = parallel() if callable(parallel) else parallel
        while pending and len(active) < width:
            job = pending.pop()
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                code = 0
                try:
                    os.close(r)
                    for fd in list(active):
                        os.close(fd)
                    (child.run_state if job.get("state") else child.run)(job, w)
                except BaseException:  # noqa: BLE001
                    code = 1
                finally:
                    os._exit(code)
            os.close(w)
            limits = {t[0]: t[2] for t in job["tests"]}
            active[r] = {"job": job, "pid": pid, "buf": b"", "done": False,
                         "order": [t[0] for t in job["tests"]], "limits": limits,
                         "last": time.time(), "start": time.time()}
        ready, _, _ = select.select(list(active), [], [], 0.25)
        now = time.time()
        for fd in list(active):
            st = active[fd]
            res = results[st["job"]["key"]]
            closed = False
            if fd in ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    closed = True
                else:
                    st["buf"] += chunk
                    st["last"] = now
                    *lines, st["buf"] = st["buf"].split(b"\n")
                    for line in lines:
                        if line == b"done":
                            st["done"] = True
                        elif line:
                            number, status, cpu = json.loads(line)
                            if number == "cpu":
                                stats[st["job"]["key"]] = (status, cpu)
                            else:
                                res[number] = (status, cpu)
            if not closed:
                nxt = next((n for n in st["order"] if n not in res), None)
                # Limits are CPU seconds; allow much more wall time on a busy machine.
                allowed = (st["limits"].get(nxt, 5.0) if nxt is not None else 5.0) * 4 + 30.0
                if now - st["last"] > allowed:
                    try:
                        os.kill(st["pid"], signal.SIGKILL)
                    except OSError:
                        pass
                    closed = True
                    log(f"{st['job']['key']}: child killed after stalling")
            if closed:
                os.close(fd)
                try:
                    _, _, usage = os.wait4(st["pid"], 0)
                    prev = stats.get(st["job"]["key"], (0, 0))
                    stats[st["job"]["key"]] = (prev[0], prev[1],
                                               round(usage.ru_utime, 2), round(usage.ru_stime, 2))
                except OSError:
                    pass
                if not st["done"]:
                    first = True
                    for n in st["order"]:
                        if n not in res:
                            res[n] = (1 if first else 3, 0.0)
                            first = False
                del active[fd]
                if adapt:
                    for test in st["job"]["tests"]:
                        verdict = res.get(test[0])
                        if verdict and verdict[0] == 0 and test[5] is None and test[4] >= 0.002:
                            spent += verdict[1]
                            expected += test[4]
                    if expected >= 0.05:
                        prior = st["job"].get("speed", 1.0)
                        learnt = max(0.7, min(6.0, (prior * 0.3 + spent) / (0.3 + expected)))
                        for job in pending:
                            job["speed"] = learnt
                        stats["_speed"] = (round(learnt, 2), round(expected, 2))
                if on_done is not None:
                    key = st["job"]["key"]
                    on_done(key, res, stats.get(key))
    return results
