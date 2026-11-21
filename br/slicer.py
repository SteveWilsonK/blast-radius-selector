"""Run only the statements of a test that can meet a change.

Most sqlglot tests are a long run of independent statements (one check after
another). The map knows, per top-level statement of a test's body, what that
statement reached at the pinned commit. A statement that reached nothing the patch
touches behaves exactly as it did before, so it can be left out, provided nothing
that is kept depends on it. This module decides which statements must stay and
builds the shortened test function.

Anything not clearly safe keeps the whole test: `plan` returns None.
"""
import ast

# Calls on the test object whose only effect is to check something.
CHECKS = ("assert", "validate", "check", "fail")
# Attributes of the test object a statement may set without touching shared state.
OWN_SETTINGS = ("maxDiff", "longMessage")


class _Unsafe(Exception):
    pass


def _root(node):
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call, ast.Starred)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node


def _is_check_call(call, me):
    """`self.assertX(...)` / `self.validate_x(...)`: a check made by the test object."""
    func = call.func
    return (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id == me and func.attr.startswith(CHECKS))


class Body:
    """What each top-level statement of one test function reads, binds and risks."""

    def __init__(self, function):
        self.function = function
        self.statements = function.body
        arguments = function.args
        names = [a.arg for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs]
        if arguments.vararg:
            names.append(arguments.vararg.arg)
        if arguments.kwarg:
            names.append(arguments.kwarg.arg)
        self.me = names[0] if names else None
        local = set(names[1:])
        for node in ast.walk(function):
            if isinstance(node, ast.Name):
                if node.id in ("super", "__class__"):
                    raise _Unsafe("super")
                if isinstance(node.ctx, (ast.Store, ast.Del)):
                    local.add(node.id)
            elif isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await, ast.Global, ast.Nonlocal)):
                raise _Unsafe("flow")
            elif isinstance(node, ast.Attribute):
                if node.attr.startswith("__") and not node.attr.endswith("__"):
                    raise _Unsafe("private name")
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node is not function:
                local.add(node.name)
            elif isinstance(node, ast.alias):
                local.add((node.asname or node.name).split(".")[0])
            elif isinstance(node, ast.ExceptHandler) and node.name:
                local.add(node.name)
        local.discard(self.me)
        self.local = local
        self.loads, self.stores, self.fixed, self.kills = [], [], [], []
        for statement in self.statements:
            loads, stores = set(), set()
            for node in ast.walk(statement):
                if isinstance(node, ast.Name) and node.id in local:
                    (loads if isinstance(node.ctx, ast.Load) else stores).add(node.id)
                elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    stores.add(node.name)
                elif isinstance(node, ast.alias):
                    stores.add((node.asname or node.name).split(".")[0])
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    stores.add(node.name)
                elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                    loads.add(node.target.id)  # `x += 1` reads x as well
            self.loads.append(loads)
            self.stores.append(stores)
            self.fixed.append(self._fixed(statement))
            self.kills.append(self._kills(statement))

    def _kills(self, statement):
        """Names this statement rebinds for certain, to a value of its own."""
        targets = None
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets = [statement.target]
        if targets is None:
            return frozenset()
        names = set()
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)) and all(
                    isinstance(e, ast.Name) for e in target.elts):
                names.update(e.id for e in target.elts)
        return frozenset(names)

    def _fixed(self, statement, checked=False):
        """True when the statement must always stay: it can end the test early or
        change something outside the test's own local names. `checked` is set inside
        a block the test object watches (`with self.assertRaises(...)` and the like)."""
        if isinstance(statement, (ast.Return, ast.ClassDef, ast.Global, ast.Nonlocal)):
            return True
        if isinstance(statement, ast.Raise):
            return not checked
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        if isinstance(statement, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            if isinstance(statement, (ast.Assign, ast.Delete)):
                targets = statement.targets
            else:
                targets = [statement.target]
            for target in targets:
                for part in (target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]):
                    if isinstance(part, ast.Starred):
                        part = part.value
                    if isinstance(part, ast.Name):
                        continue
                    root = _root(part)
                    if isinstance(root, ast.Name) and root.id in self.local:
                        continue  # changes a local object: followed through its name
                    if (isinstance(part, ast.Attribute) and isinstance(part.value, ast.Name)
                            and part.value.id == self.me and part.attr in OWN_SETTINGS):
                        continue
                    return True
            return False
        if isinstance(statement, ast.Expr):
            call = statement.value
            if not isinstance(call, ast.Call):
                return False
            # A call whose result is thrown away is there for its effect. Checks made
            # by the test object, calls on local objects (followed through their
            # names) and calls a watched block expects to fail leave the rest alone.
            if checked or _is_check_call(call, self.me):
                return False
            root = _root(call.func)
            return not (isinstance(root, ast.Name) and root.id in self.local)
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            inner = checked or any(
                isinstance(item.context_expr, ast.Call) and _is_check_call(item.context_expr, self.me)
                for item in statement.items)
            return any(self._fixed(child, inner) for child in statement.body)
        children = []
        for field in ("body", "orelse", "finalbody"):
            children.extend(getattr(statement, field, None) or ())
        for handler in getattr(statement, "handlers", None) or ():
            children.extend(handler.body)
        for case in getattr(statement, "cases", None) or ():
            children.extend(case.body)
        return any(self._fixed(child, checked) for child in children)

    def export(self):
        """Everything the runner needs to shorten this test without parsing it again:
        the lines of each statement and how the statements depend on each other."""
        function = self.function
        first = min([function.lineno] + [d.lineno for d in function.decorator_list])
        spans = [(s.lineno, s.end_lineno) for s in self.statements]
        if not spans or spans[0][0] <= function.lineno:
            return None
        for (_, end), (start, _) in zip(spans, spans[1:]):
            if start <= end:
                return None  # two statements share a line
        return {"head": (first, spans[0][0] - 1), "spans": spans,
                "indent": self.statements[0].col_offset,
                "loads": [sorted(x) for x in self.loads],
                "stores": [sorted(x) for x in self.stores],
                "kills": [sorted(x) for x in self.kills],
                "fixed": [bool(x) for x in self.fixed]}


