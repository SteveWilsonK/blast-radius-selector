"""Runs inside a forked child: load the patched sqlglot and run chosen tests."""
import importlib
import json
import os
import signal
import sys
import time
import unittest


ROW_READERS = ("test_execute_tpch", "test_execute_tpcds")
DEBUG_FILE = os.environ.get("BR_FAILURES")  # development only: where to write tracebacks
# CPU seconds allowed for loading a test file plus its class set-up.
PHASE_LIMIT = 10.0
# Development only: report tests as this many times slower, to see how the run-or-select
# rules behave on a slower machine without having one.
SLOW = float(os.environ.get("BR_SLOW") or 1.0)


class TestTimeout(BaseException):
    pass


# CPU seconds a single test may take; one that takes longer is stopped and taken as
# passing (0 = no cap).
CAP = float(os.environ.get("BR_CAP", "0"))
_capped = [False]
# After a commit's first failing test, candidates in other test classes ("class") or
# other test files ("file") are not run and are taken as passing ("" = off).
DROP_REST = os.environ.get("BR_DROP_REST", "")


class OutOfBudget(BaseException):
    pass


class NeedState(BaseException):
    """A probe run met a failure: the caller compares state before going on."""


class SkipRest(BaseException):
    """The remaining candidates are costly and nothing cheaper failed."""


class _Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.failfast = True      # a failing subtest ends its test: the verdict is known
        self.bad = set()          # ids of tests that failed
        self.broken_classes = set()

    def stop(self):               # one failing test must not stop the others
        pass

    def _mark(self, test):
        if isinstance(test, unittest.TestCase) and hasattr(test, "_testMethodName"):
            self.bad.add(test.id())
        else:
            # setUpClass / setUpModule failure: unittest reports "setUpClass (mod.Class)"
            self.broken_classes.add(str(test.id() if hasattr(test, "id") else test))

    def addError(self, test, err):
        if _capped[0] and err and err[0] is TestTimeout:
            _timeouts[0] -= 1     # stopped at the cap, not hung: counts as a pass
            return
        self._mark(test)
        self._note(test, err)

    def addFailure(self, test, err):
        self._mark(test)
        self._note(test, err)

    def _note(self, test, err):
        if DEBUG_FILE and err:
            import traceback

            with open(DEBUG_FILE, "a") as handle:
                handle.write(f"== {test}\n" + "".join(traceback.format_exception(*err))[-1800:] + "\n")

    def addUnexpectedSuccess(self, test):
        self._mark(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test)
            self._note(subtest, err)


_timeouts = [0]
# Run cost left (seconds) above which a failed probe hands over to the state comparison.
PROBE_REST = float(os.environ.get("BR_PROBE_REST", "0.3"))


def _alarm(signum, frame):
    _timeouts[0] += 1
    raise TestTimeout()


# The pinned fingerprint, when the process the children are forked from has read it.
PINNED = {}


