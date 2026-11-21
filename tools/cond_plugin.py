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


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel_path):
        self.rel_path = rel_path

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

inline_pool.install(True)   # pool work runs here, pickled as in the real pool


def pytest_sessionfinish(session, exitstatus):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"cond-{NAME}.json"), "w") as handle:
        merged = dict(_seen)
        for site in _same:
            merged[site] = merged.get(site, 0) | SAME
        for site in _apart:
            merged[site] = merged.get(site, 0) | APART
        json.dump(merged, handle)
