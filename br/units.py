"""Map a source line to the smallest piece of code that runs as one unit."""
import ast

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
BODIES = ("body", "orelse", "finalbody", "handlers", "cases")


def _start(node):
    start = node.lineno
    for dec in getattr(node, "decorator_list", ()):
        start = min(start, dec.lineno)
    return start


def _header_end(node):
    first = None
    for name in BODIES:
        for child in getattr(node, name, ()) or ():
            line = _start(child) if hasattr(child, "lineno") else None
            if line is not None and (first is None or line < first):
                first = line
    return node.end_lineno if first is None else max(_start(node), first - 1)


def locate(tree, line):
    """Return (kind, first_line, last_line, class_chain, names) for an old-file line.

    names are the plain names a class- or module-level assignment binds.

    kind is "func" when the line runs each time a function or lambda is called,
    "import" when it runs once as its module or class body is executed, and
    None when the line is in no statement (blank, comment between statements).
    """
    kind = "import"
    node = tree
    unit = None
    classes = []
    statement = None
    while True:
        found = None
        for child in ast.iter_child_nodes(node):
            if hasattr(child, "lineno") and _start(child) <= line <= child.end_lineno:
                found = child
                break
        if found is None:
            break
        node = found
        if isinstance(node, ast.stmt):
            compound = any(getattr(node, name, None) for name in BODIES)
            if compound:
                end = _header_end(node)
                if line <= end:
                    unit = (kind, _start(node), end)
                    statement = node
                    break
                if isinstance(node, FUNCS):
                    kind = "func"
                elif isinstance(node, ast.ClassDef):
                    classes.append(node.name)
                unit = None
            else:
                unit = (kind, node.lineno, node.end_lineno)
                statement = node
        elif isinstance(node, ast.Lambda):
            kind = "func"
            unit = (kind, node.lineno, node.end_lineno)
    if unit is None:
        return None, line, line, classes, []
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) \
            and isinstance(statement.value.value, str):
        # A bare string: a docstring. It is only reachable through __doc__, which
        # nothing in the pinned commit reads.
        return "doc", unit[1], unit[2], classes, []
    names = []
    if unit[0] == "import" and isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    names.append(sub.id)
    return unit[0], unit[1], unit[2], classes, names


def statement_at(tree, line):
    """The innermost statement whose own lines (header, for a compound one) hold `line`."""
    node = tree
    found_statement = None
    while True:
        found = None
        for child in ast.iter_child_nodes(node):
            if hasattr(child, "lineno") and _start(child) <= line <= child.end_lineno:
                found = child
                break
        if found is None:
            break
        node = found
        if isinstance(node, ast.stmt):
            found_statement = node
            if any(getattr(node, name, None) for name in BODIES) and line <= _header_end(node):
                return node
    if found_statement is not None and any(getattr(found_statement, name, None) for name in BODIES):
        return None  # inside a block but on none of its statements: blank or comment
    return found_statement


def signature(node):
    """Text that is equal for two statements exactly when they mean the same.

    For a compound statement only its header counts: its body is other statements.
    """
    if node is None:
        return None
    if not any(getattr(node, name, None) for name in BODIES):
        return ast.dump(node)
    parts = [type(node).__name__]
    for field, value in ast.iter_fields(node):
        if field in BODIES:
            parts.append(f"{field}:{len(value)}")
        elif isinstance(value, ast.AST):
            parts.append(ast.dump(value))
        elif isinstance(value, list):
            parts.append("[" + ",".join(ast.dump(v) if isinstance(v, ast.AST) else repr(v)
                                        for v in value) + "]")
        else:
            parts.append(repr(value))
    return "|".join(parts)


def enclosing_function(tree, line):
    """(first, last) lines of the innermost def holding `line`, or None at class/module level."""
    node = tree
    best = None
    while True:
        found = None
        for child in ast.iter_child_nodes(node):
            if hasattr(child, "lineno") and _start(child) <= line <= child.end_lineno:
                found = child
                break
        if found is None:
            break
        node = found
        if isinstance(node, FUNCS + (ast.Lambda,)):
            best = (node.lineno, node.end_lineno)
    return best
