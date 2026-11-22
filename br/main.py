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
SPEED_FLOOR = float(os.environ.get("BR_SPEED_FLOOR", "1.0"))
# The scorer allows about 4.5 s of wall time per commit (3 min for 40, 4.5 min for 60).
WALL_PER_COMMIT = float(os.environ.get("BR_WALL", "4.0"))
RUN_ALL = os.environ.get("BR_RUN_ALL") == "1"   # development: never stop early on cost
STATE = os.environ.get("BR_STATE", "1") == "1"   # compare loaded state for class-level changes
STATE_FILE = os.path.join(os.path.dirname(MAP), "state.bin")
# Compare state only after a cheap candidate has failed: when the first few pass, the
# commit is taken as clean without the comparison.
LAZY_STATE = os.environ.get("BR_LAZY_STATE", "1") == "1"
# The helper that runs the first batch of test children stays for the second one.
ONE_HELPER = os.environ.get("BR_ONE_HELPER", "0") == "1"
STATE_MIN_TESTS = 25
STATE_MIN_COST = 1.0
EXP_DROP = set(filter(None, os.environ.get("BR_EXP_DROP", "test_execute_tpcds").split(",")))
SLICE = os.environ.get("BR_SLICE", "0") == "1"  # run tests by statement where the map allows


def log(*args):
    print(*args, file=sys.stderr, flush=True)


RULED_OUT = {}   # commit -> candidates dropped because nothing they read changed


ALIKE = {}       # costly test number -> numbers of the cheap tests most like it
SOFT = set()     # costly tests decided by their look-alikes alone
JOBS = {}        # commit -> the job its candidates last ran as
TRACK_HARD_ONLY = os.environ.get("BR_TRACK_HARD_ONLY", "0") == "1"
TRACK_NO_DROPPED = os.environ.get("BR_TRACK_NO_DROPPED", "0") == "1"
# On: costly tests are planned as before the never-run rule and only left out when run.
SOFT_SKIP = os.environ.get("BR_SOFT_SKIP", "1") == "1"
# With SOFT_SKIP: a slice of one of the two giant tests is not run either.
# 1: such a slice is taken out of the plan; 2: it stays in the plan (so the stop rule
# still counts it) and is only left out by the child, then decided like a never-run giant.
SKIP_GIANT = int(os.environ.get("BR_SKIP_GIANT", "2"))
GIANT_SKIPPED = set()
# Commits with at most this many failing tests get each of them run again alone (0 = off).
RECHECK = int(os.environ.get("BR_RECHECK", "0"))
# On: tests that failed in this batch are run once on the unpatched code; the ones that
# fail there too are never selected. Only tests up to BASELINE_MAX seconds of run cost.
BASELINE = os.environ.get("BR_BASELINE", "0") == "1"
BASELINE_MAX = float(os.environ.get("BR_BASELINE_MAX", "0.3"))
UNRUNNABLE = {}  # commit -> its moderately costly candidates, when none of its candidates ran
# On: such candidates are left out (no look-alike could fail), unless they read a patched file.
SOFT_BLIND = os.environ.get("BR_SOFT_BLIND", "0") == "1"
# Off: a moderately costly test is selected only when it reads a patched file.
SOFT_ALIKE = os.environ.get("BR_SOFT_ALIKE", "1") == "1"
# Off: reading a patched file no longer selects a moderately costly test by itself;
# only a failed look-alike does.
SOFT_FORCE = os.environ.get("BR_SOFT_FORCE", "1") == "1"
# On: a never-run test without a look-alike list is left out too (off: the share rule).
SOFT_ALL = os.environ.get("BR_SOFT_ALL", "0") == "1"


def load_alike(tests):
    """Look-alikes of the tests too costly to run (maps/alike.json, from the pinned commit)."""
    path = os.path.join(os.path.dirname(MAP), "alike.json")
    if not os.path.exists(path):
        return
    number = {t["id"]: t["number"] for t in tests}
    try:
        with open(path) as handle:
            for name, others in json.load(handle).items():
                if name in number:
                    ALIKE[number[name]] = {number[o] for o in others if o in number}
    except Exception as error:  # noqa: BLE001
        log(f"look-alike file unreadable ({error!r})")


CAPPED = {}      # commit -> costly tests given a short CPU allowance there


