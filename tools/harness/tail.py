"""Development harness: do costly candidates fail when the cheap ones do not?

Inside the dev image (repo at /app, sqlglot at /opt/sqlglot, harness dir at /h):
    python -m tools.harness.tail /h [jobs] [costly_seconds]
For every patch whose plan holds a candidate at least `costly_seconds` to run, runs
every planned candidate to the end (no early stop) and writes /h/tail/<id>.json:
the verdict and planned cost of each candidate. Nothing here ships in the image.
"""
import json
import os
import sys

sys.path.insert(0, "/app")
from br import main as selector  # noqa: E402
from br.candidates import Analyzer  # noqa: E402
from br.execute import run_jobs  # noqa: E402
from br.maps import Maps  # noqa: E402

harness = sys.argv[1]
parallel = int(sys.argv[2]) if len(sys.argv) > 2 else 4
costly = float(sys.argv[3]) if len(sys.argv) > 3 else 0.4
OUT = sys.argv[4] if len(sys.argv) > 4 else "tail"
tests = json.load(open(os.path.join(harness, "tests.json")))["tests"]
seconds = {t["number"]: t["seconds"] for t in tests}
id_of = {t["number"]: t["id"] for t in tests}
maps = Maps("/app/maps/map.bin", tests)
analyzer = Analyzer(maps, selector.SOURCE, "/app/maps/refs.bin")
os.makedirs(os.path.join(harness, OUT), exist_ok=True)

import unittest  # noqa: E402,F401
import unittest.mock  # noqa: E402,F401
import duckdb  # noqa: E402,F401
import numpy  # noqa: E402,F401
import pandas  # noqa: E402,F401
import pandas.testing  # noqa: E402,F401

jobs = []
for name in sorted(os.listdir(os.path.join(harness, "commits"))):
    commit = name[:-6]
    if os.path.exists(os.path.join(harness, OUT, commit + ".json")):
        continue
    analysis = analyzer.analyze(open(os.path.join(harness, "commits", name), encoding="utf-8").read())
    files = dict(analysis.py_changed)
    files.update(analysis.data_changed)
    if not analysis.strong or any(v is None for v in files.values()):
        continue
    selector.SPEED = 1.0
    job = selector.plan_job(commit, analysis, maps, analyzer, id_of, seconds, maps.cost, files)
    if job is None or not any(t[4] >= costly for t in job["tests"]):
        continue
    if sum(t[4] for t in job["tests"]) > 25:
        continue  # the very hot ones are settled by other rules
    job["run_all"] = True
    job["budget"] = 1e9
    job["prior"] = None
    job["direct"] = maps.numbers(analysis.direct)
    jobs.append(job)
print(f"{len(jobs)} patches with a costly candidate", file=sys.stderr, flush=True)


def done(key, verdicts, stats):
    job = next(j for j in jobs if j["key"] == key)
    out = {"tests": [[t[0], id_of[t[0]], round(t[4], 3), verdicts.get(t[0], (3, 0))[0],
                      verdicts.get(t[0], (3, 0))[1]] for t in job["tests"]],
           "direct": job["direct"]}
    json.dump(out, open(os.path.join(harness, OUT, key + ".json"), "w"))


run_jobs(jobs, parallel=parallel, on_done=done)
print("done", file=sys.stderr)
