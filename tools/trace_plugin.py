"""pytest plugin: per-test maps for sqlglot at the pinned commit.

For each test (run alone in its own process) it records
  * the source lines executed: at import, in functions during the test, in
    class/module bodies during the test (lazy imports), and while pickling,
  * the same lines per fixture case, for tests that walk a fixture file,
  * the files opened and directories listed,
  * the Expr classes instantiated and the dialect classes used.
Writes $TRACE_OUT/trace-$TRACE_NAME.jsonl.
"""
import json
import os
import sys
import time

import inline_pool

ROOT = os.getcwd().rstrip("/") + "/"
OUT_DIR = os.environ.get("TRACE_OUT", "/out")
NAME = os.environ.get("TRACE_NAME") or os.environ.get("PYTEST_XDIST_WORKER", "main")
TOOL = sys.monitoring.COVERAGE_ID
DISABLE = sys.monitoring.DISABLE
KEYS = ("lines", "blines", "plines", "llines", "files", "pyfiles", "dirs", "classes", "dialects",
        "loose_classes", "loose_dialects", "wires", "reads")


def _new():
    ctx = {k: set() for k in KEYS}
    ctx["cases"] = {}  # (fixture, index) -> {"lines": set, "dialects": set, "classes": set}
    return ctx


_import = _new()
_ctx = _import
_case = None       # set the active fixture case's lines go to
_pending = None    # lines since the last case boundary, not yet attributed
_in_pickle = 0
_alive = True


def _on_line(code, line):
    if _alive and sys is not None:
        if code is _test_code:
            # A line of the test's own body: note which top-level statement it belongs
            # to, and keep these events coming (never disabled).
            index = _stmt_of.get(line)
            if index is not None and index != _stmt[0]:
                _enter_statement(index)
            _ctx["lines"].add((code.co_filename, line))
            return None
        fn = code.co_filename
        if fn.startswith(ROOT):
            bit = _stmt[1]
            if _in_pickle:
                # Pickling for a pool worker: kept apart, and by fixture case when one
                # is active, so a change reached only this way can still be narrowed.
                if _case is not None:
                    _case["plines"].add((fn, line))
                else:
                    _ctx["plines"].add((fn, line))
                if bit:
                    d = _body["plines"]
                    d[(fn, line)] = d.get((fn, line), 0) | bit
            elif not (code.co_flags & 1):
                # Module and class bodies are not CO_OPTIMIZED: they run once, at import.
                _ctx["blines"].add((fn, line))
            else:
                if _loading[0]:
                    # A function running while a module loads (a metaclass, a helper
                    # called from a class body): what it does stays for the rest of
                    # the process, not just for the statement or case that loaded it.
                    _ctx["llines"].add((fn, line))
                if _case is not None:
                    _case["lines"].add((fn, line))
                elif _pending is not None:
                    _pending.add((fn, line))
                else:
                    _ctx["lines"].add((fn, line))
                if bit:
                    d = _body["lines"]
                    d[(fn, line)] = d.get((fn, line), 0) | bit
    return DISABLE


# ---- modules loading while a test runs ------------------------------------
_loading = [0]