def decide(selected, verdict, forced=(), ruled_out=0, capped=()):
    """From what was run to what is selected.

    verdict: number -> (status, cpu) for the tests handed to a child; status 0 passed,
    1 failed, 3 not run. Candidates missing from it were never handed over (too costly
    to run here). Returns (keep, failed, ran, unknown, expensive).
    """
    failed = [n for n in selected if verdict.get(n, (None,))[0] == 1]
    ran = [n for n in selected if verdict.get(n, (3,))[0] in (0, 1)]
    unknown = [n for n in selected if n in verdict and verdict[n][0] == 3]
    expensive = [n for n in selected if n not in verdict
                 or (n in GIANT_SKIPPED and verdict[n][0] == 5)]
    # A costly test that was to get a short allowance but was never reached is decided
    # like one that is not run at all: by its look-alikes.
    waiting = [n for n in unknown if n in capped and (n in ALIKE or SOFT_ALL)]
    waiting += [n for n in selected if verdict.get(n, (None,))[0] == 5
                and n not in GIANT_SKIPPED]   # planned, left out by the child
    keep = set(failed) | (set(unknown) - set(waiting))
    # Tests the state comparison ruled out count as passed, but only alongside tests
    # that really ran: on their own they say nothing about a test that does read
    # what changed.
    soft = [n for n in expensive if n in SOFT and (n in ALIKE or SOFT_ALL)]
    hard = [n for n in expensive if n not in soft]
    if policy.select_expensive(len(ran) + (ruled_out if ran else 0), len(failed)):
        keep |= set(hard)
    else:
        # A test that reads a patched file is never dropped on the others' evidence.
        keep |= set(hard) & set(forced)
    # Moderately costly tests: selected when one of the tests most like them failed
    # (or they read a patched file), left out otherwise.
    failed_set = set(failed)
    for n in soft + waiting:
        if (SOFT_FORCE and n in forced) or (SOFT_ALIKE and failed_set & ALIKE.get(n, set())):
            keep.add(n)
    return keep, failed, ran, unknown, hard


def insure(keep, dropped, ms, risk):
    """Costly tests left out on the others' evidence: put back the ones that cost
    less to schedule than the risk of leaving them out. Tries every combination
    (there are at most a handful of such tests)."""
    if not dropped or risk <= 0:
        return keep
    dropped = sorted(dropped, key=lambda n: -ms[n])[:4]
    base = sorted(keep)
    best = None
    for mask in range(1 << len(dropped)):
        extra = [n for i, n in enumerate(dropped) if (mask >> i) & 1]
        cost = split(base + extra, ms)[0] / 1000 + policy.MISS * risk * (len(dropped) - len(extra))
        if best is None or cost < best[0] - 1e-9:
            best = (cost, extra)
    return set(keep) | set(best[1])


def told(expensive, id_of, direct):
    """The never-run tests the child's stop rule should count on being selected."""
    out = expensive
    if TRACK_HARD_ONLY:
        out = [e for e in out if e[0] not in SOFT]
    if TRACK_NO_DROPPED and EXP_DROP:
        # A test this build never selects (unless it reads a patched file) must not make
        # the rest look cheap to select unrun.
        out = [e for e in out if e[0] in direct or id_of[e[0]].rsplit("::", 1)[1] not in EXP_DROP]
    return out


