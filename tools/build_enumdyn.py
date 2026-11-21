"""Where sqlglot's Enum classes are used other than by naming one member.

Usage: build_enumdyn.py sqlglot_checkout out_file   (run while the image is built)

A change to one Enum member's value reaches code that names that member (the refs
index has those reads) and code that reaches members without naming them: calling
the class to look a value up, subscripting it, iterating it, passing the class
around. This lists, per Enum class key ("module:Qualname"), the code units that use
the class (or a name bound to it) in one of those ways: (file, kind, first, last).
Annotations, isinstance checks and member access are left out.
"""
import ast
import os
import pickle
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from br import units  # noqa: E402

ENUM_BASES = {"Enum", "AutoName", "IntEnum", "IntFlag", "Flag", "StrEnum"}

root, out_file = sys.argv[1], sys.argv[2]
files = []
for top in ("sqlglot", "tests"):
    for folder, _, names in os.walk(os.path.join(root, top)):
        for name in names:
            if name.endswith(".py"):
                files.append(os.path.relpath(os.path.join(folder, name), root))
files.sort()

trees = {}
for path in files:
    try:
        trees[path] = ast.parse(open(os.path.join(root, path), encoding="utf-8").read())
    except SyntaxError:
        pass


def module_of(path):
    name = path[:-3].replace("/", ".")
    return name[:-9] if name.endswith(".__init__") else name


def base_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


# Enum classes, found by their bases (repeated so subclasses of sqlglot's own enum
# bases such as AutoName are found too).
enums = {}       # class key -> class name
names_known = set(ENUM_BASES)
changed = True
while changed:
    changed = False
    for path, tree in trees.items():
        if not path.startswith("sqlglot/"):
            continue

        def walk(body, prefix):
            global changed
            for node in body:
                if isinstance(node, ast.ClassDef):
                    qual = prefix + node.name
                    key = f"{module_of(path)}:{qual}"
                    if key not in enums and any(base_name(b) in names_known for b in node.bases):
                        enums[key] = node.name
                        names_known.add(node.name)
                        changed = True
                    walk(node.body, qual + ".")

        walk(tree.body, "")

# Names that stand for an Enum class: its own name and any name assigned from it.
aliases = {key: {name} for key, name in enums.items()}
for path, tree in trees.items():
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name):
            source = base_name(value)
            for key, names in aliases.items():
                if source in names:
                    names.add(target.id)

all_names = {}
for key, names in aliases.items():
    for name in names:
        all_names.setdefault(name, set()).add(key)


def annotation_lines(tree):
    """Line spans of annotations, which never use the class at run time."""
    spans = []
    for node in ast.walk(tree):
        parts = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.append(node.returns)
            args = node.args
            for arg in args.args + args.posonlyargs + args.kwonlyargs + [args.vararg, args.kwarg]:
                if arg is not None:
                    parts.append(arg.annotation)
        elif isinstance(node, ast.AnnAssign):
            parts.append(node.annotation)
        for part in parts:
            if part is not None:
                spans.append((part.lineno, part.col_offset, part.end_lineno, part.end_col_offset))
    return spans


def inside(spans, node):
    for line, col, end_line, end_col in spans:
        if (line, col) <= (node.lineno, node.col_offset) and \
                (node.end_lineno, node.end_col_offset) <= (end_line, end_col):
            return True
    return False


dynamic = {key: [] for key in enums}
for path, tree in trees.items():
    spans = annotation_lines(tree)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            name = node.attr
        else:
            continue
        keys = all_names.get(name)
        if not keys:
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Attribute) and parent.value is node:
            continue   # naming a member (or an attribute): the refs index covers it
        if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is node:
            continue   # an alias: its own uses are followed under the alias name
        if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and \
                parent.func.id in ("isinstance", "issubclass") and node is not parent.func:
            continue
        if isinstance(parent, ast.Tuple) and isinstance(parents.get(parent), ast.Call) and \
                isinstance(parents[parent].func, ast.Name) and \
                parents[parent].func.id in ("isinstance", "issubclass"):
            continue
        if inside(spans, node):
            continue
        kind, first, last, _, _ = units.locate(tree, node.lineno)
        if kind is None:
            continue
        for key in keys:
            entry = (path, kind, first, last)
            if (key, entry) not in seen:
                seen.add((key, entry))
                dynamic[key].append(entry)

blob = zlib.compress(pickle.dumps({"enums": sorted(enums), "dynamic": dynamic}, protocol=5), 9)
open(out_file, "wb").write(blob)
print(f"{len(enums)} enum classes, {sum(len(v) for v in dynamic.values())} dynamic uses",
      file=sys.stderr)
