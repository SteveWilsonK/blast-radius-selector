"""Blast Radius selector entry point."""
import json
import os
import sys
import time

from . import candidates, policy, slicer
from .candidates import Analyzer
from .maps import Maps
from .schedule import fill, split

DATA = os.environ.get("BR_DATA", "/data")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.environ.get("BR_SOURCE", "/opt/sqlglot")
MAP = os.environ.get("BR_MAP", os.path.join(APP, "maps", "map.bin"))
NO_EXEC = os.environ.get("BR_NO_EXEC") == "1"
# Multiplier on the run costs stored in the map (they were measured with the same
# child set-up the selector uses, so none is needed).
COST_SCALE = 1.0
# Tests whose whole body is one loop over the `Dialects` enum imported by their module.
DIALECT_LOOPS = ("tests/test_generator.py::TestGenerator::test_generator_memory",)
BASE_DIALECT = "sqlglot.dialects.dialect:Dialect"
# Loaded once, unpatched, before the test children are forked.
WARM = ("sqlglot", "sqlglot.optimizer", "sqlglot.executor", "sqlglot.lineage", "tests.helpers")
# Test classes that read their fixture files in setUpClass and keep the cases.
CLASS_LOADED = ("tests/test_executor.py::TestExecutor",)
# Scorer CPU seconds per second of the run costs stored in the map; measured at start.
SPEED = 1.0
# The scorer allows about 4.5 s of wall time per commit (3 min for 40, 4.5 min for 60).
WALL_PER_COMMIT = 4.0
RUN_ALL = os.environ.get("BR_RUN_ALL") == "1"   # development: never stop early on cost
STATE = os.environ.get("BR_STATE", "1") == "1"   # compare loaded state for class-level changes
STATE_FILE = os.path.join(os.path.dirname(MAP), "state.bin")
STATE_MIN_TESTS = 25
STATE_MIN_COST = 1.0
SLICE = os.environ.get("BR_SLICE", "0") == "1"  # run tests by statement where the map allows


def log(*args):
    print(*args, file=sys.stderr, flush=True)


RULED_OUT = {}   # commit -> candidates dropped because nothing they read changed


def decide(selected, verdict, forced=(), ruled_out=0):
    """From what was run to what is selected.

    verdict: number -> (status, cpu) for the tests handed to a child; status 0 passed,
    1 failed, 3 not run. Candidates missing from it were never handed over (too costly
    to run here). Returns (keep, failed, ran, unknown, expensive).
    """
    failed = [n for n in selected if verdict.get(n, (None,))[0] == 1]
    ran = [n for n in selected if verdict.get(n, (3,))[0] in (0, 1)]
    unknown = [n for n in selected if n in verdict and verdict[n][0] == 3]
    expensive = [n for n in selected if n not in verdict]
    keep = set(failed) | set(unknown)
    # Tests the state comparison ruled out count as passed, but only alongside tests
    # that really ran: on their own they say nothing about a test that does read
    # what changed.
    if policy.select_expensive(len(ran) + (ruled_out if ran else 0), len(failed)):
        keep |= set(expensive)
    else:
        # A test that reads a patched file is never dropped on the others' evidence.
        keep |= set(expensive) & set(forced)
    return keep, failed, ran, unknown, expensive