def plan_job(commit, analysis, maps, analyzer, id_of, seconds, cost_of, files):
    """Decide what to run for one commit, and how much of each test."""
    specs = []
    expensive = []
    capped = []
    skipped = []
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
                if policy.CASE_CAP and full < policy.GIANT:
                    # At most CASE_CAP of the cases that meet the change are run, the
                    # cheapest ones; a test that passes them is taken as passing.
                    need = dict(need)
                    for f in tagged:
                        if f not in need or need[f] == "changed":
                            continue
                        wanted_cases = range(entry[f]["n"]) if need[f] is None else need[f]
                        if len(wanted_cases) > policy.CASE_CAP:
                            share = entry[f].get("share") or ()
                            need[f] = set(sorted(wanted_cases, key=lambda i: (
                                share[i] if i < len(share) else 0.0, i))[:policy.CASE_CAP])
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
        if SOFT_SKIP and policy.NEVER_RUN <= estimate < 5.0 and (full < policy.GIANT or SKIP_GIANT == 2):
            # Planned exactly as a test that runs (order, grouping, stop rule), but the
            # child leaves it out; it is then decided like any never-run costly test.
            skipped.append(number)
            if full >= policy.GIANT:
                GIANT_SKIPPED.add(number)
        elif estimate >= (5.0 if SOFT_SKIP and not (SKIP_GIANT == 1 and full >= policy.GIANT)
                          else policy.NEVER_RUN):
            if full < policy.GIANT and policy.SOFT_CAP > 0:
                # A moderately costly test is run for a short while only: a failure in
                # that time selects it, otherwise it is taken as passing.
                capped.append(number)
                estimate = min(estimate, policy.SOFT_CAP / SPEED)
            else:
                expensive.append((number, seconds[number]))
                if full < policy.GIANT:
                    SOFT.add(number)
                continue
        specs.append([number, id_of[number], (5.0 + full * 25) * SPEED, seconds[number],
                      estimate, cases, 0, full])
    if os.environ.get("BR_WHOLE") == "1":
        for spec in specs:      # development: no test is cut down to fixture cases
            spec[4], spec[5] = spec[7], None
    if not specs:
        # Nothing here is cheap enough to run: remember the moderately costly ones.
        UNRUNNABLE[commit] = {number for number, _ in expensive if number in SOFT}
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
    CAPPED[commit] = set(capped)
    # Lines of the patched files that sqlglot runs while it loads: a function that
    # ran then cannot simply be given new code (see swap.py).
    load_lines = {path: sorted(maps.self_import.get(path) or ()) for path in files
                  if path.endswith(".py")}
    return {"key": commit, "root": SOURCE, "files": files, "pickle": analysis.pickle,
            "load_lines": load_lines,
            "prior": prior, "tests": tests_spec, "capped": capped, "skip": skipped,
            # The stop rule in the child counts on costly tests being selected; the
            # moderately costly ones are decided apart, so it is not told about them.
            "expensive": told(expensive, id_of, set(maps.numbers(analysis.direct))),
            "forced": maps.numbers(analysis.direct), "plans": plans,
            "speed": SPEED, "budget": 40.0, "run_all": RUN_ALL}


def state_unstable():
    """Modules whose loaded state differs from run to run, or None if unknown."""
    from . import state as state_module

    try:
        if os.path.exists(STATE_FILE + ".meta"):
            with open(STATE_FILE + ".meta") as handle:
                return set(json.load(handle).get("unstable_modules") or ())
        return set(state_module.load(STATE_FILE).get("unstable_modules") or ())
    except Exception as error:  # noqa: BLE001
        log(f"state file unreadable ({error!r})")
        return None


def state_eligible(selected, analysis, unstable, cost_of, seconds):
    """Is this a commit the state comparison is meant for?"""
    if analysis is None or analysis.everything or not analysis.body_units:
        return False
    files = dict(analysis.py_changed)
    files.update(analysis.data_changed)
    if any(text is None for text in files.values()):
        return False
    if any(candidates.module_of(path) in unstable for path in files if path.endswith(".py")):
        return False
    weight = sum(cost_of.get(n, seconds[n]) for n in selected)
    return not (len(selected) < STATE_MIN_TESTS and weight < STATE_MIN_COST)


