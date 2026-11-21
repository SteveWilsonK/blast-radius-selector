"""pytest plugin: comparison coverage of sqlglot at the pinned commit.

For every single-operator comparison in sqlglot's source (`a is b`, `a == b`,
`a < b`, ...) and every test, records how the two operands related each time the
comparison ran: same object or not, equal or not, and their order when they have
one. Ordinary line coverage says a test reached a comparison; this says what the
comparison saw. Each test runs alone in its own process, so everything the process
evaluates, from import to teardown, belongs to that test.

Run from the sqlglot checkout with the tools folder and the repo on PYTHONPATH:
    python -m pytest <test id> -p cond_plugin
Writes $COND_OUT/cond-$COND_NAME.json: {"file:line:col": bitmask of relations seen}.
"""
import ast
import builtins
import enum
import importlib.abc
import importlib.machinery
import importlib.util
import json
import marshal
import operator
import os
import sys

ROOT = os.getcwd().rstrip("/") + "/"
PACKAGE = ROOT + "sqlglot/"
OUT_DIR = os.environ.get("COND_OUT", "/out")
NAME = os.environ.get("COND_NAME", "main")
CACHE = os.path.join(OUT_DIR, "_code")
# The character-by-character tokenizer loop compares millions of times per test;
# following it would slow tracing far too much. Changes there use line coverage only.
SKIP = set(filter(None, os.environ.get("COND_SKIP", "sqlglot/tokenizer_core.py,sqlglot/trie.py").split(",")))

OPS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3, ast.Lt: 4, ast.LtE: 5, ast.Gt: 6, ast.GtE: 7}
_seen = {}     # "file:line:col" -> bitmask of relation codes
CHEAP = (type(None), bool, int, float, str, bytes, type)


def _tri(function, a, b):
    """0 false, 1 true, 2 when the comparison cannot be made."""
    try:
        return 1 if function(a, b) else 0
    except Exception:  # noqa: BLE001
        return 2


def _equal(a, b):
    """Would `a == b` hold? 2 when finding out would mean a deep comparison of two
    objects of the same rich type (sqlglot expressions hash their whole tree)."""
    if a is b:
        return 1
    ta, tb = type(a), type(b)
    if ta is not tb or ta in CHEAP or isinstance(a, enum.Enum):
        return _tri(operator.eq, a, b)
    return 2


def _code(same, equal, less, more):
    return same + 2 * equal + 6 * less + 18 * more   # 0..53


SAME = 1 << _code(1, 1, 2, 2)          # one object (so equal too)
APART = 1 << _code(0, 0, 2, 2)         # different objects, not equal
_same = set()    # sites that have seen SAME
_apart = set()   # sites that have seen APART


def _other(site, a, b):
    """Two different objects where `is` was asked: are they equal?"""
    equal = _equal(a, b)
    if equal == 0:
        _apart.add(site)
    else:
        _seen[site] = _seen.get(site, 0) | (1 << _code(0, equal, 2, 2))


def _is(site, a, b):
    if a is b:
        if site not in _same:
            _same.add(site)
        return True
    if b is None or a is None:
        if site not in _apart:
            _other(site, a, b)
        return False
    _other(site, a, b)
    return False


def _is_not(site, a, b):
    if a is b:
        if site not in _same:
            _same.add(site)
        return False
    if b is None or a is None:
        if site not in _apart:
            _other(site, a, b)
        return True
    _other(site, a, b)
    return True


def _eq(site, a, b):
    result = a == b
    if a is b:
        if result is True:
            if site not in _same:
                _same.add(site)
        else:
            _seen[site] = _seen.get(site, 0) | (1 << _code(1, _tri(operator.eq, a, b), 2, 2))
    elif result is False:
        if site not in _apart:
            _apart.add(site)
    else:
        equal = 1 if result is True else _tri(operator.eq, a, b)
        _seen[site] = _seen.get(site, 0) | (1 << _code(0, equal, 2, 2))
    return result


def _ne(site, a, b):
    result = a != b
    if a is b:
        if result is False:
            if site not in _same:
                _same.add(site)
        else:
            _seen[site] = _seen.get(site, 0) | (1 << _code(1, _tri(operator.eq, a, b), 2, 2))
    elif result is True:
        if site not in _apart:
            _apart.add(site)
    else:
        equal = 0 if result is True else _tri(operator.eq, a, b)
        _seen[site] = _seen.get(site, 0) | (1 << _code(0, equal, 2, 2))
    return result