def run_state(job, out_fd):
    """Load every sqlglot module with the patch applied and report which loaded
    names differ from the pinned commit (see state.py). Writes one line:
    ["state", {changed, added, removed, truncated} or {error}, cpu]."""
    out = os.fdopen(out_fd, "w", buffering=1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    started = time.process_time()
    try:
        root = job["root"]
        os.chdir(root)
        if root not in sys.path:
            sys.path.insert(0, root)
        from . import overlay, state

        for name in list(sys.modules):
            if name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests.")):
                del sys.modules[name]
        overlay.install(root, job["files"])
        signal.signal(signal.SIGPROF, _alarm)
        signal.setitimer(signal.ITIMER_PROF, 8.0 * job.get("speed", 1.0))
        sys.setrecursionlimit(20000)
        pinned = PINNED.get(job["state_path"]) or state.load(job["state_path"])
        state.load_everything()
        table, cut = state.fingerprint()
        signal.setitimer(signal.ITIMER_PROF, 0)
        skip = set(pinned.get("unstable") or ())
        changed, added, removed = state.compare(
            pinned["table"], {k: v for k, v in table.items() if k not in skip})
        result = {"changed": changed, "added": added, "removed": removed,
                  "truncated": bool(cut or pinned.get("truncated"))}
    except BaseException as error:  # noqa: BLE001 - the patched code may not even load
        result = {"error": repr(error)[:200]}
    cpu = round(time.process_time() - started, 3)
    out.write(json.dumps(["state", result, cpu]) + "\n")
    out.write(json.dumps(["cpu", cpu, 0.0]) + "\n")
    out.write("done\n")
    out.flush()


def run(job, out_fd):
    """job: dict(root, files, pickle, expensive=[(number, recorded)], speed, budget,
    tests=[(number, id, limit, recorded, run_cost, cases, part)]).

    cases is None to run a test in full, else {fixture: [case indexes]}: the fixture
    loaders then yield only those cases. part separates tests of one class that must
    not share class-level fixtures (filtered and unfiltered loads).

    Writes one JSON line per test: [number, failed(0/1/2=timeout), cpu_seconds].
    """
    out = os.fdopen(out_fd, "w", buffering=1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    root = job["root"]
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ["SKIP_INTEGRATION"] = "0"
    from . import inline_pool, overlay

    if os.environ.get("BR_GC", "1") == "1":
        import gc

        # sqlglot builds many small objects; the default collector settings spend a
        # visible share of the run scanning them. Tests that need a collection ask
        # for it themselves.
        gc.freeze()
        gc.set_threshold(200000, 50, 100)
    # sqlglot may already be loaded, unpatched, by the parent. That copy is only good
    # if the patch touches none of the loaded modules; otherwise start clean.
    patched = [overlay.module_name(rel)[0] for rel in job["files"] if rel.endswith(".py")]
    if any(name in sys.modules for name in patched) or any(
            rel.startswith("tests/") and rel.endswith(".py") for rel in job["files"]):
        swaps = None
        if job.get("load_lines") is not None and os.environ.get("BR_SWAP", "0") == "1":
            # A change inside function bodies: give the loaded functions their new
            # code instead of loading everything again (see swap.py).
            try:
                from . import swap

                swaps = swap.plan(root, job["files"], overlay.module_name, job["load_lines"])
                if swaps is not None:
                    swap.apply(swaps)
            except BaseException:  # noqa: BLE001
                swaps = None
        if swaps is None:
            for name in list(sys.modules):
                if name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests.")):
                    del sys.modules[name]
        elif os.environ.get("BR_REPORT"):
            out.write(json.dumps(["swap", len(swaps), 0.0]) + "\n")
    inline_pool.install(job["pickle"])
    overlay.install(root, job["files"])
    if job["files"]:
        # Tests that start another Python must meet the same patched files there.
        try:
            spec = f"/tmp/br-overlay-{os.getpid()}.json"
            with open(spec, "w", encoding="utf-8") as handle:
                json.dump({"root": root, "files": job["files"]}, handle)
            hook = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook")
            os.environ["BR_OVERLAY"] = spec
            os.environ["PYTHONPATH"] = os.pathsep.join(
                [hook, root] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p])
        except Exception:  # noqa: BLE001
            pass
    signal.signal(signal.SIGPROF, _alarm)

    active = {"cases": None}
    if any(t[5] is not None for t in job["tests"]):
        try:
            import tests.helpers as helpers

            def wrap(original):
                def loader(filename):
                    cases = active["cases"]
                    if cases is None:
                        yield from original(filename)
                        return
                    # A test cut down by statement (see slicer) loads fixtures in full.
                    allowed = cases.get(filename, None if "@body" in cases else ())
                    if allowed is None:
                        yield from original(filename)
                        return
                    if allowed == "changed":
                        # The fixture itself was patched: only the cases that differ
                        # from the pinned file can behave differently.
                        patched = list(original(filename))
                        with overlay.original():
                            pinned = list(original(filename))
                        if len(patched) != len(pinned):
                            yield from patched
                        else:
                            for new_item, old_item in zip(patched, pinned):
                                if new_item != old_item:
                                    yield new_item
                        return
                    if not allowed:
                        return
                    for index, item in enumerate(original(filename)):
                        if index in allowed:
                            yield item

                return loader

            helpers.load_sql_fixture_pairs = wrap(helpers.load_sql_fixture_pairs)
            helpers.load_sql_fixtures = wrap(helpers.load_sql_fixtures)
        except BaseException:  # noqa: BLE001 - helpers broken by the patch: tests will say so
            pass

    by_class = {}
    order = []
    from .policy import Tracker

    info = {t[0]: (t[3], t[4]) for t in job["tests"]}
    case_filter = {t[0]: (None if t[5] is None else
                          {f: (v if v is None or v == "changed" else frozenset(v))
                           for f, v in t[5].items()})
                   for t in job["tests"]}
    tracker = Tracker([(t[0], t[3], t[4]) for t in job["tests"]], job.get("expensive", ()),
                      job.get("speed", 1.0), job.get("prior"))
    stop_all = False
    stop_code = 3
    probe = bool(job.get("probe"))
    need_state = False
    forced = set(job.get("forced", ()))
    import_cpu = 0.0
    for number, test_id, limit, _, _, _, part in job["tests"]:
        path, cls, method = test_id.split("::")
        key = (path, cls, part)
        if key not in by_class:
            by_class[key] = []
            order.append(key)
        by_class[key].append((number, method, limit))

    budget = time.process_time() + job.get("budget", 1e9)
    fail_scope = set()
    for path, cls_name, part in order:
        items = by_class[(path, cls_name, part)]
        if DROP_REST and fail_scope and not probe and \
                (path if DROP_REST == "file" else (path, cls_name)) not in fail_scope:
            for number, _, _ in items:
                out.write(json.dumps([number, 3 if number in forced else 4, 0.0]) + "\n")
            continue
        if stop_all:
            for number, _, _ in items:
                code = 3 if number in forced else stop_code
                out.write(json.dumps([number, code, 0.0]) + "\n")
            continue
        module_name = path[:-3].replace("/", ".")
        phase_limit = PHASE_LIMIT * job.get("speed", 1.0) if not _timeouts[0] else 2.0
        try:
            t_import = time.process_time()
            # Loading a test file or setting up its class can hang on a patched helper:
            # the CPU alarm covers that too, not only the test bodies.
            signal.setitimer(signal.ITIMER_PROF, phase_limit)
            module = importlib.import_module(module_name)
            cls = getattr(module, cls_name)
            signal.setitimer(signal.ITIMER_PROF, 0)
            import_cpu += time.process_time() - t_import
            if job.get("lazy", True) and path not in job["files"]:
                from . import lazy

                # The row tables are read only by the two tests that run the TPC
                # queries through sqlglot's own executor.
                rows = any(method in ROW_READERS for _, method, _ in items)
                lazy.install(module, cls, rows)
        except BaseException:  # noqa: BLE001 - the file cannot be imported: its tests fail
            signal.setitimer(signal.ITIMER_PROF, 0)
            for number, _, _ in items:
                out.write(json.dumps([number, 1, 0.0]) + "\n")
            fail_scope.add(path if DROP_REST == "file" else (path, cls_name))
            continue
        if path not in job["files"]:
            # Tests that only need some statements of their body get a shortened body.
            lines = None
            for number, method, _ in items:
                wanted = (case_filter[number] or {}).get("@body")
                plan = (job.get("plans") or {}).get(str(number))
                if wanted is None or plan is None:
                    continue
                try:
                    from . import slicer

                    if lines is None:
                        with overlay.original():
                            with open(os.path.join(root, path), encoding="utf-8") as handle:
                                lines = handle.readlines()
                    setattr(cls, method, slicer.build(
                        plan, set(wanted), lines, cls_name, method,
                        os.path.join(root, path), module.__dict__))
                except BaseException:  # noqa: BLE001 - cannot shorten: the test runs whole
                    pass
        # While class fixtures load, allow every case any test of this group needs.
        union = {}
        for number, _, _ in items:
            cases = case_filter[number]
            if cases is None or "@body" in cases:
                # Whole tests, and tests cut down by statement, read fixtures in full.
                union = None
                break
            for fixture, allowed in cases.items():
                have = union.get(fixture, frozenset())
                if allowed is None or have is None:
                    union[fixture] = None
                elif allowed == "changed" or have == "changed":
                    # "changed" only mixes with itself or with nothing.
                    union[fixture] = "changed" if (not have or have == "changed") and (
                        allowed == "changed" or not allowed) else None
                else:
                    union[fixture] = have | allowed
        active["cases"] = union
        result = _Result()
        suite = unittest.TestSuite()
        cases = []
        for number, method, limit in items:
            try:
                case = cls(method)
            except BaseException:  # noqa: BLE001
                out.write(json.dumps([number, 1, 0.0]) + "\n")
                continue
            cases.append((number, case, limit))
        # Drive the suite by hand so each test is timed and reported as it ends, while
        # class and module fixtures still run once (TestSuite.run handles them).
        state = {"i": 0, "t0": 0.0}

        def start(test, _state=state, _cases=cases, module=module):
            if probe and tracker.failed and tracker.todo_cpu > PROBE_REST:
                raise NeedState()
            if not job.get("run_all") and time.process_time() > budget:
                raise OutOfBudget()
            upcoming = _cases[_state["i"]][0]
            # Enough clean runs: the rest is taken as passing, however many are left
            # (before the cost rule, which would select a large rest unrun).
            if not job.get("run_all") and upcoming not in forced and tracker.skip_costly(info[upcoming][1]):
                raise SkipRest()
            if not job.get("run_all") and tracker.should_stop():
                raise OutOfBudget()
            if _timeouts[0] >= 3:
                raise OutOfBudget()  # the patch hangs tests: stop paying for it
            _state["t0"] = time.process_time()
            number = _cases[_state["i"]][0]
            active["cases"] = case_filter[number]
            only = (case_filter[number] or {}).get("__dialects__")
            if only is not None and hasattr(module, "Dialects"):
                # Walk only the dialects the patch can reach (see DIALECT_LOOPS in main).
                _state["dialects"] = module.Dialects
                module.Dialects = [d for d in module.Dialects if d.value in only]
            limit = _cases[_state["i"]][2]
            if _timeouts[0]:
                # One test already ran out of time: give the rest far less rope.
                limit = min(limit, 2.0 + 4.0 * info[number][1])
            _capped[0] = bool(CAP) and CAP < limit
            signal.setitimer(signal.ITIMER_PROF, CAP if _capped[0] else limit)

        def finish(test, _state=state, _cases=cases, _result=result, union=union, module=module):
            _capped[0] = False
            if "dialects" in _state:
                module.Dialects = _state.pop("dialects")
            # Between tests the class fixtures of the next one may run: keep an alarm.
            signal.setitimer(signal.ITIMER_PROF, _state["phase"] if not _timeouts[0] else 2.0)
            active["cases"] = union
            number, case, _ = _cases[_state["i"]]
            _state["i"] += 1
            cpu = (time.process_time() - _state["t0"]) * SLOW
            failed = 1 if case.id() in _result.bad else 0
            out.write(json.dumps([number, failed, round(cpu, 4)]) + "\n")
            _state["reported"] = _state.get("reported", 0) + 1
            recorded, run_cost = info[number]
            tracker.record(recorded, run_cost, failed, cpu)
            if failed:
                fail_scope.add(path if DROP_REST == "file" else (path, cls_name))

        result.startTest = lambda test, _s=start: (unittest.TestResult.startTest(result, test), _s(test))[0]
        result.stopTest = lambda test, _f=finish: (_f(test), unittest.TestResult.stopTest(result, test))[0]
        for _, case, _ in cases:
            suite.addTest(case)
        unrun = 1  # tests never reached (class fixture failed, suite died): they fail
        state["phase"] = phase_limit
        try:
            signal.setitimer(signal.ITIMER_PROF, phase_limit)
            suite.run(result)
        except OutOfBudget:
            unrun = 3  # not run at all: the caller decides
            stop_all = True
        except NeedState:
            unrun = 3
            stop_all = True
            need_state = True
        except SkipRest:
            unrun = 4  # not run, taken as passing
            stop_all = True
            stop_code = 4
        except BaseException:  # noqa: BLE001
            pass
        signal.setitimer(signal.ITIMER_PROF, 0)
        for number, case, _ in cases[state.get("reported", 0):]:
            out.write(json.dumps([number, unrun, 0.0]) + "\n")
    if need_state:
        out.write(json.dumps(["state?", 1, 0.0]) + "\n")
    out.write(json.dumps(["cpu", round(time.process_time(), 3), round(import_cpu, 3)]) + "\n")
    out.write("done\n")
    out.flush()
