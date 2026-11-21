"""Blast Radius selector entry point."""
import json
import os
import sys
import time

from . import policy
from .candidates import Analyzer
from .maps import Maps
from .schedule import fill, split

DATA = os.environ.get("BR_DATA", "/data")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.environ.get("BR_SOURCE", "/opt/sqlglot")
MAP = os.environ.get("BR_MAP", os.path.join(APP, "maps", "map.bin"))
NO_EXEC = os.environ.get("BR_NO_EXEC") == "1"
# Scorer CPU seconds per second of the run costs stored in the map; measured at start.
SPEED = 1.0
# The scorer allows about 4.5 s of wall time per commit (3 min for 40, 4.5 min for 60).
WALL_PER_COMMIT = 4.0


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def plan_job(commit, analysis, maps, analyzer, id_of, seconds, cost_of, files):
    """Decide what to run for one commit, and how much of each test."""
    specs = []
    expensive = []
    for bit, number in maps.bits(analysis.strong):
        full = cost_of.get(number, seconds[number])
        cases = None
        estimate = full
        if maps.has_cases(bit):
            need = analyzer.needs(analysis, bit)
            if need is not None:
                entry = maps.cases(bit)
                total = sum(data["n"] for data in entry.values())
                wanted = sum(entry[f]["n"] if v is None else len(v) for f, v in need.items())
                if total and wanted < total:
                    cases = {f: (None if v is None else sorted(v)) for f, v in need.items()}
                    estimate = full * (0.05 + 0.95 * wanted / total)
        if estimate >= policy.NEVER_RUN:
            expensive.append((number, seconds[number]))
            continue
        specs.append([number, id_of[number], max(10.0, full * SPEED * 10), seconds[number],
                      estimate, cases, 0, full])
    if not specs:
        return None
    # Tests of one class that load fixtures in full keep apart from those that do not,
    # when the filtered ones save enough to pay for a second class set-up.
    by_class = {}
    for spec in specs:
        by_class.setdefault(spec[1].rsplit("::", 1)[0], []).append(spec)
    for group in by_class.values():
        filtered = [s for s in group if s[5] is not None]
        if filtered and len(filtered) < len(group):
            if sum(s[7] - s[4] for s in filtered) > 1.5:
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
                f"{sum(len(v) for v in s[5].values() if v is not None)} cases")
            log(f"    {s[7]:.2f}s -> {s[4]:.2f}s {cases} part{s[6]} {s[1].split('::', 1)[1]}")
    prior = policy.DATA_PRIOR if analysis.data_only else None
    if prior is not None and policy.Tracker(
            [(t[0], t[3], t[4]) for t in tests_spec], expensive, SPEED, prior).should_stop():
        return None  # selecting them unrun is cheaper than checking
    return {"key": commit, "root": SOURCE, "files": files, "pickle": analysis.pickle,
            "prior": prior, "tests": tests_spec, "expensive": expensive,
            "speed": SPEED, "budget": 40.0}


def main():
    global SPEED
    started = time.time()
    SPEED = float(os.environ.get("BR_SPEED") or policy.machine_speed())
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    maps = Maps(MAP, tests)
    analyzer = Analyzer(maps, SOURCE, os.path.join(os.path.dirname(MAP), "refs.bin"))
    commit_dir = os.path.join(DATA, "commits")
    names = sorted(f for f in os.listdir(commit_dir) if f.endswith(".patch"))
    everything = [t["number"] for t in tests]
    id_of = {t["number"]: t["id"] for t in tests}
    seconds = {t["number"]: t["seconds"] for t in tests}
    plans = []
    for name in names:
        commit = name[: -len(".patch")]
        try:
            with open(os.path.join(commit_dir, name), encoding="utf-8", errors="replace") as f:
                analysis = analyzer.analyze(f.read())
            if analysis.everything:
                selected = everything
            else:
                selected = maps.numbers(analysis.strong)
        except Exception as error:  # never lose the whole batch to one odd patch
            log(f"{commit}: analysis failed ({error!r}); running everything")
            selected = everything
            analysis = None
        plans.append((commit, selected, analysis))

    # Run candidates against the patched code; keep the ones that fail.
    cost_of = maps.cost
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
        try:
            verdicts = run_guarded(jobs, 2, deadline, preload, log)
            if len(verdicts) <= 1 and len(preload) > 2:
                # Nothing came back: a shared library may not load here. Let each
                # child load what it needs on its own instead.
                log("helper returned nothing; retrying without preloading")
                verdicts = run_guarded(jobs, 2, deadline, preload[:2], log)
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
        if commit in verdicts:
            verdict = verdicts[commit]
            failed = [n for n in selected if verdict.get(n, (None,))[0] == 1]
            ran = [n for n in selected if verdict.get(n, (3,))[0] in (0, 1)]
            unknown = [n for n in selected if n in verdict and verdict[n][0] == 3]
            expensive = [n for n in selected if n not in verdict]
            cpu = sum(v[1] for v in verdict.values())
            keep = set(failed) | set(unknown)
            if not ran or len(failed) / len(ran) >= policy.EXPENSIVE_SHARE:
                keep |= set(expensive)
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


if __name__ == "__main__":
    main()