def _order(function):
    def ordered(site, a, b):
        result = function(a, b)   # exactly what the source expression computes
        code = _code(1 if a is b else 0, _tri(operator.eq, a, b),
                     _tri(operator.lt, a, b), _tri(operator.gt, a, b))
        _seen[site] = _seen.get(site, 0) | (1 << code)
        return result

    return ordered


HOOKS = {ast.Is: "__br_is__", ast.IsNot: "__br_is_not__", ast.Eq: "__br_eq__", ast.NotEq: "__br_ne__",
         ast.Lt: "__br_lt__", ast.LtE: "__br_le__", ast.Gt: "__br_gt__", ast.GtE: "__br_ge__"}
builtins.__br_is__ = _is
builtins.__br_is_not__ = _is_not
builtins.__br_eq__ = _eq
builtins.__br_ne__ = _ne
builtins.__br_lt__ = _order(operator.lt)
builtins.__br_le__ = _order(operator.le)
builtins.__br_gt__ = _order(operator.gt)
builtins.__br_ge__ = _order(operator.ge)


# ---- and / or ------------------------------------------------------------
# For every two-operand `x and y` / `x or y` inside a function: which truth pairs
# each test saw. When the second operand is skipped (short-circuit) and is free of
# side effects, it is also evaluated on the side, so its truth is known too.
# Codes: 0 TT, 1 TF, 2 FT, 3 FF (both known); 4 F? and 5 T? (second unknown).
_bools = {}   # "bool:file:line:col" -> bitmask of codes

PURE_CALLS = {"isinstance", "len", "bool", "type", "hasattr", "callable", "getattr"}
PURE_METHODS = {"get", "startswith", "endswith", "is_type", "isupper", "islower", "isdigit",
                "upper", "lower", "strip", "find_ancestor"}


def _pure(node):
    """Can this expression be evaluated an extra time without changing anything?"""
    if isinstance(node, (ast.Name, ast.Constant)):
        return True
    if isinstance(node, ast.Attribute):
        return _pure(node.value)
    if isinstance(node, ast.Subscript):
        return _pure(node.value) and _pure(node.slice)
    if isinstance(node, ast.Compare):
        return _pure(node.left) and all(_pure(c) for c in node.comparators)
    if isinstance(node, ast.UnaryOp):
        return _pure(node.operand)
    if isinstance(node, ast.BoolOp):
        return all(_pure(v) for v in node.values)
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_pure(e) for e in node.elts)
    if isinstance(node, ast.Call):
        if node.keywords and not all(_pure(k.value) for k in node.keywords):
            return False
        if not all(_pure(a) for a in node.args):
            return False
        if isinstance(node.func, ast.Name):
            return node.func.id in PURE_CALLS
        if isinstance(node.func, ast.Attribute):
            return node.func.attr in PURE_METHODS and _pure(node.func.value)
    return False


def _note_bool(site, code):
    _bools[site] = _bools.get(site, 0) | (1 << code)


def _side(thunk):
    """Truth of a skipped operand, evaluated on the side: 1, 0 or None."""
    global _quiet_cond
    _quiet_cond += 1
    try:
        return 1 if thunk() else 0
    except BaseException:  # noqa: BLE001
        return None
    finally:
        _quiet_cond -= 1


def _and(site, x, thunk, pure):
    if x:
        y = thunk()
        _note_bool(site, 0 if y else 1)
        return y
    seen = _side(thunk) if pure else None
    _note_bool(site, 4 if seen is None else (2 if seen else 3))
    return x


def _or(site, x, thunk, pure):
    if not x:
        y = thunk()
        _note_bool(site, 2 if y else 3)
        return y
    seen = _side(thunk) if pure else None
    _note_bool(site, 5 if seen is None else (0 if seen else 1))
    return x


_quiet_cond = 0
builtins.__br_and__ = _and
builtins.__br_or__ = _or


def _has(node, kinds):
    return any(isinstance(n, kinds) for n in ast.walk(node))


# ---- Enum values ---------------------------------------------------------
# Which tests use the value of which member of a sqlglot Enum: reading `.value`, or
# looking a member up by value (`Cls(value)`, hits and misses alike). Comparing,
# hashing or printing a plain Enum member never uses its value, so a change to one
# member's value reaches only these tests (and whatever sqlglot built from the
# value while loading, which the state comparison sees).
# Sites: "enumval:module:Class|MEMBER" and "enumv2m:module:Class|<canon value>".
_enum_seen = set()


