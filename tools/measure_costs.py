"""Time every test in the in-process runner on unpatched code.

Usage (inside the image, repo at /app, sqlglot at /opt/sqlglot):
    python -m tools.measure_costs /data/tests.json out.json
"""
import json
import os
import sys

sys.path.insert(0, "/app")
from br.execute import run_jobs  # noqa: E402

tests = json.load(open(sys.argv[1]))["tests"]
by_file = {}
for t in tests:
    by_file.setdefault(t["id"].split("::")[0], []).append(t)
only = os.environ.get("ONLY")
if only:
    by_file = {p: g for p, g in by_file.items() if only in p}
jobs = [{"key": path, "lazy": os.environ.get("LAZY", "1") == "1", "root": os.environ.get("BR_SOURCE", "/opt/sqlglot"), "files": {},
         "pickle": False, "tests": [(t["number"], t["id"], 300.0, 0.0, 0.0, None, 0) for t in group]}
        for path, group in by_file.items()]
results = run_jobs(jobs, parallel=int(os.environ.get("JOBS", "4")))
out = {}
bad = []
for path, group in by_file.items():
    for t in group:
        status, cpu = results[path].get(t["number"], (3, 0.0))
        out[t["id"]] = cpu
        if status != 0:
            bad.append((t["id"], status))
json.dump(out, open(sys.argv[2], "w"))
print(f"{len(out)} tests, total cpu {sum(out.values()):.1f}s, not passing: {bad[:20]} ({len(bad)})",
      file=sys.stderr)
