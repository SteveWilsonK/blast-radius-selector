"""Split selected tests over workers to minimise busiest load + 2 s per worker."""
WORKER_MS = 2000
MAX_WORKERS = 8


def _lpt(order, ms, k):
    loads = [0] * k
    workers = [[] for _ in range(k)]
    for n in order:
        i = loads.index(min(loads))
        loads[i] += ms[n]
        workers[i].append(n)
    return loads, workers


def _improve(loads, workers, ms, floor):
    """Move or swap tests off the busiest worker while that lowers the makespan."""
    for _ in range(200):
        top = max(loads)
        if top <= floor:
            break
        b = loads.index(top)
        best = None
        for j in range(len(loads)):
            if j == b:
                continue
            for x in workers[b]:
                # move x from b to j
                new_top = max(top - ms[x], loads[j] + ms[x])
                if new_top < top and (best is None or new_top < best[0]):
                    best = (new_top, j, x, None)
                for y in workers[j]:
                    delta = ms[x] - ms[y]
                    if delta <= 0:
                        continue
                    new_top = max(top - delta, loads[j] + delta)
                    if new_top < top and (best is None or new_top < best[0]):
                        best = (new_top, j, x, y)
        if best is None:
            break
        _, j, x, y = best
        workers[b].remove(x)
        workers[j].append(x)
        loads[b] -= ms[x]
        loads[j] += ms[x]
        if y is not None:
            workers[j].remove(y)
            workers[b].append(y)
            loads[j] -= ms[y]
            loads[b] += ms[y]
    return loads, workers


def split(numbers, ms):
    """numbers: test numbers; ms: number -> duration in ms. Returns (cost_ms, workers)."""
    if not numbers:
        return 0, []
    order = sorted(numbers, key=lambda n: (-ms[n], n))
    total = sum(ms[n] for n in order)
    longest = ms[order[0]]
    best = None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        floor = max(longest, -(-total // k))
        if best is not None and floor + WORKER_MS * k >= best[0]:
            continue
        loads, workers = _lpt(order, ms, k)
        if max(loads) > floor and len(order) <= 400:
            loads, workers = _improve(loads, workers, ms, floor)
        used = [w for w in workers if w]
        cost = max(loads) + WORKER_MS * len(used)
        if best is None or cost < best[0]:
            best = (cost, used)
    return best


def fill(workers, ms, extras):
    """Add `extras` (most valuable first) to workers' idle time without raising the cost.

    Every worker but the busiest idles until the busiest ends, so tests that fit
    there are selected for free. Returns the numbers that were added.
    """
    if not workers:
        return []
    loads = [sum(ms[n] for n in w) for w in workers]
    top = max(loads)
    added = []
    for n in extras:
        need = ms[n]
        choice = None
        for i, load in enumerate(loads):
            if load + need <= top and (choice is None or load > loads[choice]):
                choice = i  # tightest fit keeps room for longer tests elsewhere
        if choice is not None:
            workers[choice].append(n)
            loads[choice] += need
            added.append(n)
    return added
