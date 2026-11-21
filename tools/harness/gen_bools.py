"""Development harness: patches that swap `and`/`or` in one two-operand boolean test.

    python3 tools/harness/gen_bools.py <sqlglot checkout> <out_dir> [count] [seed]
Writes <out_dir>/commits/bNNN.patch and tests.json is copied by hand. Sites are picked
at random among boolean tests inside functions, the shape of the sample's code changes.
"""
import ast
import difflib
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from br import relations  # noqa: E402

root, out = sys.argv[1], sys.argv[2]
count = int(sys.argv[3]) if len(sys.argv) > 3 else 80
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 7)
skip = {"sqlglot/tokenizer_core.py", "sqlglot/trie.py"}
sites = []
for folder, _, names in os.walk(os.path.join(root, "sqlglot")):
    for name in names:
        if not name.endswith(".py"):
            continue
        path = os.path.relpath(os.path.join(folder, name), root)
        if path in skip:
            continue
        text = open(os.path.join(root, path), encoding="utf-8").read()
        tree = ast.parse(text)
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not isinstance(node, ast.BoolOp) or len(node.values) != 2:
                continue
            a, b = node.values
            if a.end_lineno != b.lineno:
                continue
            p = parents.get(node)
            inside = False
            while p is not None:
                if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    inside = True
                    break
                p = parents.get(p)
            if inside and relations._bool_context(node, parents):
                sites.append((path, node.lineno, a.end_col_offset, b.col_offset,
                              "and" if isinstance(node.op, ast.And) else "or"))
rng.shuffle(sites)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
made = 0
for path, line, start, end, op in sites:
    lines = open(os.path.join(root, path), encoding="utf-8").read().splitlines(keepends=True)
    text = lines[line - 1]
    between = text[start:end]
    if between.strip() != op:
        continue
    new = text[:start] + between.replace(op, "or" if op == "and" else "and") + text[end:]
    changed = lines[:line - 1] + [new] + lines[line:]
    diff = "".join(difflib.unified_diff(lines, changed, f"a/{path}", f"b/{path}"))
    with open(os.path.join(out, "commits", f"b{made:03d}.patch"), "w") as handle:
        handle.write(f"diff --git a/{path} b/{path}\n" + diff)
    made += 1
    if made >= count:
        break
print(f"{len(sites)} sites, {made} patches", file=sys.stderr)
