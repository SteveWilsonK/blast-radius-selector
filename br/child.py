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
# On: when function code cannot be swapped, reload only the patched modules and the
# modules that import them (fewer than PARTIAL_MAX), not all of sqlglot.
PARTIAL_RELOAD = os.environ.get("BR_PARTIAL", "1") == "1"
PARTIAL_MAX = int(os.environ.get("BR_PARTIAL_MAX", "40"))
# On: the per-test cap holds only until the commit's first failure.
CAP_BEFORE = os.environ.get("BR_CAP_BEFORE", "0") == "1"
_capped = [False]
_cap_hit = [False]
VALUE_ORDER = os.environ.get("BR_VALUE_ORDER", "1") == "1"
_ignore = [False]
DIAG_BODIES = os.environ.get("BR_DIAG", "") == "bodies"
# CPU seconds one commit's child may spend before it stops running tests (0 = no limit).
COMMIT_BUDGET = float(os.environ.get("BR_COMMIT_BUDGET", "0.15"))
STAKES_K = float(os.environ.get("BR_STAKES_K", "0.4"))
CHEAP_FAIL = os.environ.get("BR_CHEAP_FAIL", "1") == "1"
STATE_STAKES = float(os.environ.get("BR_STATE_STAKES", "0"))
# On: the flat budget holds until the first failure, k x stakes after it.
STAKES_AFTER = os.environ.get("BR_STAKES_AFTER", "1") == "1"
_allowed = [False]
# After a commit's first failure a test may use at most its recorded seconds times this
# in CPU before it is abandoned and selected unrun (0 = no such allowance).
ALLOW = float(os.environ.get("BR_ALLOW", "0"))
# Price of a CPU second when, after a commit's first failure, each remaining candidate is
# judged on its own (0 = off: the remaining candidates are run or left as one block).
PICK = float(os.environ.get("BR_PICK", "0"))


def _nothing(*args, **kwargs):
    return None
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
            _cap_hit[0] = True
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
        if err is not None and _ignore[0]:
            return
        if err is not None:
            if _capped[0] and err[0] is TestTimeout:
                _timeouts[0] -= 1     # stopped at the cap inside a sub-test: a pass
                _cap_hit[0] = True
                return
            self._mark(test)
            self._note(subtest, err)


_timeouts = [0]
# Run cost left (seconds) above which a failed probe hands over to the state comparison.
PROBE_REST = float(os.environ.get("BR_PROBE_REST", "0.3"))


_reimport = [None]
_GRAPH = []


def _importers(mods):
    """The modules that import any of mods while they load, directly or not (from the
    import graph of the pinned source built with the image), mods included."""
    if not _GRAPH:
        reverse = {}
        try:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "maps", "modgraph.json")
            with open(path) as handle:
                for name, deps in json.load(handle).items():
                    for dep in deps:
                        reverse.setdefault(dep, set()).add(name)
        except Exception:  # noqa: BLE001
            reverse = None
        _GRAPH.append(reverse)
    reverse = _GRAPH[0]
    if reverse is None:
        return None
    seen = set(mods)
    todo = list(mods)
    while todo:
        for user in reverse.get(todo.pop(), ()):
            if user not in seen:
                seen.add(user)
                todo.append(user)
    return seen


