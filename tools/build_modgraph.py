"""python tools/build_modgraph.py <sqlglot checkout> <out.json>

For every sqlglot module, the sqlglot modules it imports while it loads (imports at
module or class level, not inside functions). The state comparison uses the reverse of
this graph: a patched module can only change values in itself and in modules that import
it, directly or through others.
"""
import ast
import json
import os
import sys


def module_name(root, path):
    rel = os.path.relpath(path, root)[:-3].replace(os.sep, ".")
    return rel[: -len(".__init__")] if rel.endswith(".__init__") else rel


def main(root, out):
    files = {}
    for base, dirs, names in os.walk(os.path.join(root, "sqlglot")):
        for n in names:
            if n.endswith(".py"):
                path = os.path.join(base, n)
                files[module_name(root, path)] = path
    graph = {}
    for name, path in files.items():
        is_pkg = path.endswith("__init__.py")
        package = name if is_pkg else name.rsplit(".", 1)[0]
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            graph[name] = sorted(files)       # unknown: depends on everything
            continue
        deps = set()

        def visit(node):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        parts = alias.name.split(".")
                        for i in range(1, len(parts) + 1):
                            deps.add(".".join(parts[:i]))
                elif isinstance(child, ast.ImportFrom):
                    if child.level:
                        base_parts = package.split(".")
                        if child.level > 1:
                            base_parts = base_parts[: len(base_parts) - (child.level - 1)]
                        mod = ".".join(base_parts + ([child.module] if child.module else []))
                    else:
                        mod = child.module or ""
                    parts = mod.split(".")
                    for i in range(1, len(parts) + 1):
                        deps.add(".".join(parts[:i]))
                    for alias in child.names:
                        deps.add(mod + "." + alias.name)
                visit(child)

        visit(tree)
        graph[name] = sorted(d for d in deps if d in files and d != name)
    with open(out, "w") as handle:
        json.dump(graph, handle)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