def refine(plans, texts, analyzer, maps, cost_of, seconds, started, count, only=None):
    """For commits that change class-level values and leave many candidates: load the
    patched code once, see which loaded values really differ from the pinned commit,
    and keep only the tests recorded reading one of those (see state.py)."""
    from . import state as state_module
    from .execute import run_guarded

    try:
        if os.path.exists(STATE_FILE + ".meta"):
            # Written beside the fingerprint when the image is built: the few names
            # needed here, without reading the whole fingerprint in this process.
            with open(STATE_FILE + ".meta") as handle:
                unstable = set(json.load(handle).get("unstable_modules") or ())
        else:
            unstable = set(state_module.load(STATE_FILE).get("unstable_modules") or ())
    except Exception as error:  # noqa: BLE001
        log(f"state file unreadable ({error!r})")
        return
    jobs = []
    where = {}
    for index, (commit, selected, analysis) in enumerate(plans):
        if only is not None and commit not in only:
            continue
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
        log(f"LEDGER state {commit} {found[1] if found else 0}")
        tags = analyzer.state_tags(result) if isinstance(result, dict) else None
        if tags is None:
            why = (result or {}).get("error") if isinstance(result, dict) else "no answer"
            log(f"{commit}: state comparison not usable ({why or 'untracked names'})")
            if os.environ.get("BR_DEBUG") == commit and isinstance(result, dict):
                log(f"  changed {result.get('changed')}\n  added {result.get('added')}\n"
                    f"  removed {result.get('removed')}")
            continue
        if os.environ.get("BR_TAGS"):
            log("TAGS " + commit + " " + json.dumps(tags))
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
    if os.environ.get("BR_GC_OFF", "0") == "1":
        # A short-lived process tree: collecting cycles only costs CPU here. The
        # helper and the test children inherit the setting.
        import gc

        gc.disable()
    # Scorer CPU seconds per second of the run costs in the map. The short probe reads
    # about 1.8 on the scorer, but the scorer's own numbers show tests there cost more
    # than that (see NOTES.md): never price CPU below SPEED_FLOOR.
    SPEED = float(os.environ.get("BR_SPEED") or 1.8)   # the probe always read its cap on the scorer
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    maps = Maps(MAP, tests)
    load_alike(tests)
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

    log(f"LEDGER main-analysis {time.process_time():.3f}")
    cost_of = maps.cost
    state_on = STATE and os.path.exists(STATE_FILE) and maps.tracked and not NO_EXEC
    probe = set()
    if state_on and LAZY_STATE:
        unstable = state_unstable()
        if unstable is not None:
            probe = {commit for commit, selected, analysis in plans
                     if state_eligible(selected, analysis, unstable, cost_of, seconds)}
    elif state_on:
        refine(plans, texts, analyzer, maps, cost_of, seconds, started, len(names))

    def build(only=None, probing=()):
        """Jobs that run candidates against the patched code."""
        jobs = []
        for commit, selected, analysis in plans:
            if only is not None and commit not in only:
                continue
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
                if commit in probing:
                    job["probe"] = True
                jobs.append(job)
                JOBS[commit] = job
        return jobs

    deadline = started + max(30.0, WALL_PER_COMMIT * len(names) - 8.0)
    # The scorer's two cores are two threads of one core: two children at once
    # slow each other down and every CPU second is charged. So one child at a
    # time, and a second only if the wall-clock limit starts to press.
    hurry = started + 0.45 * (deadline - started)

    def width():
        return 1 if time.time() < hurry else 2

    def second_batch(found):
        """Jobs for the commits whose probe met a failure, after the state comparison."""
        need = {commit for commit in probe
                if isinstance(found.get(commit), dict) and found[commit].pop("state?", None)}
        if not need:
            return None
        # A cheap candidate failed on a class-level change: now the comparison pays.
        log(f"state comparison wanted for {sorted(need)}")
        for commit in need:
            found.pop(commit, None)
        refine(plans, texts, analyzer, maps, cost_of, seconds, started, len(names), only=need)
        return build(only=need)

    def verify(jobs, followup=None):
        from .execute import run_guarded

        preload = ["unittest", "unittest.mock"]
        heavy = ("tests/test_optimizer.py", "tests/test_executor.py")
        if any(t[1].startswith(heavy) for job in jobs for t in job["tests"]):
            preload += ["duckdb"] if os.environ.get("BR_LAZY", "1") == "1" else \
                ["duckdb", "numpy", "pandas", "pandas.testing"]
        try:
            found = run_guarded(jobs, width, deadline, preload, log, warm=WARM,
                                adapt=policy.ADAPT and not os.environ.get("BR_SPEED"),
                                followup=followup)
            if len(found) <= 1 and len(preload) > 2:
                # Nothing came back: a shared library may not load here. Let each
                # child load what it needs on its own instead.
                log("helper returned nothing; retrying without preloading")
                found = run_guarded(jobs, width, deadline, preload[:2], log)
        except Exception as error:
            log(f"verification failed ({error!r}); selecting unrun")
            found = {}
        return found

    # Run candidates against the patched code; keep the ones that fail.
    jobs = build(probing=probe)
    if ONE_HELPER and probe:
        # One helper process for both batches (the second one only if a probe failed).
        verdicts = verify(jobs, followup=second_batch) if jobs else {}
    else:
        verdicts = verify(jobs) if jobs else {}
        try:
            again = second_batch(verdicts)
            if again:
                more = verify(again)
                stats = more.pop("_stats", {})
                verdicts.setdefault("_stats", {}).update(stats)
                verdicts.update(more)
        except Exception as error:  # noqa: BLE001
            log(f"second pass failed ({error!r}); selecting unrun")

    if BASELINE:
        # Every test that failed somewhere in this batch runs once more against the
        # unpatched code, all in one child. One that fails there too fails here
        # whatever the patch is, so its failure says nothing and it is not selected.
        seen = {}
        for commit, selected, analysis in plans:
            verdict = verdicts.get(commit)
            base = JOBS.get(commit)
            if not isinstance(verdict, dict) or base is None:
                continue
            for n in selected:
                if n in seen or verdict.get(n, (None,))[0] != 1:
                    continue
                spec = next((t for t in base["tests"] if t[0] == n), None)
                cost = cost_of.get(n, seconds[n])
                if spec is not None and cost <= BASELINE_MAX:
                    seen[n] = (spec[0], spec[1], spec[2], spec[3], cost, None, 0)
        if seen:
            try:
                job = {"key": "#base", "root": SOURCE, "files": {}, "pickle": False, "prior": None,
                       "tests": sorted(seen.values(), key=lambda t: t[4]), "expensive": [],
                       "forced": [], "plans": {}, "speed": SPEED, "budget": 40.0, "run_all": True}
                found = verify([job]).get("#base") or {}
                broken = {n for n in seen if found.get(n, (0,))[0] == 1}
                log(f"baseline: {len(seen)} failing tests run unpatched, {len(broken)} fail there too"
                    + (": " + ", ".join(sorted(id_of[n] for n in broken))[:300] if broken else ""))
                for commit, selected, analysis in plans:
                    verdict = verdicts.get(commit)
                    if isinstance(verdict, dict):
                        for n in broken:
                            if verdict.get(n, (None,))[0] == 1:
                                verdict[n] = (0, verdict[n][1])
            except Exception as error:  # noqa: BLE001
                log(f"baseline check failed ({error!r})")

    if RECHECK:
        # A commit with only a few failing tests: run each of them once more, alone in
        # a fresh child. A test that passes there failed because of what ran before it
        # in the same process, not because of the patch, and is not selected.
        again = []
        for commit, selected, analysis in plans:
            verdict = verdicts.get(commit)
            base = JOBS.get(commit)
            if not isinstance(verdict, dict) or base is None:
                continue
            bad = [n for n in selected if verdict.get(n, (None,))[0] == 1]
            if not bad or len(bad) > RECHECK:
                continue
            for n in bad:
                spec = [t for t in base["tests"] if t[0] == n]
                if spec:
                    again.append(dict(base, key=f"{commit}#{n}", tests=spec, expensive=[],
                                      capped=[], prior=None, probe=False, run_all=True))
        if again:
            try:
                found = verify(again)
                cleared = 0
                for key, result in found.items():
                    if "#" not in str(key) or not isinstance(result, dict):
                        continue
                    commit, n = key.split("#")
                    n = int(n)
                    if result.get(n, (1,))[0] == 0:
                        verdicts[commit][n] = (0, verdicts[commit][n][1] + result[n][1])
                        cleared += 1
                log(f"recheck: {len(again)} failing tests run again alone, {cleared} passed")
            except Exception as error:  # noqa: BLE001
                log(f"recheck failed ({error!r})")

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
            swapped = verdict.pop("swap", None)
            forced = maps.numbers(analysis.direct) if analysis is not None else ()
            keep, failed, ran, unknown, expensive = decide(
                selected, verdict, forced, RULED_OUT.get(commit, 0), CAPPED.get(commit, ()))
            left_out = [n for n in expensive if n not in keep]
            if left_out:
                # Unrun candidates say nothing about the costly tests: the share of
                # failures only counts as evidence when the others really ran.
                thin = unknown or len(ran) < policy.MIN_RUN
                risk = policy.RISK_UNVERIFIED if thin else policy.RISK_VERIFIED
                keep = insure(keep, left_out, ms, risk)
            if EXP_DROP:
                # Experiment builds only: never select these never-run tests (unless
                # they read a patched file), to see on the scorer what they cost us
                # and whether they really fail.
                keep = set(keep) - {n for n in expensive
                                    if id_of[n].rsplit("::", 1)[1] in EXP_DROP and n not in forced}
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
            if swapped:
                note += f", swapped {swapped[0]}"
            selected = sorted(keep)
        elif (SOFT_BLIND or not SOFT_FORCE) and UNRUNNABLE.get(commit) and analysis is not None:
            direct = set(maps.numbers(analysis.direct)) if SOFT_FORCE else set()
            selected = [n for n in selected if n not in UNRUNNABLE[commit] or n in direct]
            note = "costly candidates left out unrun"
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
