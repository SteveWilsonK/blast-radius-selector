"""Development harness: single-token edits to class-level and module-level statements.

    python3 tools/harness/gen_class.py <sqlglot_checkout> <out_dir> [count] [seed]

Each patch changes one token inside a statement that runs when its module loads: a
flag flipped, a number moved by one, a string altered, one name swapped for a
neighbour, or one entry of a table removed. Used only to check the selector.
"""
import ast
import difflib
import json
import os
import random
import sys

root, out_dir = sys.argv[1], sys.argv[2]
count = int(sys.argv[3]) if len(sys.argv) > 3 else 200
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 11)

files = []
for folder, _, names in os.walk(os.path.join(root, "sqlglot")):
    for name in names:
        if name.endswith(".py"):
            files.append(os.path.relpath(os.path.join(folder, name), root))
files.sort()
texts = {p: open(os.path.join(root, p), encoding="utf-8").read() for p in files}
trees = {p: ast.parse(t) for p, t in texts.items()}


def statements(tree):
    """(statement, in_class) for every assignment that runs at load time."""
    found = []

    def visit(body, in_class):
        for node in body:
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                found.append((node, in_class))
            elif isinstance(node, ast.ClassDef):
                visit(node.body, True)
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For)):
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(node, field, []) or [], in_class)

    visit(tree.body, False)
    return found


def inside_function(statement, node):
    """Is `node` within a lambda or comprehension function of the statement?"""
    for parent in ast.walk(statement):
        if isinstance(parent, ast.Lambda) and any(child is node for child in ast.walk(parent.body)):
            return True
    return False


def edits(path, statement):
    """Possible (kind, line, col, end_col, replacement) edits within one statement."""
    lines = texts[path].splitlines()
    out = []
    names = []
    for node in ast.walk(statement):
        if not hasattr(node, "lineno") or node.lineno != getattr(node, "end_lineno", None):
            continue
        if inside_function(statement, node):
            continue
        span = (node.lineno, node.col_offset, node.end_col_offset)
        source = lines[node.lineno - 1][node.col_offset:node.end_col_offset]
        if isinstance(node, ast.Constant):
            if node.value is True:
                out.append(("flag", *span, "False"))
            elif node.value is False:
                out.append(("flag", *span, "True"))
            elif type(node.value) is int:
                out.append(("number", *span, str(node.value + rng.choice((1, -1)))))
            elif type(node.value) is str and source[:1] in "\"'" and len(source) > 2 \
                    and not source.startswith(('"""', "'''")):
                quote = source[0]
                body = source[1:-1]
                if "\\" in body or "{" in body:
                    continue
                choice = rng.choice(("append", "drop", "case"))
                if choice == "append":
                    new = body + "X"
                elif choice == "drop" and len(body) > 1:
                    new = body[:-1]
                else:
                    new = body.swapcase() if body.swapcase() != body else body + "_"
                out.append(("string", *span, quote + new + quote))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            names.append((node, source))
    # swap one dotted name for another used in the same statement
    distinct = sorted({source for _, source in names})
    if len(distinct) > 1:
        for node, source in names:
            other = rng.choice([s for s in distinct if s != source])
            if source.split(".")[0] == other.split(".")[0]:
                out.append(("swap", node.lineno, node.col_offset, node.end_col_offset, other))
    return out


def remove_entry(path, statement):
    """Delete one whole line holding one element of a multi-line collection."""
    lines = texts[path].splitlines(keepends=True)
    choices = []
    for node in ast.walk(statement):
        if isinstance(node, (ast.Dict, ast.Set, ast.Tuple, ast.List)) and node.end_lineno > node.lineno:
            items = node.keys if isinstance(node, ast.Dict) else node.elts
            values = node.values if isinstance(node, ast.Dict) else node.elts
            for key, value in zip(items, values):
                if key is None or key.lineno != value.end_lineno:
                    continue
                text = lines[key.lineno - 1]
                if text.strip().endswith(",") and text.lstrip()[:1] not in "#)]}":
                    choices.append(key.lineno)
    if not choices:
        return None
    line = rng.choice(choices)
    new = lines[:line - 1] + lines[line:]
    return "".join(new)


def diff(path, new):
    a = texts[path].splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    body = "".join(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=3))
    return f"diff --git a/{path} b/{path}\n" + body


os.makedirs(os.path.join(out_dir, "commits"), exist_ok=True)
pool = [(p, s, c) for p in files for s, c in statements(trees[p])]
weights = [3 if in_class else 1 for _, _, in_class in pool]
kinds = {}
seen = set()
attempts = 0
while len(kinds) < count and attempts < count * 60:
    attempts += 1
    path, statement, in_class = rng.choices(pool, weights=weights)[0]
    if rng.random() < 0.15:
        new = remove_entry(path, statement)
        kind = "remove"
    else:
        options = edits(path, statement)
        if not options:
            continue
        # even out the kinds: strings and swaps are far more numerous
        wanted = rng.choice(("flag", "number", "string", "swap"))
        options = [o for o in options if o[0] == wanted] or options
        kind, line, col, end, replacement = rng.choice(options)
        lines = texts[path].splitlines(keepends=True)
        text = lines[line - 1]
        lines[line - 1] = text[:col] + replacement + text[end:]
        new = "".join(lines)
    if new is None or new == texts[path]:
        continue
    try:
        ast.parse(new)
    except SyntaxError:
        continue
    patch = diff(path, new)
    if patch in seen:
        continue
    seen.add(patch)
    name = f"c{len(kinds):03d}"
    kinds[name] = kind + ("" if in_class else "-module")
    with open(os.path.join(out_dir, "commits", name + ".patch"), "w", encoding="utf-8") as handle:
        handle.write(patch)
json.dump(kinds, open(os.path.join(out_dir, "kinds.json"), "w"), indent=0)
import collections
print(collections.Counter(kinds.values()))
