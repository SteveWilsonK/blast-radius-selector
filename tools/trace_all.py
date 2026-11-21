"""Trace every test in its own process. Usage: trace_all.py tests.json out_dir [jobs]"""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

tests = json.load(open(sys.argv[1]))["tests"]
out = sys.argv[2]
jobs = int(sys.argv[3]) if len(sys.argv) > 3 else 8
os.makedirs(out, exist_ok=True)


def run(test):
    env = dict(os.environ, TRACE_OUT=out, TRACE_NAME=str(test["number"]))
    r = subprocess.run(
        [sys.executable, "-m", "pytest", test["id"], "-q", "-p", "trace_plugin",
         "-p", "no:cacheprovider", "-p", "no:xdist", "-x"],
        env=env, capture_output=True, text=True)
    ok = "1 passed" in r.stdout
    if not ok:
        sys.stderr.write(f"NOT PASSED {test['id']}\n{r.stdout[-1500:]}\n{r.stderr[-500:]}\n")
    return ok


with ThreadPoolExecutor(jobs) as pool:
    results = list(pool.map(run, tests))
print(f"{sum(results)} of {len(results)} passed", file=sys.stderr)
