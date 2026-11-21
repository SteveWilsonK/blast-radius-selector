"""Development harness: true failing tests for each patch, by running every test.

Inside the dev image (repo at /app, sqlglot at /opt/sqlglot, harness dir at /h):
    python -m tools.harness.label /h [jobs] [first] [last]
Writes /h/answers/<id>.json as it goes and /h/answers.json at the end. Uses the same
in-process runner as the selector, with pickling on, no fixture filters, every test.
"""
import json
import os
import sys

sys.path.insert(0, "/app")
from br import patch as patchlib  # noqa: E402
from br.execute import run_jobs  # noqa: E402

harness = sys.argv[1]
parallel = int(sys.argv[2]) if len(sys.argv) > 2 else 4
root = os.environ.get("BR_SOURCE", "/opt/sqlglot")
tests = json.load(open(os.path.join(harness, "tests.json")))["tests"]
names = sorted(f[:-6] for f in os.listdir(os.path.join(harness, "commits")) if f.endswith(".patch"))
first = int(sys.argv[3]) if len(sys.argv) > 3 else 0
last = int(sys.argv[4]) if len(sys.argv) > 4 else len(names)
os.makedirs(os.path.join(harness, "answers"), exist_ok=True)

by_file = {}
for t in tests:
    by_file.setdefault(t["id"].split("::")[0], []).append(t)

import unittest  # noqa: E402,F401
import unittest.mock  # noqa: E402,F401
import duckdb  # noqa: E402,F401
import numpy  # noqa: E402,F401
import pandas  # noqa: E402,F401
import pandas.testing  # noqa: E402,F401

for name in names[first:last]:
    target = os.path.join(harness, "answers", name + ".json")
    if os.path.exists(target):
        continue
    text = open(os.path.join(harness, "commits", name + ".patch"), encoding="utf-8").read()
    files = {}
    ok = True
    for fp in patchlib.parse(text):
        path = fp.old_path or fp.new_path
        try:
            old = open(os.path.join(root, path), encoding="utf-8").read() if fp.old_path else ""
        except OSError:
            ok = False
            break
        new = patchlib.apply(old, fp) if fp.new_path else None
        if fp.new_path and new is None:
            ok = False
            break
        files[path] = new
    if not ok:
        print(f"{name}: patch does not apply", file=sys.stderr)
        continue
    jobs = [{"key": path, "root": root, "files": files, "pickle": True,
             "tests": [(t["number"], t["id"], max(60.0, t["seconds"] * 4), 0.0, 0.0) for t in group]}
            for path, group in by_file.items()]
    # Heaviest files first so the pool stays busy.
    jobs.sort(key=lambda job: -sum(t["seconds"] for t in by_file[job["key"]]))
    results = run_jobs(jobs, parallel=parallel)
    failing, unknown = [], []
    for path, group in by_file.items():
        for t in group:
            status = results[path].get(t["number"], (3, 0.0))[0]
            if status == 1:
                failing.append(t["number"])
            elif status == 3:
                unknown.append(t["number"])
    json.dump({"failing": sorted(failing), "unknown": sorted(unknown)}, open(target, "w"))
    print(f"{name}: {len(failing)} failing, {len(unknown)} unknown", file=sys.stderr, flush=True)

kinds = json.load(open(os.path.join(harness, "kinds.json")))
commits = {}
for name in names:
    target = os.path.join(harness, "answers", name + ".json")
    if os.path.exists(target):
        answer = json.load(open(target))
        commits[name] = {"failing": answer["failing"], "kind": kinds.get(name, "?")}
json.dump({"commits": commits}, open(os.path.join(harness, "answers.json"), "w"))
print(f"{len(commits)} labelled", file=sys.stderr)