def plan_job(commit, analysis, maps, analyzer, id_of, seconds, cost_of, files):
    """Decide what to run for one commit, and how much of each test."""
    specs = []
    expensive = []
    plans = {}
    for bit, number in maps.bits(analysis.strong):
        full = cost_of.get(number, seconds[number]) * COST_SCALE
        cases = None
        estimate = full
        if maps.has_cases(bit):
            need = analyzer.needs(analysis, bit)
            entry = maps.cases(bit)
            if need is not None and "@body" in entry:
                # A test recorded by statement: run the statements that meet the
                # change and whatever those depend on (see slicer).
                body = entry["@body"]
                wanted = need.get("@body")
                if SLICE and wanted is not None:
                    kept = slicer.keep(body["plan"], wanted)
                    share = sum(body["share"][i] for i in kept)
                    rest = max(0.0, 1.0 - sum(body["share"]))
                    if len(kept) < body["n"] and share + rest < 0.8:
                        cases = {"@body": sorted(kept)}
                        estimate = full * max(0.03, min(1.0, rest + share))
                        plans[str(number)] = {k: body["plan"][k] for k in ("head", "spans", "indent")}
                need = None
            if need is not None:
                # Only fixtures the test works through case by case weigh on its run time.
                tagged = [f for f, data in entry.items() if data.get("tagged")]
                total = sum(entry[f]["n"] for f in tagged)
                wanted = sum(entry[f]["n"] if need.get(f) is None else
                             (1 if need[f] == "changed" else len(need[f])) for f in tagged)
                limited = any(v is not None for v in need.values())
                if limited and (not total or wanted < total):
                    cases = {f: (v if v is None or v == "changed" else sorted(v))
                             for f, v in need.items()}
                    estimate = full * (0.05 + 0.95 * (wanted / total if total else 0.0))
                    shares = [entry[f].get("share") for f in tagged]
                    if tagged and all(shares):
                        # Timed per case: the test's own code plus the cases it will run.
                        every = sum(sum(share) for share in shares)
                        chosen = 0.0
                        for f, share in zip(tagged, shares):
                            v = need.get(f)
                            if v is None:
                                chosen += sum(share)
                            elif v == "changed":
                                chosen += max(share) if share else 0.0
                            else:
                                chosen += sum(share[i] for i in v if i < len(share))
                        estimate = full * max(0.03, min(1.0, 1.0 - every + chosen))
        if cases is None and id_of[number] in DIALECT_LOOPS and not (analysis.whole >> bit) & 1 \
                and not (analysis.direct >> bit) & 1 and analysis.specs and all(
                    kind == "dialects" or (kind == "lines" and family)
                    for kind, _, family, _ in analysis.specs):
            # This test repeats the same work for every dialect. Every reason it is a
            # candidate is tied to the dialects built from the patched file, so only
            # their turns of the loop can change.
            wanted = set()
            for kind, what, family, _ in analysis.specs:
                wanted.update(what if kind == "dialects" else family)
            names = sorted({"" if key == BASE_DIALECT else key.split(":")[0].rsplit(".", 1)[1]
                            for key in wanted})
            if names and len(names) < 12:
                cases = {"__dialects__": names}
                estimate = full * (0.05 + len(names) / 34.0)
        if estimate >= policy.NEVER_RUN:
            expensive.append((number, seconds[number]))
            continue
        specs.append([number, id_of[number], (5.0 + full * 25) * SPEED, seconds[number],
                      estimate, cases, 0, full])
    if not specs:
        return None
    # Tests of one class that load fixtures in full keep apart from those that do not,
    # when the filtered ones save enough to pay for a second class set-up.
    by_class = {}
    for spec in specs:
        by_class.setdefault(spec[1].rsplit("::", 1)[0], []).append(spec)
    for name, group in by_class.items():
        filtered = [s for s in group if s[5] is not None]
        if filtered and len(filtered) < len(group) and name in CLASS_LOADED:
            # This class loads its fixtures once for all its tests, so a test that
            # needs every case would make the filtered ones walk them all.
            if sum(s[7] - s[4] for s in filtered) > 1.0:
                for s in group:
                    if s[5] is None:
                        s[6] = 1
            else:
                for s in filtered:
                    s[4], s[5] = s[7], None
    specs.sort(key=lambda s: s[4])
    tests_spec = [tuple(s[:7]) for s in specs]
    if os.environ.get("BR_DEBUG") == commit:
        log(f"  plan: {len(specs)} to run, est {sum(s[4] for s in specs):.2f}s "
            f"(full {sum(s[7] for s in specs):.2f}s), {sum(1 for s in specs if s[5] is not None)} "
            f"filtered, {len(expensive)} never run")
        for s in sorted(specs, key=lambda s: -s[7])[:10]:
            cases = "whole" if s[5] is None else (
                f"{sum(1 if v == 'changed' else len(v) for v in s[5].values() if v is not None)} cases")
            log(f"    {s[7]:.2f}s -> {s[4]:.2f}s {cases} part{s[6]} {s[1].split('::', 1)[1]}")
    # A patched data file nearly always fails the test that reads it. With a single
    # reader, selecting it unrun can be cheaper than checking. With several readers
    # that prior says nothing about which of them fail, so they are always checked.
    prior = policy.DATA_PRIOR if analysis.data_only and len(tests_spec) == 1 and not expensive \
        else None
    if prior is not None and policy.Tracker(
            [(t[0], t[3], t[4]) for t in tests_spec], expensive, SPEED, prior).should_stop():
        return None  # selecting it unrun is cheaper than checking
    return {"key": commit, "root": SOURCE, "files": files, "pickle": analysis.pickle,
            "prior": prior, "tests": tests_spec, "expensive": expensive,
            "forced": maps.numbers(analysis.direct), "plans": plans,
            "speed": SPEED, "budget": 40.0, "run_all": RUN_ALL}


