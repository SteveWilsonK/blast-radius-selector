"""Which dialects are built from which source files, at the pinned commit.

Each dialect is loaded alone in a fresh interpreter; every sqlglot source file
that loading it brings in is part of that dialect's family. Run inside the image:
    PYTHONPATH=/sqlglot python family_map.py out.json
"""
import json
import subprocess
import sys

PROBE = r"""
import json, sys
from sqlglot.dialects.dialect import Dialect
name = sys.argv[1]
instance = Dialect.get_or_raise(name or None)
cls = type(instance)
# Touch the lazily built pieces so their modules are loaded too.
for attr in ("tokenizer_class", "parser_class", "generator_class", "jsonpath_tokenizer_class"):
    getattr(cls, attr, None)
files = []
for module in list(sys.modules.values()):
    path = getattr(module, "__file__", None) or ""
    if "/sqlglot/sqlglot/" in path or path.startswith("/sqlglot/sqlglot/"):
        files.append(path[path.index("/sqlglot/sqlglot/") + len("/sqlglot/"):])
print(json.dumps([cls.__module__ + ":" + cls.__qualname__, files]))
"""

names = json.loads(subprocess.run(
    [sys.executable, "-c",
     "import json; import sqlglot.dialects as d; from sqlglot.dialects.dialect import Dialects; "
     "print(json.dumps(sorted({x.value for x in Dialects} | set(d.DIALECT_MODULE_NAMES))))"],
    capture_output=True, text=True, check=True).stdout)

families = {}
for name in names:
    result = subprocess.run([sys.executable, "-c", PROBE, name], capture_output=True, text=True)
    if result.returncode:
        print(f"skipped {name!r}: {result.stderr.strip().splitlines()[-1:]}", file=sys.stderr)
        continue
    key, files = json.loads(result.stdout.strip().splitlines()[-1])
    for path in files:
        families.setdefault(path, set()).add(key)

json.dump({path: sorted(keys) for path, keys in sorted(families.items())},
          open(sys.argv[1], "w"), indent=0)
print(f"{len(families)} files, {len(names)} dialect names", file=sys.stderr)
