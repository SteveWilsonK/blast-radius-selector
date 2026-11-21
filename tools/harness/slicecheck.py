"""Check statement slicing on the unpatched code: any slice of a passing test must pass.

Usage (in the dev image, cwd = sqlglot checkout): slicecheck.py tests.json [trials] [seed]
For every test that can be sliced, picks random sets of "needed" statements, lets the
slicer add what they depend on, runs the shortened test and reports any that fails.
"""
import ast
import importlib
import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.getcwd())
sys.path.insert(0, "/app")
from br import inline_pool, slicer  # noqa: E402

if __name__ != "__main__":
    raise SystemExit(0)
inline_pool.install(False)
import logging  # noqa: E402

logging.getLogger("sqlglot").propagate = True
costs = json.load(open("/app/maps/costs.arm64.json")) if os.path.exists("/app/maps/costs.arm64.json") else {}
tests = [t for t in json.load(open(sys.argv[1]))["tests"] if costs.get(t["id"], 0) < 3.0]
trials = int(sys.argv[2]) if len(sys.argv) > 2 else 3
random.seed(int(sys.argv[3]) if len(sys.argv) > 3 else 1)
os.environ["SKIP_INTEGRATION"] = "0"
trees, sources = {}, {}
ran = failed = skipped = whole = 0
for test in tests:
    path, class_name, method = test["id"].split("::")
    if path not in trees:
        text = open(path, encoding="utf-8").read()
        trees[path] = ast.parse(text)
        sources[path] = text.splitlines(keepends=True)
    plan = slicer.analyse(trees[path], class_name, method)
    if plan is None or len(plan["spans"]) < 4:
        skipped += 1
        continue
    module = importlib.import_module(path[:-3].replace("/", "."))
    cls = getattr(module, class_name)
    original = cls.__dict__.get(method)
    if original is None:
        skipped += 1
        continue
    count = len(plan["spans"])
    for trial in range(trials):
        size = (1, max(1, count // 10), max(1, count // 2))[trial % 3]
        needed = set(random.sample(range(count), size))
        kept = slicer.keep(plan, needed)
        if len(kept) >= count:
            whole += 1
            continue
        try:
            setattr(cls, method, slicer.build(plan, kept, sources[path], class_name, method,
                                              os.path.abspath(path), module.__dict__))
            result = unittest.TestResult()
            suite = unittest.TestSuite([cls(method)])
            suite.run(result)
            bad = result.errors + result.failures
        except BaseException as error:  # noqa: BLE001
            bad = [(None, repr(error))]
        finally:
            setattr(cls, method, original)
        ran += 1
        if bad:
            failed += 1
            print(f"FAIL {test['id']} needed={sorted(needed)[:8]} kept={len(kept)}/{count}\n"
                  f"   {chr(10).join(bad[0][1].strip().splitlines()[-7:])[:900]}")
print(f"slices run {ran}, failed {failed}, whole test kept {whole}, not sliceable {skipped}")
