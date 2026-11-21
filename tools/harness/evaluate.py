"""Development harness: compare a run of the selector with the true failing sets.

    python3 tools/harness/evaluate.py <harness_dir> <eval_dir> prepare
        copies the patches that already have answers into <eval_dir>
    python3 tools/harness/evaluate.py <harness_dir> <eval_dir> report <stderr_log>
        per-kind cost against the oracle, and every missed commit
"""
import collections
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from br.schedule import split  # noqa: E402

harness, target, mode = sys.argv[1], sys.argv[2], sys.argv[3]
kinds = json.load(open(os.path.join(harness, "kinds.json")))
tests = json.load(open(os.path.join(harness, "tests.json")))["tests"]
ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
ids = {t["number"]: t["id"] for t in tests}

if mode == "prepare":
    os.makedirs(os.path.join(target, "commits"), exist_ok=True)
    commits = {}
    for name in sorted(os.listdir(os.path.join(harness, "answers"))):
        commit = name[:-5]
        answer = json.load(open(os.path.join(harness, "answers", name)))
        if answer["unknown"]:
            continue
        commits[commit] = {"failing": answer["failing"], "kind": kinds.get(commit, "?")}
        shutil.copy(os.path.join(harness, "commits", commit + ".patch"),
                    os.path.join(target, "commits", commit + ".patch"))
    for stale in os.listdir(os.path.join(target, "commits")):
        if stale[:-6] not in commits:
            os.remove(os.path.join(target, "commits", stale))
    shutil.copy(os.path.join(harness, "tests.json"), os.path.join(target, "tests.json"))
    json.dump({"commits": commits}, open(os.path.join(target, "answers.json"), "w"))
    print(f"{len(commits)} labelled commits in {target}")
    sys.exit(0)

answers = json.load(open(os.path.join(target, "answers.json")))["commits"]
output = {}
for line in open(os.path.join(os.path.dirname(os.path.abspath(sys.argv[4])), "..", "output.jsonl")
                 if False else sys.argv[5]):
    row = json.loads(line)
    output[row["commit"]] = row["workers"]
report = {}
for line in open(sys.argv[4]):
    if line.startswith("REPORT "):
        report = {r["commit"]: r for r in json.loads(line[7:])}
by_kind = collections.defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0, 0])
missed = []
for commit, answer in sorted(answers.items()):
    workers = output.get(commit, [])
    chosen = {n for w in workers for n in w}
    cost = (max((sum(ms[n] for n in w) for w in workers), default=0) + 2000 * len(workers)) / 1000
    oracle = split(answer["failing"], ms)[0] / 1000
    cpu = report.get(commit, {}).get("cpu", 0.0)
    miss = [n for n in answer["failing"] if n not in chosen]
    row = by_kind[answer["kind"]]
    row[0] += 1
    row[1] += cost
    row[2] += cpu
    row[3] += oracle
    row[4] += bool(miss)
    row[5] += bool(answer["failing"])
    if miss:
        missed.append((commit, answer["kind"], len(answer["failing"]), len(miss),
                       [ids[n].split("::", 1)[1] for n in miss[:4]],
                       report.get(commit, {}).get("note", ""),
                       " ".join(report.get(commit, {}).get("notes", []))[:160]))
total = [sum(v[i] for v in by_kind.values()) for i in range(6)]
print(f"{'kind':11s} {'n':>4s} {'fail':>4s} {'sim':>8s} {'cpu':>7s} {'oracle':>8s} {'gap':>8s} {'miss':>4s}")
for kind, v in sorted(by_kind.items()):
    print(f"{kind:11s} {v[0]:4d} {v[5]:4d} {v[1]:8.1f} {v[2]:7.1f} {v[3]:8.1f} {v[1] + v[2] - v[3]:+8.1f} {v[4]:4d}")
print(f"{'TOTAL':11s} {total[0]:4d} {total[5]:4d} {total[1]:8.1f} {total[2]:7.1f} {total[3]:8.1f} "
      f"{total[1] + total[2] - total[3]:+8.1f} {total[4]:4d}   (+{300 * total[4]} s for misses)")
for row in missed:
    print("MISS", *row)