def _watch_imports():
    import builtins
    import importlib

    real_import = builtins.__import__
    real_import_module = importlib.import_module

    def loaded(name, fromlist):
        module = sys.modules.get(name)
        if module is None:
            return False
        if fromlist:
            have = getattr(module, "__dict__", {})
            for item in fromlist:
                if item != "*" and item not in have:
                    return False
        return True

    def enter():
        _loading[0] += 1
        if _loading[0] == 1 and _ctx is not _import:
            sys.monitoring.restart_events()

    def leave():
        _loading[0] -= 1
        if _loading[0] == 0 and _ctx is not _import:
            sys.monitoring.restart_events()

    def __import__(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and loaded(name, fromlist):
            return real_import(name, globals, locals, fromlist, level)
        enter()
        try:
            return real_import(name, globals, locals, fromlist, level)
        finally:
            leave()

    def import_module(name, package=None):
        if not name.startswith(".") and name in sys.modules:
            return real_import_module(name, package)
        enter()
        try:
            return real_import_module(name, package)
        finally:
            leave()

    builtins.__import__ = __import__
    importlib.import_module = import_module


# ---- statements of the test body -----------------------------------------
# Most tests are a long run of independent statements. While the test's own function
# runs, everything recorded is also credited to the top-level statement of its body
# that is executing, so a change can be traced to the statements that reach it.

_test_code = None    # code object of the running test's function
_stmt_of = {}        # line of that function -> index of the top-level statement
_stmt = [-1, 0, 0.0]  # index of the statement running, its bit, when it started
_body = None         # per-statement tables of the running test


def _new_body(count, first):
    return {"n": count, "first": first, "lines": {}, "plines": {}, "dialects": {},
            "classes": {}, "wires": {}, "reads": {}, "time": {}}


def _enter_statement(index):
    now = time.process_time()
    if _stmt[0] >= 0:
        _body["time"][_stmt[0]] = _body["time"].get(_stmt[0], 0.0) + now - _stmt[2]
    _stmt[0] = index
    _stmt[1] = (1 << index) if index >= 0 else 0
    _stmt[2] = now
    sys.monitoring.restart_events()


def _credit(table, key):
    bit = _stmt[1]
    if bit:
        d = _body[table]
        d[key] = d.get(key, 0) | bit


_trees = {}


def _stop_body():
    global _test_code
    _test_code = None
    _stmt[0], _stmt[1] = -1, 0


def _track_body(item):
    """Find the test's function and the line span of each statement in its body."""
    global _test_code, _stmt_of, _body
    _test_code, _stmt_of, _body = None, {}, None
    _stmt[0], _stmt[1] = -1, 0
    try:
        import ast
        import inspect

        function = getattr(item, "obj", None)
        function = inspect.unwrap(getattr(function, "__func__", function))
        code = function.__code__
        path = code.co_filename
        if not path.startswith(ROOT + "tests/"):
            return
        tree = _trees.get(path)
        if tree is None:
            tree = _trees[path] = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == code.co_name:
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                if first == code.co_firstlineno:
                    for index, statement in enumerate(node.body):
                        for line in range(statement.lineno, statement.end_lineno + 1):
                            _stmt_of[line] = index
                    _body = _new_body(len(node.body), node.lineno)
                    _test_code = code
                    return
    except Exception:
        _test_code, _stmt_of, _body = None, {}, None


def _pack_body(body):
    """The per-statement tables with each distinct set of statements written once."""
    ids, sets = {}, []

    def sid(mask):
        key = ids.get(mask)
        if key is None:
            key = ids[mask] = len(sets)
            sets.append(format(mask, "x"))
        return key

    def by_file(table):
        out = {}
        for (fn, line), mask in table.items():
            out.setdefault(fn[len(ROOT):], {})[line] = sid(mask)
        return out

    return {"n": body["n"], "first": body["first"],
            "lines": by_file(body["lines"]), "plines": by_file(body["plines"]),
            "dialects": {k: sid(m) for k, m in body["dialects"].items()},
            "classes": {k: sid(m) for k, m in body["classes"].items()},
            "wires": {k: sid(m) for k, m in body["wires"].items()},
            "reads": {k: sid(m) for k, m in body["reads"].items()},
            "time": [round(body["time"].get(i, 0.0), 5) for i in range(body["n"])],
            "sets": sets}


def _on_start(code, offset):
    # A function whose body sits on its `def`/`lambda` line (one-line lambdas above
    # all) raises no LINE event when called. Its entry is recorded as its first line.
    return _on_line(code, code.co_firstlineno)


def _audit(event, args):
    if not _alive:
        return
    if event == "open":
        path = args[0]
        if isinstance(path, bytes):
            try:
                path = path.decode()
            except Exception:
                return
        if not isinstance(path, str):
            return
        if not path.startswith("/"):
            path = os.path.abspath(path)
        if not path.startswith(ROOT) or "__pycache__" in path:
            return
        if path.endswith(".py"):
            frame = sys._getframe(1)
            depth = 0
            while frame is not None and depth < 12:
                if frame.f_code.co_filename.startswith("<frozen importlib"):
                    _ctx["pyfiles"].add(path)
                    return
                frame = frame.f_back
                depth += 1
        _ctx["files"].add(path)
    elif event in ("os.listdir", "os.scandir", "glob.glob", "glob.glob/2"):
        path = args[0] if args else None
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if isinstance(path, str) or path is None:
            path = os.path.abspath(path or ".")
            if (path + "/").startswith(ROOT) and "__pycache__" not in path:
                frame = sys._getframe(1)
                depth = 0
                while frame is not None and depth < 12:
                    if frame.f_code.co_filename.startswith("<frozen importlib"):
                        return
                    frame = frame.f_back
                    depth += 1
                _ctx["dirs"].add(path + "/")


# ---- class-level tables --------------------------------------------------
# sqlglot keeps much of each dialect in class-level dicts (FUNCTIONS, TRANSFORMS, ...)
# whose values are callables. Those dicts are built once, when the module loads, so
# line coverage cannot say which test uses which entry. Each callable entry is
# therefore replaced, as its class is created, by a thin recorder that notes
# "class | table | key" when called and then calls the original. Subclass tables that
# copy an entry copy its recorder, so a call is credited to the class that defines it.

import builtins
import enum
import functools

_wired = set()
_real_build_class = builtins.__build_class__


def _canon(key):
    if isinstance(key, str):
        return key
    if isinstance(key, type):
        return key.__name__
    if isinstance(key, enum.Enum):
        return key.name
    return None


def _recorder(tag, value):
    def wire(*args, **kwargs):
        _ctx["wires"].add(tag)
        if _case is not None:
            _case["wires"].add(tag)
        _credit("wires", tag)
        return value(*args, **kwargs)

    try:
        functools.update_wrapper(wire, value)
    except Exception:
        pass
    wire.__br_wire__ = tag
    if hasattr(value, "__self__"):
        wire.__self__ = value.__self__  # sqlglot looks at the owner of a bound builder
    return wire


# ---- reads of class-level values -----------------------------------------
# A class-level value is set once, when its module loads, so line coverage cannot say
# which test uses which one. Each plain dict on a sqlglot class is replaced, as the
# class is created, by a dict that notes the key of every entry fetched from it
# ("class|NAME|key", or "class|NAME|*" when it is used as a whole). Each simple
# constant is put behind a getter that notes "class|NAME" when it is read.

_tracked = set()   # "class|NAME" of every value recorded this way
_placed = {}       # the same -> (class, NAME, recorder), to see if it is still in place
_shared = {}       # "class|NAME" -> the "class|NAME" whose recorder it shares
_SIMPLE = (bool, int, float, str, bytes, type(None), tuple, list, set, frozenset)

try:
    from br.state import canon_key as _canon_key
except Exception:  # pragma: no cover - the tracer must run inside the repo
    raise


def _note(tag):
    if _ctx is _import:
        return
    _ctx["reads"].add(tag)
    if _case is not None:
        _case["reads"].add(tag)
    bit = _stmt[1]
    if bit:
        d = _body["reads"]
        d[tag] = d.get(tag, 0) | bit


class _RecDict(dict):
    """A dict that notes how it is used: "|key" when an entry is fetched or found,
    "|!" when a key is looked for and missing, "|#" when its keys are walked or
    counted, "|*" when it is used as a whole."""

    def _br_key(self, key):
        if _ctx is _import:
            return
        names = self.__dict__.get("_br_names")
        if names is None:
            return
        try:
            tag = names.get(key)
        except TypeError:
            tag = None
        if tag is None:
            canon = _canon_key(key)
            tag = self._br_tag + "|" + (canon if canon is not None else "*")
            try:
                names[key] = tag
            except TypeError:
                pass
        _note(tag)

    def _br_all(self):
        tag = self.__dict__.get("_br_tag")
        if tag is not None:
            _note(tag + "|*")

    def _br_flag(self, mark):
        tag = self.__dict__.get("_br_tag")
        if tag is not None:
            _note(tag + mark)

    def __getitem__(self, key):
        try:
            value = dict.__getitem__(self, key)
        except KeyError:
            self._br_flag("|!")   # asked for an entry that is not there
            raise
        self._br_key(key)
        return value

    def get(self, key, default=None):
        try:
            value = dict.__getitem__(self, key)
        except KeyError:
            self._br_flag("|!")
            return default
        self._br_key(key)
        return value

    def __contains__(self, key):
        if dict.__contains__(self, key):
            self._br_key(key)
            return True
        self._br_flag("|!")
        return False

    def __iter__(self):
        # Also defined so that C code merging this dict into another goes through
        # keys() and __getitem__ instead of reading the entries directly.
        self._br_flag("|#")       # which keys there are, and how many
        return dict.__iter__(self)

    def keys(self):
        self._br_flag("|#")
        return dict.keys(self)

    def __len__(self):
        self._br_flag("|#")
        return dict.__len__(self)

    def values(self):
        self._br_all()
        return dict.values(self)

    def items(self):
        self._br_all()
        return dict.items(self)

    def copy(self):
        self._br_all()
        return dict.copy(self)

    def __eq__(self, other):
        self._br_all()
        return dict.__eq__(self, other)

    def __ne__(self, other):
        self._br_all()
        return dict.__ne__(self, other)

    __hash__ = None

    def __or__(self, other):
        self._br_all()
        return dict.__or__(dict(dict.items(self)), other)

    def __ror__(self, other):
        self._br_all()
        return dict.__or__(dict(other), dict(dict.items(self)))

    def __ior__(self, other):
        self._br_all()
        dict.update(self, other)
        return self

    def __repr__(self):
        self._br_all()
        return dict.__repr__(self)

    def __reduce_ex__(self, protocol):
        self._br_all()
        return (dict, (dict(dict.items(self)),))

    def __reduce__(self):
        return self.__reduce_ex__(2)

    def __copy__(self):
        self._br_all()
        return dict(dict.items(self))

    def __deepcopy__(self, memo):
        import copy

        self._br_all()
        return copy.deepcopy(dict(dict.items(self)), memo)

    def pop(self, *args):
        self._br_all()
        return dict.pop(self, *args)

    def popitem(self):
        self._br_all()
        return dict.popitem(self)

    def setdefault(self, key, default=None):
        self._br_all()
        return dict.setdefault(self, key, default)

    def update(self, *args, **kwargs):
        self._br_all()
        return dict.update(self, *args, **kwargs)

    def __setitem__(self, key, value):
        self._br_all()
        return dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        self._br_all()
        return dict.__delitem__(self, key)

    def clear(self):
        self._br_all()
        return dict.clear(self)


class _RecAttr:
    """Stands in for a simple class-level constant: reading it through the class or
    an instance notes the read and gives the constant."""

    __slots__ = ("value", "tag")

    def __init__(self, value, tag):
        self.value = value
        self.tag = tag

    def __get__(self, instance, owner=None):
        if _ctx is not _import:
            _note(self.tag)
        return self.value


def _record_reads(cls, owner):
    lowercase_too = cls.__module__.startswith("sqlglot.expressions")
    for attr in list(vars(cls)):
        if attr.startswith("__") or attr.startswith("_abc_"):
            continue
        value = cls.__dict__[attr]
        tag = f"{owner}|{attr}"
        kind = type(value)
        if kind is _RecDict:
            # Another class's table under a second name: its reads are noted there.
            shared = value.__dict__.get("_br_tag")
            if shared is not None and shared != tag:
                _shared[tag] = shared
        elif kind is dict:
            # Class dictionary, `value`, and the argument below: anything more is
            # another holder that would keep using the unrecorded original.
            if sys.getrefcount(value) > 3:
                continue
            proxy = _RecDict(value)
            proxy._br_tag = tag
            proxy._br_names = {}
            type.__setattr__(cls, attr, proxy)
            _tracked.add(tag)
            _placed[tag] = (cls, attr, proxy)
        elif kind in _SIMPLE or isinstance(value, enum.Enum):
            if not (lowercase_too or attr.lstrip("_")[:1].isupper()):
                continue
            getter = _RecAttr(value, tag)
            type.__setattr__(cls, attr, getter)
            _tracked.add(tag)
            _placed[tag] = (cls, attr, getter)


def _build_class(func, name, *bases, **kwargs):
    cls = _real_build_class(func, name, *bases, **kwargs)
    try:
        if cls.__module__.startswith("sqlglot."):
            owner = cls.__module__ + ":" + cls.__qualname__
            for attr, table in list(vars(cls).items()):
                if type(table) is not dict or attr.startswith("__"):
                    continue
                for key, value in list(table.items()):
                    if isinstance(value, type) or not callable(value):
                        continue
                    if getattr(value, "__br_wire__", None):
                        continue
                    canon = _canon(key)
                    if canon is None:
                        continue
                    tag = f"{owner}|{attr}|{canon}"
                    table[key] = _recorder(tag, value)
                    _wired.add(tag)
            del table
            if not (isinstance(cls, enum.EnumMeta) or issubclass(cls, tuple)):
                _record_reads(cls, owner)
    except Exception:
        import traceback

        sys.stderr.write("RECORDER " + traceback.format_exc())
    return cls


builtins.__build_class__ = _build_class


# ---- fixture cases -------------------------------------------------------

_tags = {}      # id(meta dict) -> (fixture, index); the dicts are kept alive in _keep
_keep = []


_clock = [0.0]


def _switch(case_key):
    """Start attributing lines to a fixture case (or to none)."""
    global _case, _pending
    if _ctx is _import:
        return
    now = time.process_time()
    if _case is not None:
        _case["t"] = _case.get("t", 0.0) + now - _clock[0]
    _clock[0] = now
    if case_key is None:
        _case = None
        _pending = set()
    else:
        _case = _ctx["cases"].setdefault(
            case_key, {"lines": set(), "plines": set(), "dialects": set(), "classes": set(),
                       "wires": set(), "reads": set()})
        if _pending:
            _case["lines"] |= _pending
        _pending = None
    sys.monitoring.restart_events()


def _flush_pending():
    global _pending
    if _pending:
        _ctx["lines"] |= _pending
    _pending = None


_by_text = {}   # SQL text of a fixture case -> its (fixture, index), or None when ambiguous


def _wrap_loader(original):
    def loader(filename):
        index = 0
        for item in original(filename):
            key = (filename, index)
            if isinstance(item, tuple) and item and isinstance(item[0], dict):
                _tags[id(item[0])] = key
                _keep.append(item[0])
            text = item if isinstance(item, str) else (
                item[1] if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], str)
                else None)
            if text is not None:
                _by_text[text] = key if _by_text.get(text, key) == key else None
            _switch(key)
            yield item
            index += 1
        # Exhausted: what follows belongs to the test as a whole.
        global _case
        if _ctx is not _import:
            if _case is not None:
                _case["t"] = _case.get("t", 0.0) + time.process_time() - _clock[0]
            _case = None
            sys.monitoring.restart_events()

    loader.__name__ = original.__name__
    return loader