def _partial_purge(job):
    """Drop only the patched sqlglot modules and their importers from sys.modules (and
    the test modules holding something of theirs). None: nothing done, purge all."""
    import types

    from . import overlay

    mods = []
    for rel in job["files"]:
        if not rel.endswith(".py"):
            continue
        if not rel.startswith("sqlglot/"):
            return None
        mods.append(overlay.module_name(rel)[0])
    if not mods:
        return None
    targets = _importers(mods)
    if targets is None or len(targets) >= PARTIAL_MAX:
        return None
    stale_tests = []
    cached = []
    for name, module in list(sys.modules.items()):
        if module is None or name in targets:
            continue
        in_tests = name == "tests" or name.startswith("tests.")
        if not (in_tests or name == "sqlglot" or name.startswith("sqlglot.")):
            continue
        for key, value in list(vars(module).items()):
            try:
                owner = value.__name__ if isinstance(value, types.ModuleType) else \
                    getattr(value, "__module__", None)
            except Exception:  # noqa: BLE001
                continue
            if owner not in targets:
                continue
            if isinstance(value, types.ModuleType) and owner.rsplit(".", 1)[0] == name:
                continue          # the package's own submodule attribute: re-import resets it
            if in_tests:
                stale_tests.append(name)
                break
            if name == "sqlglot.dialects":
                cached.append(key)  # filled lazily by the package's __getattr__
                continue
            return None           # another sqlglot module holds one of their objects
    for key in cached:
        sys.modules["sqlglot.dialects"].__dict__.pop(key, None)
    for name in list(targets) + stale_tests:
        sys.modules.pop(name, None)
    _reimport[0] = sorted(n for n in targets if n not in sys.modules)
    return targets
