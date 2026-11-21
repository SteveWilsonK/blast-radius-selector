"""When to stop running candidates and what to select in the end.

Costs are in scorer seconds. A candidate can be run here (its CPU time is charged
once) or selected without running (it then weighs on the simulated schedule).
"""
WORKER = 2.0
MAX_WORKERS = 8
# A test this costly to run here is never run: it is selected or left out on evidence.
NEVER_RUN = 5.0
# Share of run candidates that must fail before unrun costly candidates are selected.
EXPENSIVE_SHARE = 0.08
# Diagnostic builds only: run every candidate that can be run, whatever it costs, so the
# server's numbers show the best reachable simulated time and the full CPU bill.
ALWAYS_RUN = True
# Passing tests to see before the rest may be selected unrun.
MIN_RUN = 8
# A candidate that costs at least this many scorer seconds to run is put off to the
# end. If by then enough cheaper candidates have run and none has failed, it is
# neither run nor selected (0 disables the rule).
COSTLY = float(__import__("os").environ.get("BR_COSTLY", "0"))
COSTLY_EVIDENCE = 20
# CPU seconds to fork a child and load the patched code before the first test.
START_COST = 0.15
# A patch to a data file a test reads nearly always fails that test.
DATA_PRIOR = 0.9


def select_expensive(ran, failed):
    """Are the candidates too costly to run selected, given how the others fared?"""
    if not ran:
        return True  # nothing else could be asked
    return failed / ran >= EXPENSIVE_SHARE


def bound(total, biggest):
    """Cheapest schedule for tests summing to `total` seconds, longest `biggest`."""
    if total <= 0:
        return 0.0
    best = None
    for k in range(1, MAX_WORKERS + 1):
        cost = max(biggest, total / k) + WORKER * k
        if best is None or cost < best:
            best = cost
    return best


class Tracker:
    """Follows one commit's run; `should_stop` says when selecting the rest is cheaper."""

    def __init__(self, runnable, expensive, speed=1.0, prior=None):
        # prior: chance that a candidate fails, known before anything has run
        self.prior = prior
        # runnable: [(number, recorded_seconds, run_cost)] in run order
        self.todo_sim = sum(r[1] for r in runnable)
        self.todo_max = max((r[1] for r in runnable), default=0.0)
        self.todo_cpu = sum(r[2] for r in runnable)
        self.exp_sim = sum(e[1] for e in expensive)
        self.exp_max = max((e[1] for e in expensive), default=0.0)
        self.fail_sim = 0.0
        self.fail_max = 0.0
        self.failed = 0
        self.ran = 0
        self.speed = speed
        self.predicted = 0.0
        self.measured = 0.0

    def record(self, recorded, run_cost, failed, cpu):
        self.ran += 1
        self.todo_sim -= recorded
        self.todo_cpu -= run_cost
        self.predicted += run_cost
        self.measured += cpu
        if failed:
            self.failed += 1
            self.fail_sim += recorded
            self.fail_max = max(self.fail_max, recorded)

    def expensive_selected(self):
        return self.exp_sim > 0 and self.ran > 0 and select_expensive(self.ran, self.failed)

    def skip_costly(self, run_cost):
        """Leave a costly candidate out: nothing cheaper failed, and enough was tried."""
        return (COSTLY > 0 and run_cost * self.speed >= COSTLY and not self.failed
                and self.ran >= COSTLY_EVIDENCE)

    def should_stop(self):
        if ALWAYS_RUN or self.todo_sim <= 1e-9:
            return False
        if self.ran < MIN_RUN and not self.failed and self.prior is None:
            return False
        base_sim, base_max = self.fail_sim, self.fail_max
        if self.expensive_selected():
            base_sim += self.exp_sim
            base_max = max(base_max, self.exp_max)
        with_rest = bound(base_sim + self.todo_sim, max(base_max, self.todo_max))
        blind = with_rest - bound(base_sim, base_max)
        if self.prior is not None and not self.ran:
            share = self.prior
        else:
            share = (self.failed + 0.2) / (self.ran + 1.0)
        run = self.todo_cpu * self.speed + share * blind
        if not self.ran:
            run += START_COST * self.speed
        return blind <= run


# Seconds the loop below took on the machine the run costs were measured on.
REFERENCE_LOOP = 0.0048


def _loop():
    total = 0
    table = {}
    for i in range(60000):
        key = i % 97
        table[key] = table.get(key, 0) + i
        total += len(str(key))
    return total


def machine_speed():
    """How much slower this machine is than the one the run costs come from.

    The fastest of several short loops, so a busy neighbour cannot inflate it, and
    kept within a band so that one odd reading cannot swing the decisions.
    """
    import time

    best = None
    for _ in range(12):
        started = time.process_time()
        _loop()
        took = time.process_time() - started
        if best is None or took < best:
            best = took
    return max(0.7, min(1.8, best / REFERENCE_LOOP))