import contextlib  # noqa: E402
import unittest  # noqa: E402

_real_subtest = unittest.TestCase.subTest


@contextlib.contextmanager
def _subtest(self, msg=unittest.case._subtest_msg_sentinel, **params):
    """A test that keeps its fixture cases in a list and walks them with subTest:
    when the sub-test names a case's SQL, its lines belong to that case."""
    key = None
    if _case is None and _ctx is not _import:
        texts = [value for value in params.values() if isinstance(value, str)]
        if isinstance(msg, str):
            texts.append(msg)
            texts.append(msg.split(", ", 1)[-1])
        for text in texts:
            key = _by_text.get(text)
            if key is not None:
                break
    if key is not None:
        _switch(key)
    try:
        with _real_subtest(self, msg, **params):
            yield
    finally:
        if key is not None:
            _switch(None)


unittest.TestCase.subTest = _subtest


def _submit_tag(args, kwargs):
    if _case is not None:
        for key, value in _ctx["cases"].items():
            if value is _case:
                return key
    for value in list(args) + list(kwargs.values()):
        tag = _tags.get(id(value))
        if tag is not None:
            return tag
    return None


class _Hooks:
    """Called by the inline pool around each piece of work."""

    @staticmethod
    def pickle_enter():
        global _in_pickle
        _in_pickle += 1
        if _in_pickle == 1:
            sys.monitoring.restart_events()

    @staticmethod
    def pickle_exit():
        global _in_pickle
        _in_pickle -= 1
        if _in_pickle == 0:
            sys.monitoring.restart_events()

    @staticmethod
    def submit_enter(args, kwargs):
        """Returns (tag, previous case) and attributes the argument-building lines."""
        previous = _case
        tag = _submit_tag(args, kwargs)
        if tag is not None and previous is None:
            _switch(tag)
        return tag, previous

    @staticmethod
    def submit_exit(state):
        tag, previous = state
        if tag is not None and previous is None:
            _switch(None)

    @staticmethod
    def result(tag):
        if tag is not None:
            _switch(tag)


