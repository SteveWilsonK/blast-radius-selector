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
KEYS = ("lines", "blines", "plines", "files", "pyfiles", "dirs", "classes", "dialects",
        "loose_classes", "loose_dialects", "wires")


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
        fn = code.co_filename
        if fn.startswith(ROOT):
            if _in_pickle:
                # Pickling for a pool worker: kept apart, and by fixture case when one
                # is active, so a change reached only this way can still be narrowed.
                if _case is not None:
                    _case["plines"].add((fn, line))
                else:
                    _ctx["plines"].add((fn, line))
            elif not (code.co_flags & 1):
                # Module and class bodies are not CO_OPTIMIZED: they run once, at import.
                _ctx["blines"].add((fn, line))
            elif _case is not None:
                _case["lines"].add((fn, line))
            elif _pending is not None:
                _pending.add((fn, line))
            else:
                _ctx["lines"].add((fn, line))
    return DISABLE


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
        return value(*args, **kwargs)

    try:
        functools.update_wrapper(wire, value)
    except Exception:
        pass
    wire.__br_wire__ = tag
    return wire


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
    except Exception:
        pass
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
                       "wires": set()})
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
sys.addaudithook(_audit)
sys.monitoring.use_tool_id(TOOL, "blast-trace")
sys.monitoring.register_callback(TOOL, sys.monitoring.events.LINE, _on_line)
sys.monitoring.set_events(TOOL, sys.monitoring.events.LINE)

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
    for key in ("lines", "blines", "plines"):
        record[key] = _pack(ctx[key])
    for key in ("files", "pyfiles", "dirs"):
        record[key] = sorted(p[len(ROOT):] for p in ctx[key])
    for key in ("classes", "dialects", "loose_classes", "loose_dialects", "wires"):
        record[key] = sorted(ctx[key])
    record["cases"] = [
        [fixture, index, _pack(case["lines"]), sorted(case["dialects"]), sorted(case["classes"]),
         _pack(case["plines"]), sorted(case["wires"]), round(case.get("t", 0.0), 5)]
        for (fixture, index), case in ctx["cases"].items()
    ]
    if phase == "run":
        record["seconds"] = round(time.process_time() - ctx.get("t0", time.process_time()), 5)
    if phase == "import":
        record["mro"] = _mro
        record["wired"] = sorted(_wired)
    _out.write(json.dumps(record) + "\n")
    _out.flush()


def pytest_runtest_setup(item):
    global _ctx, _case, _pending
    _patch_sqlglot()
    _ctx = _new()
    _ctx["t0"] = time.process_time()
    _case = None
    _pending = None
    sys.monitoring.restart_events()


def pytest_runtest_logfinish(nodeid, location):
    global _ctx, _case
    if _ctx is not _import:
        _flush_pending()
        _case = None
        _emit(nodeid, "run", _ctx)
        _ctx = _import


def pytest_sessionfinish(session, exitstatus):
    global _alive
    _emit(None, "import", _import)
    _alive = False
