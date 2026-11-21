"""Record comparison coverage for every test, each in its own process.
Usage: cond_all.py tests.json out_dir [jobs]"""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

tests = json.load(open(sys.argv[1]))["tests"]
out = sys.argv[2]
jobs = int(sys.argv[3]) if len(sys.argv) > 3 else 4
os.makedirs(out, exist_ok=True)


def run(test):
    target = os.path.join(out, f"cond-{test['number']}.json")
    if os.path.exists(target):
        return True
    env = dict(os.environ, COND_OUT=out, COND_NAME=str(test["number"]))
    r = subprocess.run(
        [sys.executable, "-m", "pytest", test["id"], "-q", "-p", "cond_plugin",
         "-p", "no:cacheprovider", "-p", "no:xdist", "-x"],
        env=env, capture_output=True, text=True)
    ok = "1 passed" in r.stdout
    if not ok:
        sys.stderr.write(f"NOT PASSED {test['id']}\n{r.stdout[-1200:]}\n{r.stderr[-400:]}\n")
    return ok


with ThreadPoolExecutor(jobs) as pool:
    results = list(pool.map(run, tests))
print(f"{sum(results)} of {len(results)} passed", file=sys.stderr)
