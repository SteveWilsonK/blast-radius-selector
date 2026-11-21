"""Runs inside a forked child: load the patched sqlglot and run chosen tests."""
import importlib
import json
import os
import signal
import sys
import time
import unittest


ROW_READERS = ("test_execute_tpch", "test_execute_tpcds")
# CPU seconds allowed for loading a test file plus its class set-up.
PHASE_LIMIT = 10.0


class TestTimeout(BaseException):
    pass


class OutOfBudget(BaseException):
    pass


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
        self._mark(test)

    def addFailure(self, test, err):
        self._mark(test)

    def addUnexpectedSuccess(self, test):
        self._mark(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test)


_timeouts = [0]


def _alarm(signum, frame):
    _timeouts[0] += 1
    raise TestTimeout()


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
    inline_pool.install(job["pickle"])
    overlay.install(root, job["files"])
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
                    allowed = cases.get(filename, ())
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
    import_cpu = 0.0
    for number, test_id, limit, _, _, _, part in job["tests"]:
        path, cls, method = test_id.split("::")
        key = (path, cls, part)
        if key not in by_class:
            by_class[key] = []
            order.append(key)
        by_class[key].append((number, method, limit))

    budget = time.process_time() + job.get("budget", 1e9)
    for path, cls_name, part in order:
        items = by_class[(path, cls_name, part)]
        if stop_all:
            for number, _, _ in items:
                out.write(json.dumps([number, 3, 0.0]) + "\n")
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
            continue
        # While class fixtures load, allow every case any test of this group needs.
        union = {}
        for number, _, _ in items:
            cases = case_filter[number]
            if cases is None:
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

        def start(test, _state=state, _cases=cases):
            if time.process_time() > budget or tracker.should_stop():
                raise OutOfBudget()
            if _timeouts[0] >= 3:
                raise OutOfBudget()  # the patch hangs tests: stop paying for it
            _state["t0"] = time.process_time()
            number = _cases[_state["i"]][0]
            active["cases"] = case_filter[number]
            limit = _cases[_state["i"]][2]
            if _timeouts[0]:
                # One test already ran out of time: give the rest far less rope.
                limit = min(limit, 2.0 + 4.0 * info[number][1])
            signal.setitimer(signal.ITIMER_PROF, limit)

        def finish(test, _state=state, _cases=cases, _result=result, union=union):
            # Between tests the class fixtures of the next one may run: keep an alarm.
            signal.setitimer(signal.ITIMER_PROF, _state["phase"] if not _timeouts[0] else 2.0)
            active["cases"] = union
            number, case, _ = _cases[_state["i"]]
            _state["i"] += 1
            cpu = time.process_time() - _state["t0"]
            failed = 1 if case.id() in _result.bad else 0
            out.write(json.dumps([number, failed, round(cpu, 4)]) + "\n")
            _state["reported"] = _state.get("reported", 0) + 1
            recorded, run_cost = info[number]
            tracker.record(recorded, run_cost, failed, cpu)

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
        except BaseException:  # noqa: BLE001
            pass
        signal.setitimer(signal.ITIMER_PROF, 0)
        for number, case, _ in cases[state.get("reported", 0):]:
            out.write(json.dumps([number, unrun, 0.0]) + "\n")
    out.write(json.dumps(["cpu", round(time.process_time(), 3), round(import_cpu, 3)]) + "\n")
    out.write("done\n")
    out.flush()