def _cheap_failures():
    """A failing comparison only needs to fail: no diff, no pretty-printed repr.
    Equality is still ==, so every verdict stays the same."""
    import contextlib
    import unittest

    def equal(self, first, second, msg=None, *args, **kwargs):
        if not first == second:
            raise self.failureException(msg or "not equal")

    unittest.TestCase._getAssertEqualityFunc = lambda self, first, second: equal.__get__(self)
    for name in ("assertMultiLineEqual", "assertListEqual", "assertTupleEqual",
                 "assertSequenceEqual", "assertDictEqual", "assertSetEqual"):
        setattr(unittest.TestCase, name, equal)

    @contextlib.contextmanager
    def sub_test(self, msg=None, **params):
        # Fail-fast already ends a test at its first failing sub-test; a skipped
        # sub-test still lets the others run.
        try:
            yield
        except unittest.SkipTest:
            pass

    unittest.TestCase.subTest = sub_test


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
    if CHEAP_FAIL:
        _cheap_failures()
    # sqlglot may already be loaded, unpatched, by the parent. That copy is only good
    # if the patch touches none of the loaded modules; otherwise start clean.
    patched = [overlay.module_name(rel)[0] for rel in job["files"] if rel.endswith(".py")]
    if any(name in sys.modules for name in patched) or any(
            rel.startswith("tests/") and rel.endswith(".py") for rel in job["files"]):
        swaps = None
        if job.get("load_lines") is not None and os.environ.get("BR_SWAP", "1") == "1":
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
            partial = _partial_purge(job) if PARTIAL_RELOAD else None
            if partial is None:
                for name in list(sys.modules):
                    if name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests.")):
                        del sys.modules[name]
        elif os.environ.get("BR_REPORT"):
            out.write(json.dumps(["swap", len(swaps), 0.0]) + "\n")
    inline_pool.install(job["pickle"], lazy=bool(job.get("giant_cap")))
    overlay.install(root, job["files"])
    if _reimport[0]:
        # The patched modules and their importers load again now, through the overlay,
        # so every registry they fill holds the new classes before any test runs.
        from importlib import import_module as _load_again

        names, _reimport[0] = _reimport[0], None
        try:
            # Registries keep classes by name: drop those of the reloaded modules first,
            # so nothing old is found if a module no longer defines them.
            registry = getattr(sys.modules.get("sqlglot.dialects.dialect"), "Dialect", None)
            classes = getattr(registry, "_classes", None)
            if isinstance(classes, dict):
                for key, value in list(classes.items()):
                    if getattr(value, "__module__", None) in names:
                        del classes[key]
            for name in names:
                _load_again(name)
        except BaseException:  # noqa: BLE001
            # A patched module that cannot load must fail exactly as on a fresh start.
            for name in list(sys.modules):
                if name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests.")):
                    del sys.modules[name]
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
    case_order = job.get("case_order") or {}
    if case_order or any(t[5] is not None for t in job["tests"]):
        try:
            import tests.helpers as helpers

            def wrap(original):
                def loader(filename):
                    cases = active["cases"]
                    seq = case_order.get(filename)
                    if cases is None:
                        allowed = None
                    else:
                        # A test cut down by statement (see slicer) loads fixtures in full.
                        allowed = cases.get(filename, None if "@body" in cases else ())
                    if seq and allowed != "changed":
                        # Cheapest cases first, so a test stopped early has seen the most
                        # cases for its CPU; cases outside the list keep their file order.
                        items = list(original(filename))
                        listed = set(seq)
                        for index in seq:
                            if index < len(items) and (allowed is None or index in allowed):
                                yield items[index]
                        for index, item in enumerate(items):
                            if index not in listed and (allowed is None or index in allowed):
                                yield item
                        return
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
    skip = set(job.get("skip", ()))
    started_at = time.process_time()
    budget_base = [started_at]   # moved on by the CPU of sliced proxy tests (not budgeted)
    unrun_here = set()
    soft = set(job.get("capped", ()))
    giant = set(job.get("giant_cap", ()))
    tp_first = set(job.get("tp_first", ()))   # run whatever the stop rules say
    giant_budget = float(job.get("giant_budget") or 0.0)
    from .policy import SOFT_CAP as soft_cap
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
    position = 0
    resorted = False
    while position < len(order):
        if VALUE_ORDER and tracker.failed and not resorted and not probe:
            # The commit has a failure, so anything left unrun will be selected. From
            # here on the candidates whose recorded seconds are largest for the CPU
            # they take go first: a CPU budget then cuts off the least rewarding ones.
            resorted = True

            def worth(number):
                recorded, run_cost = info[number]
                return recorded / max(run_cost, 0.0005)

            rest = order[position:]
            for key in rest:
                by_class[key].sort(key=lambda item: (item[0] not in tp_first, -worth(item[0])))
            rest.sort(key=lambda key: (not any(item[0] in tp_first for item in by_class[key]),
                                       -max(worth(item[0]) for item in by_class[key])))
            order[position:] = rest
        path, cls_name, part = order[position]
        position += 1
        items = by_class[(path, cls_name, part)]
        if DROP_REST and fail_scope and not probe and \
                (path if DROP_REST == "file" else (path, cls_name)) not in fail_scope:
            for number, _, _ in items:
                out.write(json.dumps([number, 3 if number in forced else 4, 0.0]) + "\n")
            continue
        if stop_all:
            for number, _, _ in items:
                code = 5 if number in skip else (3 if number in forced else stop_code)
                out.write(json.dumps([number, code, 0.0]) + "\n")
            continue
        if skip:
            # Planned like any other candidate, but too costly to run: the caller
            # decides these on other evidence.
            for number, _, _ in items:
                if number in skip:
                    out.write(json.dumps([number, 5, 0.0]) + "\n")
            items = [item for item in items if item[0] not in skip]
            if not items:
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
            if DIAG_BODIES:
                # Measuring build: the test's body runs twice; only the first run counts.
                def twice(_body=getattr(case, method)):
                    _body()
                    _ignore[0] = True
                    try:
                        _body()
                    except TestTimeout:
                        raise
                    except BaseException:  # noqa: BLE001
                        pass
                    finally:
                        _ignore[0] = False

                setattr(case, method, twice)
            cases.append((number, case, limit))
        # Drive the suite by hand so each test is timed and reported as it ends, while
        # class and module fixtures still run once (TestSuite.run handles them).
        state = {"i": 0, "t0": 0.0}

        def start(test, _state=state, _cases=cases, module=module):
            if _cases[_state["i"]][0] not in tp_first:
                if probe and tracker.failed and tracker.todo_cpu > PROBE_REST:
                    raise NeedState()
                if not job.get("run_all") and time.process_time() > budget:
                    raise OutOfBudget()
                if (STAKES_K > 0 and not job.get("run_all") and (tracker.failed or not STAKES_AFTER)
                        and time.process_time() - budget_base[0] > STAKES_K * tracker.stakes()):
                    # One CPU second costs one score second: stop once the CPU spent here
                    # passes k times what verifying the rest could still save.
                    if tracker.failed:
                        raise OutOfBudget()
                    raise SkipRest()
                if (COMMIT_BUDGET > 0 and not job.get("run_all") and not (STAKES_AFTER and tracker.failed)
                        and time.process_time() - budget_base[0] > COMMIT_BUDGET):
                    # This commit has had its CPU: what is left is selected if a failure
                    # was seen and taken as passing if none was.
                    if tracker.failed:
                        raise OutOfBudget()
                    raise SkipRest()
                upcoming = _cases[_state["i"]][0]
                # Enough clean runs: the rest is taken as passing, however many are left
                # (before the cost rule, which would select a large rest unrun).
                if not job.get("run_all") and upcoming not in forced and tracker.skip_costly(info[upcoming][1]):
                    raise SkipRest()
                if PICK > 0 and tracker.failed and not job.get("run_all"):
                    # The commit already has a failing test, so whatever is left unrun gets
                    # selected. Each remaining candidate is judged on its own: run it only
                    # if the scheduled seconds it would cost unrun outweigh the CPU of
                    # running it; otherwise leave it unrun (and selected).
                    recorded, run_cost = info[upcoming]
                    share = (tracker.failed + 0.2) / (tracker.ran + 1.0)
                    if recorded * (1.0 - share) <= run_cost * PICK:
                        unrun_here.add(upcoming)
                        test.setUp = test.tearDown = _nothing
                        setattr(test, test._testMethodName, _nothing)
                        tracker.todo_sim -= recorded
                        tracker.todo_cpu -= run_cost
                        _state["t0"] = time.process_time()
                        return
                elif not job.get("run_all") and tracker.should_stop():
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
            cap = giant_budget if number in giant else (soft_cap if number in soft else (
                0 if (CAP_BEFORE and tracker.failed) else CAP))
            _allowed[0] = False
            if ALLOW > 0 and tracker.failed and number not in giant:
                # The commit already has a failure, so this test is selected if left
                # unrun. Checking it may cost at most what selecting it would: its
                # recorded seconds (times ALLOW). Past that it is abandoned and selected.
                allowance = max(0.01, info[number][0] * ALLOW)
                if not cap or allowance < cap:
                    cap = allowance
                    _allowed[0] = True
            _cap_hit[0] = False
            _capped[0] = bool(cap) and cap < limit
            signal.setitimer(signal.ITIMER_PROF, cap if _capped[0] else limit)

        def finish(test, _state=state, _cases=cases, _result=result, union=union, module=module):
            _capped[0] = False
            if "dialects" in _state:
                module.Dialects = _state.pop("dialects")
            # Between tests the class fixtures of the next one may run: keep an alarm.
            signal.setitimer(signal.ITIMER_PROF, _state["phase"] if not _timeouts[0] else 2.0)
            active["cases"] = union
            number, case, _ = _cases[_state["i"]]
            _state["i"] += 1
            if number in unrun_here:
                out.write(json.dumps([number, 3, 0.0]) + "\n")
                _state["reported"] = _state.get("reported", 0) + 1
                return
            cpu = (time.process_time() - _state["t0"]) * SLOW
            if number in giant or number in tp_first:
                budget_base[0] += time.process_time() - _state["t0"]
            failed = 1 if case.id() in _result.bad else 0
            # A giant test stopped at its CPU allowance with no failure so far: 6.
            status = 6 if (number in giant and _cap_hit[0] and not failed) else failed
            if _allowed[0] and _cap_hit[0] and not failed:
                # Abandoned at its allowance: unrun, so the caller selects it.
                out.write(json.dumps([number, 3, round(cpu, 4)]) + "\n")
                _state["reported"] = _state.get("reported", 0) + 1
                recorded, run_cost = info[number]
                tracker.todo_sim -= recorded
                tracker.todo_cpu -= run_cost
                return
            out.write(json.dumps([number, status, round(cpu, 4)]) + "\n")
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