def keep(plan, needed, suffix=True):
    """Indexes of the statements to run so that the shortened test fails whenever the
    full one would.

    `needed` are the statements that reach the change. What they produce can differ
    under the patch, so every later statement that uses a name they touched is
    affected too, and so on down the test. With `suffix`, everything after the first
    affected statement stays: state the change leaves behind in sqlglot itself (a
    cache filled on first use) is then seen by all that follows. Kept as well: the
    fixed statements, and whatever any kept statement depends on.
    """
    loads, stores, kills, fixed = plan["loads"], plan["stores"], plan["kills"], plan["fixed"]
    count = len(fixed)
    affected = set(i for i in needed if 0 <= i < count)
    tainted = set()
    for j in range(count):
        touched = set(loads[j]) | set(stores[j])
        if j in affected:
            tainted |= touched
        elif tainted and not tainted.isdisjoint(loads[j]):
            affected.add(j)
            tainted |= touched
        else:
            tainted.difference_update(kills[j])
    if suffix and affected:
        affected.update(range(min(affected), count))
    kept = set(affected)
    kept.update(i for i in range(count) if fixed[i])
    work = list(kept)
    while work:
        index = work.pop()
        for name in loads[index]:
            # Back to the statement that last gave the name a fresh value; from there
            # on, every statement touching the name, or anything that came to share
            # an object with it, may shape what `index` sees.
            start = -1
            for j in range(index - 1, -1, -1):
                if name in kills[j]:
                    start = j
                    break
            linked = {name}
            for j in range(max(start, 0), index):
                touched = set(loads[j]) | set(stores[j])
                if j == start or (touched & linked):
                    linked |= touched
                    if j not in kept:
                        kept.add(j)
                        work.append(j)
    return kept


def analyse(tree, class_name, method):
    """The slicing plan of one test method, or None when it must run whole."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method:
                    try:
                        return Body(item).export()
                    except _Unsafe:
                        return None
    return None


def build(plan, kept, lines, class_name, method, filename, namespace):
    """The test function with only the kept statements, compiled in its module.

    `lines` are the lines of the test file. The function is rebuilt from its own
    source text inside a class of the same name, so decorators and private names
    work as in the original."""
    first, last = plan["head"]
    text = [f"class {class_name}:\n"]
    text.extend(lines[first - 1:last])
    count = 0
    for index, (start, end) in enumerate(plan["spans"]):
        if index in kept:
            text.extend(lines[start - 1:end])
            count += 1
    if not count:
        text.append(" " * plan["indent"] + "pass\n")
    scope = {}
    exec(compile("".join(text), filename, "exec"), namespace, scope)  # noqa: S102
    return scope[class_name].__dict__[method]
