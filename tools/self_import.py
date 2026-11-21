"""Function lines sqlglot runs while its own modules load, at the pinned commit.

A table that such a line reads is turned into other structures before any test
runs, so reads of its name during tests do not tell who depends on it.
    PYTHONPATH=/sqlglot python self_import.py out.json
"""
import importlib
import json
import pkgutil
import sys

ROOT = "/sqlglot/"
seen = set()
TOOL = sys.monitoring.COVERAGE_ID
DISABLE = sys.monitoring.DISABLE


def on_line(code, line):
    if code.co_flags & 1 and code.co_filename.startswith(ROOT):
        seen.add((code.co_filename[len(ROOT):], line))
    return DISABLE


sys.monitoring.use_tool_id(TOOL, "self-import")
sys.monitoring.register_callback(TOOL, sys.monitoring.events.LINE, on_line)
sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_START,
                                 lambda code, offset: on_line(code, code.co_firstlineno))
sys.monitoring.set_events(TOOL, sys.monitoring.events.LINE | sys.monitoring.events.PY_START)

import sqlglot  # noqa: E402
from sqlglot.dialects.dialect import Dialect  # noqa: E402

# Which dialects are put to work while modules load (parsing default types and so on).
used = set()
_init = Dialect.__init__


def _dialect_init(self, **kwargs):
    used.add(type(self).__module__ + ":" + type(self).__qualname__)
    _init(self, **kwargs)


Dialect.__init__ = _dialect_init

for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    try:
        importlib.import_module(info.name)
    except Exception as error:  # noqa: BLE001
        print(f"skipped {info.name}: {error!r}", file=sys.stderr)
sys.monitoring.set_events(TOOL, 0)

by_file = {}
for path, line in seen:
    by_file.setdefault(path, []).append(line)
json.dump({"lines": {path: sorted(lines) for path, lines in sorted(by_file.items())},
           "dialects": sorted(used)}, open(sys.argv[1], "w"))
print(f"{len(seen)} lines in {len(by_file)} files; dialects used: {sorted(used)}", file=sys.stderr)
