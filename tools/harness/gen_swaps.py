"""Development harness: patches that swap one comparison operator, on lines sqlglot
also runs while it loads (the hardest case for comparison coverage).

    python3 tools/harness/gen_swaps.py <sqlglot checkout> <out_dir> [count] [seed]
"""
import ast
import difflib
import json
import os
import random
import sys

root, out = sys.argv[1], sys.argv[2]
count = int(sys.argv[3]) if len(sys.argv) > 3 else 80
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 5)
here = os.path.dirname(os.path.abspath(__file__))
loaded = json.load(open(os.path.join(here, "..", "..", "maps", "self_import.json")))["lines"]
SWAP = {ast.Lt: "<=", ast.LtE: "<", ast.Gt: ">=", ast.GtE: ">", ast.Eq: "!=", ast.NotEq: "==",
        ast.Is: "==", ast.IsNot: "!="}
TEXT = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!=",
        ast.Is: "is", ast.IsNot: "is not"}
sites = []
for path, lines in loaded.items():
    if not path.startswith("sqlglot/"):
        continue
    lines = set(lines)
    text = open(os.path.join(root, path), encoding="utf-8").read()
    tree = ast.parse(text)
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in SWAP \
                    and node.lineno in lines and node.left.end_lineno == node.comparators[0].lineno:
                sites.append((path, node.lineno, node.left.end_col_offset,
                              node.comparators[0].col_offset, type(node.ops[0])))
rng.shuffle(sites)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
made = 0
for path, line, start, end, op in sites:
    lines = open(os.path.join(root, path), encoding="utf-8").read().splitlines(keepends=True)
    text = lines[line - 1]
    between = text[start:end]
    if between.strip() != TEXT[op]:
        continue
    new = text[:start] + between.replace(TEXT[op], SWAP[op], 1) + text[end:]
    changed = lines[:line - 1] + [new] + lines[line:]
    diff = "".join(difflib.unified_diff(lines, changed, f"a/{path}", f"b/{path}"))
    with open(os.path.join(out, "commits", f"s{made:03d}.patch"), "w") as handle:
        handle.write(f"diff --git a/{path} b/{path}\n" + diff)
    made += 1
    if made >= count:
        break
print(f"{len(sites)} load-time comparison sites, {made} patches", file=sys.stderr)
