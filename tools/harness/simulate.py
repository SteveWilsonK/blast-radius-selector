"""Development harness: replay the selector's run-time policy against known answers.

    python3 tools/harness/simulate.py <sqlglot_checkout> <harness_dir> [<harness_dir> ...]

No test is run. For every labelled patch it plans the job exactly as the selector
does, feeds the true verdicts to the same stopping and selection rules, and reports
simulated seconds, estimated CPU, the oracle cost and any miss. Used to compare
policies quickly; the container run stays the real check.
"""
import collections
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from br import main as selector  # noqa: E402
from br import policy  # noqa: E402
from br.candidates import Analyzer  # noqa: E402
from br.maps import Maps  # noqa: E402
from br.schedule import fill, split  # noqa: E402

source = sys.argv[1]
SPEED = float(os.environ.get("BR_SPEED", "1.0"))
selector.SPEED = SPEED
selector.SOURCE = source
here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "maps")
VERBOSE = os.environ.get("VERBOSE")

totals = collections.defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0, 0.0])
rows = []
for harness in sys.argv[2:]:
    tests = json.load(open(os.path.join(harness, "tests.json")))["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    seconds = {t["number"]: t["seconds"] for t in tests}
    id_of = {t["number"]: t["id"] for t in tests}
    maps = Maps(os.path.join(here, "map.bin"), tests)
    analyzer = Analyzer(maps, source, os.path.join(here, "refs.bin"))
    kinds = json.load(open(os.path.join(harness, "kinds.json")))
    for name in sorted(os.listdir(os.path.join(harness, "answers"))):
        commit = name[:-5]
        answer = json.load(open(os.path.join(harness, "answers", name)))
        if answer["unknown"]:
            continue
        truth = set(answer["failing"])
        text = open(os.path.join(harness, "commits", commit + ".patch")).read()
        analysis = analyzer.analyze(text)
        selected = maps.numbers(analysis.strong)
        files = dict(analysis.py_changed)
        files.update(analysis.data_changed)
        job = None
        if selected and not any(v is None for v in files.values()):
            job = selector.plan_job(commit, analysis, maps, analyzer, id_of, seconds, maps.cost, files)
        cpu = 0.0
        verdict = None
        if job is not None:
            cpu += policy.START_COST * SPEED
            order, groups = [], {}
            for t in job["tests"]:
                key = (t[1].rsplit("::", 1)[0], t[6])
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                groups[key].append(t)
            tracker = policy.Tracker([(t[0], t[3], t[4]) for t in job["tests"]],
                                     job["expensive"], SPEED, job.get("prior"))
            verdict = {}
            stopped = False
            skipped = None
            forced = set(job.get("forced", ()))
            for key in order:
                for t in groups[key]:
                    if stopped or tracker.should_stop():
                        stopped = True
                        verdict[t[0]] = (3 if (skipped is None or t[0] in forced) else 4, 0.0)
                        continue
                    if t[0] not in forced and tracker.skip_costly(t[4]):
                        stopped = True
                        skipped = True
                        verdict[t[0]] = (4, 0.0)
                        continue
                    failed = t[0] in truth
                    # a failing test stops at its first failure: charge part of it
                    spent = t[4] * SPEED * (0.5 if failed else 1.0)
                    cpu += spent
                    verdict[t[0]] = (1 if failed else 0, spent)
                    tracker.record(t[3], t[4], failed, spent)
            keep = selector.decide(selected, verdict, maps.numbers(analysis.direct))[0]
            chosen = sorted(keep)
        else:
            chosen = sorted(set(selected))
        cost, workers = split(chosen, ms)
        if workers:
            spare = [n for n in selected if n not in set(chosen)]
            spare.sort(key=lambda n: ((verdict or {}).get(n, (3,))[0] == 0, ms[n], n))
            fill(workers, ms, spare)
        final = {n for w in workers for n in w}
        miss = truth - final
        oracle = split(sorted(truth), ms)[0] / 1000
        kind = kinds.get(commit, "sample")
        row = totals[kind]
        row[0] += 1
        row[1] += cost / 1000
        row[2] += cpu
        row[3] += oracle
        row[4] += bool(miss)
        rows.append((cost / 1000 + cpu - oracle, commit, kind, len(selected), len(truth),
                     cost / 1000, cpu, oracle, len(miss),
                     " ".join(n for n in analysis.notes if not n.startswith("read:"))[:90]))

print(f"{'kind':11s} {'n':>4s} {'sim':>8s} {'cpu':>7s} {'oracle':>8s} {'gap':>8s} {'miss':>4s}")
total = [0, 0.0, 0.0, 0.0, 0]
for kind, v in sorted(totals.items()):
    print(f"{kind:11s} {v[0]:4d} {v[1]:8.1f} {v[2]:7.1f} {v[3]:8.1f} {v[1] + v[2] - v[3]:+8.1f} {v[4]:4d}")
    for i in range(5):
        total[i] += v[i]
print(f"{'TOTAL':11s} {total[0]:4d} {total[1]:8.1f} {total[2]:7.1f} {total[3]:8.1f} "
      f"{total[1] + total[2] - total[3]:+8.1f} {total[4]:4d}   per commit: "
      f"{(total[1] + total[2] + 300 * total[4]) / max(1, total[0]):.2f} vs oracle {total[3] / max(1, total[0]):.2f}")
rows.sort(reverse=True)
for row in rows[: int(VERBOSE or 12)]:
    print(f"  {row[1]} {row[2]:9s} cand={row[3]:5d} fail={row[4]:4d} sim={row[5]:6.2f} cpu={row[6]:6.2f} "
          f"oracle={row[7]:6.2f} miss={row[8]} {row[9]}")
for row in rows:
    if row[8]:
        print("  MISS", row[1], row[2], row[9])
