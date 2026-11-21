"""Development harness: is comparison coverage sound for and-or swap patches?

Inside the dev image (repo at /app, sqlglot at /opt/sqlglot, harness dir at /h):
    python -m tools.harness.boolcheck /h [jobs]
For every patch that only swaps one comparison's operator, runs every test that line
coverage alone would consider (comparison coverage switched off) to the end and
writes /h/boolcheck/<id>.json with each verdict. A failing test that comparison
coverage would have left out is a soundness bug.
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
tests = json.load(open(os.path.join(harness, "tests.json")))["tests"]
seconds = {t["number"]: t["seconds"] for t in tests}
id_of = {t["number"]: t["id"] for t in tests}
maps = Maps("/app/maps/map.bin", tests)
narrow = Analyzer(maps, selector.SOURCE, "/app/maps/refs.bin", "/app/maps/cond.bin")
wide = Analyzer(maps, selector.SOURCE, "/app/maps/refs.bin", None)
os.makedirs(os.path.join(harness, "boolcheck"), exist_ok=True)

import unittest  # noqa: E402,F401
import unittest.mock  # noqa: E402,F401
import duckdb  # noqa: E402,F401
import numpy  # noqa: E402,F401
import pandas  # noqa: E402,F401
import pandas.testing  # noqa: E402,F401

jobs = []
kept = {}
for name in sorted(os.listdir(os.path.join(harness, "commits"))):
    commit = name[:-6]
    if os.path.exists(os.path.join(harness, "boolcheck", commit + ".json")):
        continue
    text = open(os.path.join(harness, "commits", name), encoding="utf-8").read()
    with_cond = narrow.analyze(text)
    if not any(note.startswith("boolswap:") for note in with_cond.notes):
        continue
    analysis = wide.analyze(text)
    files = dict(analysis.py_changed)
    files.update(analysis.data_changed)
    if not analysis.strong or any(v is None for v in files.values()):
        continue
    selector.SPEED = 1.0
    selector.policy.NEVER_RUN = 15.0   # run all but the two very slow tests
    job = selector.plan_job(commit, analysis, maps, wide, id_of, seconds, maps.cost, files)
    if job is None or sum(t[4] for t in job["tests"]) > 40:
        continue
    job["run_all"] = True
    job["budget"] = 1e9
    job["prior"] = None
    jobs.append(job)
    kept[commit] = maps.numbers(with_cond.strong)
print(f"{len(jobs)} and-or swap patches to check", file=sys.stderr, flush=True)


def done(key, verdicts, stats):
    job = next(j for j in jobs if j["key"] == key)
    out = {"tests": [[t[0], id_of[t[0]], verdicts.get(t[0], (3, 0))[0]] for t in job["tests"]],
           "kept": kept[key]}
    json.dump(out, open(os.path.join(harness, "boolcheck", key + ".json"), "w"))


run_jobs(jobs, parallel=parallel, on_done=done)
print("done", file=sys.stderr)