def refine(plans, texts, analyzer, maps, cost_of, seconds, started, count):
    """For commits that change class-level values and leave many candidates: load the
    patched code once, see which loaded values really differ from the pinned commit,
    and keep only the tests recorded reading one of those (see state.py)."""
    from . import state as state_module
    from .execute import run_guarded

    try:
        unstable = set(state_module.load(STATE_FILE).get("unstable_modules") or ())
    except Exception as error:  # noqa: BLE001
        log(f"state file unreadable ({error!r})")
        return
    jobs = []
    where = {}
    for index, (commit, selected, analysis) in enumerate(plans):
        if analysis is None or analysis.everything or not analysis.body_units:
            continue
        files = dict(analysis.py_changed)
        files.update(analysis.data_changed)
        if any(text is None for text in files.values()):
            continue
        if any(candidates.module_of(path) in unstable for path in files if path.endswith(".py")):
            continue
        weight = sum(cost_of.get(n, seconds[n]) for n in selected)
        if len(selected) < STATE_MIN_TESTS and weight < STATE_MIN_COST:
            continue  # few and cheap candidates: running them costs less than looking
        jobs.append({"key": commit, "state": True, "root": SOURCE, "files": files, "tests": [],
                     "state_path": STATE_FILE, "speed": SPEED})
        where[commit] = index
    if not jobs:
        return
    deadline = started + max(20.0, 1.5 * count)
    try:
        results = run_guarded(jobs, 1, deadline, [], log)
    except Exception as error:  # noqa: BLE001
        log(f"state comparison failed ({error!r})")
        return
    for commit, index in where.items():
        found = (results.get(commit) or {}).get("state")
        result = found[0] if found else None
        tags = analyzer.state_tags(result) if isinstance(result, dict) else None
        if tags is None:
            why = (result or {}).get("error") if isinstance(result, dict) else "no answer"
            log(f"{commit}: state comparison not usable ({why or 'untracked names'})")
            if os.environ.get("BR_DEBUG") == commit and isinstance(result, dict):
                log(f"  changed {result.get('changed')}\n  added {result.get('added')}\n"
                    f"  removed {result.get('removed')}")
            continue
        try:
            analysis = analyzer.analyze(texts[commit], state=tags)
        except Exception as error:  # noqa: BLE001
            log(f"{commit}: analysis with state failed ({error!r})")
            continue
        before = len(plans[index][1])
        narrowed = maps.numbers(analysis.strong)
        if len(narrowed) >= before:
            # The older rules already pin it down at least as well: keep them.
            log(f"{commit}: state comparison: {before} -> {len(narrowed)} candidates, not used")
            continue
        plans[index] = (commit, narrowed, analysis)
        # Tests ruled out here are as good as run and passed when judging how widely
        # the change breaks things (see decide).
        RULED_OUT[commit] = before - len(narrowed)
        log(f"{commit}: state comparison: {before} -> {len(narrowed)} candidates")