def _install_enum_hooks():
    from br.state import canon_key

    value_property = enum.Enum.__dict__["value"]
    plain_get = value_property.fget

    def value(self):
        cls = type(self)
        if cls.__module__.startswith("sqlglot."):
            site = f"enumval:{cls.__module__}:{cls.__qualname__}|{self._name_}"
            if site not in _enum_seen:
                _enum_seen.add(site)
                _bools[site] = 1
        return plain_get(self)

    hooked = enum.property(value)
    hooked.__set_name__(enum.Enum, "value")
    type.__setattr__(enum.Enum, "value", hooked)
    original_call = enum.EnumType.__call__

    def call(cls, value, *args, **kwargs):
        if not args and not kwargs and cls.__module__.startswith("sqlglot."):
            name = canon_key(value)
            site = f"enumv2m:{cls.__module__}:{cls.__qualname__}|{name if name is not None else '*'}"
            if site not in _enum_seen:
                _enum_seen.add(site)
                _bools[site] = 1
        return original_call(cls, value, *args, **kwargs)

    enum.EnumType.__call__ = call


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel_path):
        self.rel_path = rel_path
        self.depth = 0   # functions we are inside

    def _function(self, node):
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _function

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if not self.depth or len(node.values) != 2:
            return node
        x, y = node.values
        if _has(y, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await)) or any(
                isinstance(n, ast.Name) and n.id == "super" for n in ast.walk(y)):
            return node
        site = f"bool:{self.rel_path}:{node.lineno}:{node.col_offset}"
        hook = "__br_and__" if isinstance(node.op, ast.And) else "__br_or__"
        thunk = ast.Lambda(args=ast.arguments(posonlyargs=[], args=[], vararg=None, kwonlyargs=[],
                                              kw_defaults=[], kwarg=None, defaults=[]), body=y)
        call = ast.Call(func=ast.Name(id=hook, ctx=ast.Load()),
                        args=[ast.Constant(site), x, thunk, ast.Constant(_pure(y))], keywords=[])
        return ast.copy_location(call, node)

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        site = f"{self.rel_path}:{node.lineno}:{node.col_offset}"
        call = ast.Call(
            func=ast.Name(id=HOOKS[type(node.ops[0])], ctx=ast.Load()),
            args=[ast.Constant(site), node.left, node.comparators[0]],
            keywords=[])
        return ast.copy_location(call, node)


class _Loader(importlib.abc.SourceLoader):
    def __init__(self, path):
        self.path = path

    def get_filename(self, fullname):
        return self.path

    def get_data(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def get_code(self, fullname):
        cached = os.path.join(CACHE, self.path[len(ROOT):].replace("/", "__") + ".marshal")
        try:
            with open(cached, "rb") as handle:
                return marshal.load(handle)
        except (OSError, EOFError, ValueError):
            pass
        source = self.get_data(self.path)
        tree = ast.parse(source, self.path)
        tree = _Rewrite(self.path[len(ROOT):]).visit(tree)
        ast.fix_missing_locations(tree)
        code = compile(tree, self.path, "exec", dont_inherit=True)
        try:
            os.makedirs(CACHE, exist_ok=True)
            temporary = f"{cached}.{os.getpid()}"
            with open(temporary, "wb") as handle:
                marshal.dump(code, handle)
            os.replace(temporary, cached)
        except OSError:
            pass
        return code


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or not spec.origin.startswith(PACKAGE) \
                or not spec.origin.endswith(".py") or spec.origin[len(ROOT):] in SKIP:
            return spec
        spec.loader = _Loader(spec.origin)
        return spec


sys.meta_path.insert(0, _Finder())

sys.path.insert(0, os.environ.get("BR_APP", "/app"))
from br import inline_pool  # noqa: E402

_install_enum_hooks()

inline_pool.install(True)   # pool work runs here, pickled as in the real pool


def pytest_sessionfinish(session, exitstatus):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"cond-{NAME}.json"), "w") as handle:
        merged = dict(_seen)
        merged.update(_bools)
        for site in _same:
            merged[site] = merged.get(site, 0) | SAME
        for site in _apart:
            merged[site] = merged.get(site, 0) | APART
        json.dump(merged, handle)