inline_pool.install(_Hooks)
_watch_imports()
sys.addaudithook(_audit)
sys.monitoring.use_tool_id(TOOL, "blast-trace")
sys.monitoring.register_callback(TOOL, sys.monitoring.events.LINE, _on_line)
sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_START, _on_start)
sys.monitoring.set_events(TOOL, sys.monitoring.events.LINE | sys.monitoring.events.PY_START)

import tests.helpers as _helpers  # noqa: E402

_helpers.load_sql_fixture_pairs = _wrap_loader(_helpers.load_sql_fixture_pairs)
_helpers.load_sql_fixtures = _wrap_loader(_helpers.load_sql_fixtures)

_patched = False
_keys = {}   # class -> key
_mro = {}    # class key -> keys of its sqlglot base classes, itself included


def _class_key(cls):
    return cls.__module__ + ":" + cls.__qualname__


def _use_dialect(cls):
    key = _class_key(cls)
    _ctx["dialects"].add(key)
    if _case is not None:
        _case["dialects"].add(key)
    else:
        _ctx["loose_dialects"].add(key)
    _credit("dialects", key)


def _patch_sqlglot():
    global _patched
    if _patched:
        return
    _patched = True
    from sqlglot.dialects import dialect as dialect_module
    from sqlglot.expressions import core

    original_init = core.Expr.__init__

    def __init__(self, **args):
        cls = type(self)
        key = _keys.get(cls)
        if key is None:
            key = _keys[cls] = _class_key(cls)
            _mro[key] = [_class_key(base) for base in cls.__mro__
                         if base.__module__.startswith("sqlglot")]
        _ctx["classes"].add(key)
        if _case is not None:
            _case["classes"].add(key)
        else:
            _ctx["loose_classes"].add(key)
        _credit("classes", key)
        original_init(self, **args)

    core.Expr.__init__ = __init__

    Dialect = dialect_module.Dialect
    original_get_or_raise = Dialect.get_or_raise.__func__
    original_dialect_init = Dialect.__init__

    def get_or_raise(cls, dialect):
        result = original_get_or_raise(cls, dialect)
        _use_dialect(type(result))
        return result

    def dialect_init(self, **kwargs):
        _use_dialect(type(self))
        original_dialect_init(self, **kwargs)

    Dialect.get_or_raise = classmethod(get_or_raise)
    Dialect.__init__ = dialect_init