def main():
    global SPEED
    started = time.time()
    SPEED = float(os.environ.get("BR_SPEED") or policy.machine_speed())
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    maps = Maps(MAP, tests)
    analyzer = Analyzer(maps, SOURCE, os.path.join(os.path.dirname(MAP), "refs.bin"),
                        os.path.join(os.path.dirname(MAP), "cond.bin"))
    commit_dir = os.path.join(DATA, "commits")
    names = sorted(f for f in os.listdir(commit_dir) if f.endswith(".patch"))
    everything = [t["number"] for t in tests]
    id_of = {t["number"]: t["id"] for t in tests}
    seconds = {t["number"]: t["seconds"] for t in tests}
    plans = []
    texts = {}
    for name in names:
        commit = name[: -len(".patch")]
        try:
            with open(os.path.join(commit_dir, name), encoding="utf-8", errors="replace") as f:
                texts[commit] = f.read()
            analysis = analyzer.analyze(texts[commit])
            if analysis.everything:
                selected = everything
            else:
                selected = maps.numbers(analysis.strong)
        except Exception as error:  # never lose the whole batch to one odd patch
            log(f"{commit}: analysis failed ({error!r}); running everything")
            selected = everything
            analysis = None
        plans.append((commit, selected, analysis))

    cost_of = maps.cost
    if STATE and os.path.exists(STATE_FILE) and maps.tracked and not NO_EXEC:
        refine(plans, texts, analyzer, maps, cost_of, seconds, started, len(names))

    # Run candidates against the patched code; keep the ones that fail.
    jobs = []
    for commit, selected, analysis in plans:
        if analysis is None or analysis.everything or not selected or NO_EXEC:
            continue
        files = dict(analysis.py_changed)
        files.update(analysis.data_changed)
        if any(text is None for text in files.values()):
            continue  # removed or unreadable files: stay with the blind selection
        try:
            job = plan_job(commit, analysis, maps, analyzer, id_of, seconds, cost_of, files)
        except Exception as error:
            log(f"{commit}: planning failed ({error!r}); selecting unrun")
            job = None
        if job is not None:
            jobs.append(job)
    verdicts = {}
    if jobs:
        from .execute import run_guarded

        preload = ["unittest", "unittest.mock"]
        heavy = ("tests/test_optimizer.py", "tests/test_executor.py")
        if any(t[1].startswith(heavy) for job in jobs for t in job["tests"]):
            preload += ["duckdb", "numpy", "pandas", "pandas.testing"]
        deadline = started + max(30.0, WALL_PER_COMMIT * len(names) - 8.0)
        # The scorer's two cores are two threads of one core: two children at once
        # slow each other down and every CPU second is charged. So one child at a
        # time, and a second only if the wall-clock limit starts to press.
        hurry = started + 0.45 * (deadline - started)

        def width():
            return 1 if time.time() < hurry else 2

        try:
            verdicts = run_guarded(jobs, width, deadline, preload, log, warm=WARM)
            if len(verdicts) <= 1 and len(preload) > 2:
                # Nothing came back: a shared library may not load here. Let each
                # child load what it needs on its own instead.
                log("helper returned nothing; retrying without preloading")
                verdicts = run_guarded(jobs, width, deadline, preload[:2], log)
        except Exception as error:
            log(f"verification failed ({error!r}); selecting unrun")
            verdicts = {}

    out = []
    report = []
    total_cost = 0
    candidate_sets = {commit: selected for commit, selected, analysis in plans
                      if analysis is not None}
    for commit, selected, analysis in plans:
        note = ""
        failed = []
        offered = len(selected)
        if commit in verdicts:
            verdict = verdicts[commit]
            forced = maps.numbers(analysis.direct) if analysis is not None else ()
            keep, failed, ran, unknown, expensive = decide(
                selected, verdict, forced, RULED_OUT.get(commit, 0))
            cpu = sum(v[1] for v in verdict.values())
            total, imports, user, system = (verdicts.get("_stats", {}).get(commit, ()) + (0,) * 4)[:4]
            if os.environ.get("BR_DEBUG") == commit:
                for n in failed:
                    log(f"  FAILED {id_of[n]}")
                for n in sorted(ran, key=lambda n: -verdict[n][1])[:12]:
                    log(f"  {verdict[n][1]:.2f}s {id_of[n]}")
            note = (f"child {total:.2f}s (imports {imports:.2f}; rusage u{user} s{system}) "
                    f"ran {len(ran)} ({cpu:.2f}s cpu), failed {len(failed)}, "
                    f"unrun {len(unknown)}, costly {len(expensive)}")
            selected = sorted(keep)
        selected = sorted(set(selected))
        cost, workers = split(selected, ms)
        # Idle time on the less busy workers is free: spend it on the candidates that
        # were left out, least certain first (never run, then run and passed).
        candidates = candidate_sets.get(commit)
        if workers and candidates:
            chosen = set(selected)
            verdict = verdicts.get(commit, {})
            spare = [n for n in candidates if n not in chosen]
            spare.sort(key=lambda n: (verdict.get(n, (3,))[0] == 0, ms[n], n))
            added = fill(workers, ms, spare)
            if added:
                note += f" +{len(added)} in idle time"
        total_cost += cost
        report.append({"commit": commit, "selected": len(selected), "workers": len(workers),
                       "cost": cost / 1000, "note": note,
                       "notes": analysis.notes if analysis else [],
                       "failed": sorted(failed), "candidates": offered,
                       "passed": sorted(n for n, v in verdicts.get(commit, {}).items()
                                        if v[0] == 0) if os.environ.get("BR_REPORT") else [],
                       "cpu": (verdicts.get("_stats", {}).get(commit) or (0.0,))[0]})
        log(f"{commit}: {len(selected)} tests, {cost / 1000:.2f}s", note,
            " ".join(analysis.notes) if analysis else "")
        out.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    if os.environ.get("BR_REPORT"):
        log("REPORT " + json.dumps(report))
    import resource
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    own = time.process_time()
    cpu = own + kids.ru_utime + kids.ru_stime
    log(f"speed factor {SPEED:.2f}")
    log(f"done in {time.time() - started:.2f}s wall; simulated {total_cost / 1000:.2f}s + "
        f"cpu {cpu:.2f}s (own {own:.2f}, children {kids.ru_utime + kids.ru_stime:.2f}) "
        f"= {total_cost / 1000 + cpu:.2f}")


def emergency():
    """Last resort: a valid answer that cannot miss, if anything above went wrong."""
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    _, workers = split([t["number"] for t in tests], ms)
    commit_dir = os.path.join(DATA, "commits")
    lines = [json.dumps({"commit": name[:-6], "workers": workers}, separators=(",", ":"))
             for name in sorted(os.listdir(commit_dir)) if name.endswith(".patch")]
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001
        log(f"selector failed ({error!r}); answering with every test")
        emergency()
