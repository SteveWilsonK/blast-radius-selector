"""Index where each class-level or module-level name is read, at the pinned commit.

Usage: build_refs.py sqlglot_checkout out_file
For every name assigned in a class body or at module level, lists the places that
read it (`x.NAME`, a bare `NAME`, or "NAME" passed to getattr/hasattr), as the code
unit containing the read: (file, kind, first_line, last_line).
"""
import ast
import os
import pickle
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from br import units  # noqa: E402

root, out_file = sys.argv[1], sys.argv[2]
files = []
for top in ("sqlglot", "tests"):
    for folder, _, names in os.walk(os.path.join(root, top)):
        for name in names:
            if name.endswith(".py"):
                files.append(os.path.relpath(os.path.join(folder, name), root))
files.sort()

trees = {}
defined = set()
for path in files:
    try:
        tree = trees[path] = ast.parse(open(os.path.join(root, path), encoding="utf-8").read())
    except SyntaxError:
        continue

    def visit(body):
        for node in body:
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for sub in ast.walk(target):
                        if isinstance(sub, ast.Name):
                            defined.add(sub.id)
            elif isinstance(node, ast.ClassDef):
                visit(node.body)
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(node, field, []) or [])

    visit(tree.body)

refs = {}
for path, tree in trees.items():
    seen = set()
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            name = node.attr
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            name = node.value  # getattr(x, "NAME") and similar string lookups
        if name is None or name not in defined:
            continue
        kind, first, last, _, _ = units.locate(tree, node.lineno)
        if kind is None:
            continue
        key = (name, kind, first, last)
        if key in seen:
            continue
        seen.add(key)
        refs.setdefault(name, []).append((path, kind, first, last))

blob = zlib.compress(pickle.dumps(refs, protocol=5), 9)
open(out_file, "wb").write(blob)
print(f"{len(defined)} names, {sum(len(v) for v in refs.values())} reads, "
      f"{len(blob) / 1e6:.2f} MB", file=sys.stderr)