def _pack(pairs):
    by_file = {}
    for fn, line in pairs:
        by_file.setdefault(fn[len(ROOT):], []).append(line)
    return {f: sorted(v) for f, v in by_file.items()}


_out = None


def _emit(test, phase, ctx):
    global _out
    if _out is None:
        os.makedirs(OUT_DIR, exist_ok=True)
        _out = open(os.path.join(OUT_DIR, f"trace-{NAME}.jsonl"), "w")
    record = {"test": test, "phase": phase}
    for key in ("lines", "blines", "plines", "llines"):
        record[key] = _pack(ctx[key])
    for key in ("files", "pyfiles", "dirs"):
        record[key] = sorted(p[len(ROOT):] for p in ctx[key])
    for key in ("classes", "dialects", "loose_classes", "loose_dialects", "wires", "reads"):
        record[key] = sorted(ctx[key])
    record["cases"] = [
        [fixture, index, _pack(case["lines"]), sorted(case["dialects"]), sorted(case["classes"]),
         _pack(case["plines"]), sorted(case["wires"]), round(case.get("t", 0.0), 5),
         sorted(case["reads"])]
        for (fixture, index), case in ctx["cases"].items()
    ]
    if phase == "run":
        record["seconds"] = round(time.process_time() - ctx.get("t0", time.process_time()), 5)
    if phase == "run":
        record["failed"] = bool(_failed[0])
    if phase == "run" and _body is not None:
        if _stmt[0] >= 0:
            _enter_statement(-1)
        record["body"] = _pack_body(_body)
    if phase == "import":
        record["mro"] = _mro
        record["wired"] = sorted(_wired)
        record["tracked"] = sorted(_tracked)
        # A recorder that later code replaced no longer sees the reads of its value.
        record["untracked"] = sorted(
            tag for tag, (cls, attr, recorder) in _placed.items()
            if cls.__dict__.get(attr) is not recorder)
        record["shared"] = _shared
    _out.write(json.dumps(record) + "\n")
    _out.flush()


def pytest_runtest_setup(item):
    global _ctx, _case, _pending
    _patch_sqlglot()
    _ctx = _new()
    _ctx["t0"] = time.process_time()
    _case = None
    _pending = None
    _track_body(item)
    sys.monitoring.restart_events()


_failed = [False]


def pytest_runtest_logreport(report):
    if report.failed:
        _failed[0] = True


def pytest_runtest_logfinish(nodeid, location):
    global _ctx, _case
    if _ctx is not _import:
        _flush_pending()
        _case = None
        _emit(nodeid, "run", _ctx)
        _ctx = _import
        _stop_body()


def pytest_sessionfinish(session, exitstatus):
    global _alive
    _emit(None, "import", _import)
    _alive = False
