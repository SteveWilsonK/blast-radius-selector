"""pytest plugin for the development harness: write the ids of failing tests.

A test fails when any of its phases or sub-tests fails or errors, or when its file
cannot be collected. Output: $OUTCOMES (JSON with "failed" ids and "broken" files).
"""
import json
import os

failed = set()
broken = set()


def pytest_runtest_logreport(report):
    if report.outcome == "failed":
        failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.outcome == "failed":
        broken.add(str(report.nodeid))


def pytest_sessionfinish(session, exitstatus):
    with open(os.environ.get("OUTCOMES", "/out/outcomes.json"), "w") as handle:
        json.dump({"failed": sorted(failed), "broken": sorted(broken)}, handle)
