"""Development harness: patches that change the value of one Enum member.

    python3 tools/harness/gen_enums.py <sqlglot checkout> <out_dir> [per_class] [seed]
A string value gets a letter appended, a number is increased by one, and `auto()` is
replaced by a string that differs from the member's name.
"""
import ast
import difflib
import os
import random
import sys

root, out = sys.argv[1], sys.argv[2]
per_class = int(sys.argv[3]) if len(sys.argv) > 3 else 12
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 3)
ENUM_BASES = {"Enum", "AutoName", "IntEnum", "IntFlag", "Flag", "StrEnum"}
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
made = 0
kinds = {}
for folder, _, names in sorted(os.walk(os.path.join(root, "sqlglot"))):
    for name in sorted(names):
        if not name.endswith(".py"):
            continue
        path = os.path.relpath(os.path.join(folder, name), root)
        text = open(os.path.join(root, path), encoding="utf-8").read()
        lines = text.splitlines(keepends=True)
        for cls in ast.walk(ast.parse(text)):
            if not isinstance(cls, ast.ClassDef):
                continue
            bases = {getattr(b, "id", getattr(b, "attr", None)) for b in cls.bases}
            if not bases & ENUM_BASES:
                continue
            members = []
            for node in cls.body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                        isinstance(node.targets[0], ast.Name) and node.lineno == node.end_lineno:
                    value = node.value
                    target = node.targets[0].id
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        new = repr(value.value + "x")
                    elif isinstance(value, ast.Constant) and type(value.value) is int:
                        new = repr(value.value + 1)
                    elif isinstance(value, ast.Call) and getattr(value.func, "id", "") == "auto":
                        new = repr(target + "_X")
                    else:
                        continue
                    members.append((node.lineno, value.col_offset, value.end_col_offset, new, target))
            rng.shuffle(members)
            for line, start, end, new, target in members[:per_class]:
                old = lines[line - 1]
                changed = lines[:line - 1] + [old[:start] + new + old[end:]] + lines[line:]
                diff = "".join(difflib.unified_diff(lines, changed, f"a/{path}", f"b/{path}"))
                commit = f"e{made:03d}"
                with open(os.path.join(out, "commits", commit + ".patch"), "w") as handle:
                    handle.write(f"diff --git a/{path} b/{path}\n" + diff)
                kinds[commit] = f"{cls.name}.{target}"
                made += 1
import json
json.dump(kinds, open(os.path.join(out, "kinds.json"), "w"), indent=0)
print(f"{made} patches", file=sys.stderr)
