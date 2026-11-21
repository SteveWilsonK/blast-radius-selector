"""A fingerprint of everything sqlglot holds once all its modules are loaded.

A change to a class body or module-level statement takes effect while modules load:
it can alter the value it binds, and anything other code derives from that value
at load time (metaclasses build lookup tables, tries and defaults). Comparing this
fingerprint between the pinned and the patched code shows exactly which names end
up different, wherever they live. Two loads of the same code give the same
fingerprint: nothing in it depends on memory addresses, hash seeds or the order in
which a set happened to be walked.

Keys are "module<TAB>name" for module-level names, "module:Class|NAME" for class
attributes and "module:Class|NAME|key" for the entries of a class-level dict (the
same names the tracer files reads under).
"""
import enum
import functools
import hashlib
import importlib
import pkgutil
import re
import sys
import types

_PATTERN = type(re.compile(""))
_DEPTH = 150
_LRU = type(functools.lru_cache(maxsize=1)(lambda: None))


def canon_key(key):
    """A stable name for a dict key, or None when it has none. The tracer and the
    fingerprint both use it, so an entry is called the same in each."""
    if isinstance(key, str):
        return "s" + key
    if isinstance(key, type):
        return f"c{key.__module__}.{key.__qualname__}"
    if isinstance(key, enum.Enum):
        return f"e{type(key).__qualname__}.{key.name}"
    if key is None or type(key) in (bool, int, float):
        return "v" + repr(key)
    if type(key) is tuple:
        parts = [canon_key(part) for part in key]
        if None in parts:
            return None
        return "t(" + ",".join(parts) + ")"
    return None


def miss_group(key):
    """Which of 64 groups a looked-for key falls in ("" when it has no stable name)."""
    import zlib

    name = canon_key(key)
    return "" if name is None else str(zlib.crc32(name.encode("utf-8", "replace")) % 64)


def miss_group_of_name(name):
    """The same group, from a key's stable name."""
    import zlib

    return str(zlib.crc32(name.encode("utf-8", "replace")) % 64)


class _Hasher:
    def __init__(self):
        self.memo = {}       # id -> digest, for objects whose digest stands on its own
        self.open = {}       # id -> depth, for objects being walked (cycles)
        self.low = 1 << 30   # shallowest open object a cut-off pointed back to
        self.truncated = False
        self.keep = []       # objects kept alive so ids are not reused

    def of(self, value, depth=0):
        kind = type(value)
        if value is None or kind in (bool, int, float, complex, str, bytes):
            return f"{kind.__name__}:{value!r}"
        key = id(value)
        done = self.memo.get(key)
        if done is not None:
            return done
        at = self.open.get(key)
        if at is not None:
            # Part of a cycle: the digest of whatever is being computed now depends
            # on where the walk came from, so it must not be reused elsewhere.
            if at < self.low:
                self.low = at
            return f"loop:{kind.__name__}"
        if depth > _DEPTH:
            self.truncated = True
            return f"deep:{kind.__name__}"
        outer = self.low
        self.low = 1 << 30
        self.open[key] = depth
        try:
            text = self._walk(value, kind, depth + 1)
        finally:
            del self.open[key]
        digest = hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=12).hexdigest()
        if self.low >= depth:
            self.memo[key] = digest   # nothing inside pointed above this object
            self.keep.append(value)
            self.low = outer
        else:
            self.low = min(outer, self.low)
        return digest

    def _walk(self, value, kind, depth):
        of = self.of
        if isinstance(value, enum.Enum):
            if isinstance(value, (str, int, float, bytes)):
                # Also a str or int: wherever it is held, its value is what compares.
                return f"enum:{kind.__module__}.{kind.__qualname__}.{value.name}:{of(value._value_, depth)}"
            # A plain member compares and hashes by identity: a table holding it does
            # not change when its value does. The member's own value is filed with
            # its class (see walk_class).
            return f"enum:{kind.__module__}.{kind.__qualname__}.{value.name}"
        if isinstance(value, type):
            return f"class:{value.__module__}.{value.__qualname__}"
        if kind in (tuple, list):
            return kind.__name__ + "(" + ",".join(of(v, depth) for v in value) + ")"
        if isinstance(value, (set, frozenset)):
            return kind.__name__ + "{" + ",".join(sorted(of(v, depth) for v in value)) + "}"
        if isinstance(value, dict):
            # Sorted: the order of a dict built by walking a set changes from run to run.
            return kind.__name__ + "{" + ",".join(sorted(
                of(k, depth) + "=" + of(v, depth) for k, v in list(value.items()))) + "}"
        if kind is types.FunctionType:
            return "function:" + self.code(value, depth) + ":" + self.surroundings(value, depth)
        if kind is types.MethodType:
            return "method:" + of(value.__self__, depth) + ":" + of(value.__func__, depth)
        if kind in (classmethod, staticmethod):
            return kind.__name__ + ":" + of(value.__func__, depth)
        if kind is property:
            return "property:" + of(value.fget, depth) + of(value.fset, depth) + of(value.fdel, depth)
        if kind is functools.partial:
            return "partial:" + of(value.func, depth) + of(value.args, depth) + of(value.keywords, depth)
        if kind is types.CodeType:
            return self._code(value, depth)
        if kind is types.ModuleType:
            return f"module:{value.__name__}"
        if kind is _PATTERN:
            return f"regex:{value.pattern!r}:{value.flags}"
        if kind is _LRU:
            # What a cache holds cannot be read; how much it holds can.
            return f"cache:{value.cache_info().currsize}:" + of(value.__wrapped__, depth)
        if kind in (types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType,
                    types.MemberDescriptorType, types.GetSetDescriptorType, types.ClassMethodDescriptorType):
            owner = getattr(value, "__self__", None)
            held = "" if owner is None or isinstance(owner, types.ModuleType) else of(owner, depth)
            return f"builtin:{getattr(value, '__qualname__', repr(value))}:{held}"
        # Any other object: its type and whatever state it carries.
        parts = [f"object:{kind.__module__}.{kind.__qualname__}"]
        state = getattr(value, "__dict__", None)
        if isinstance(state, dict):
            parts.append(of(state, depth))
        seen = False
        for owner in kind.__mro__:
            slots = owner.__dict__.get("__slots__", ())
            for name in ((slots,) if isinstance(slots, str) else slots or ()):
                if name not in ("__dict__", "__weakref__"):
                    seen = True
                    try:
                        parts.append(name + "=" + of(getattr(value, name), depth))
                    except AttributeError:
                        pass
        if not isinstance(state, dict) and not seen:
            try:
                parts.append("repr=" + re.sub(r" at 0x[0-9a-f]+", "", repr(value)))
            except Exception:  # noqa: BLE001
                pass
        return "|".join(parts)

    def _code(self, code, depth):
        consts = ",".join(self.of(c, depth) for c in code.co_consts)
        return (f"code:{code.co_name}:{code.co_argcount}:{code.co_flags}:"
                f"{hashlib.blake2b(code.co_code, digest_size=8).hexdigest()}:"
                f"{code.co_names}:{code.co_varnames}:{code.co_freevars}:{consts}")

    def code(self, function, depth=0):
        """What the function's own source compiles to."""
        return self.of(function.__code__, depth)

    def surroundings(self, function, depth=0):
        """What the function was given from outside when it was made: defaults and
        the variables it closes over."""
        of = self.of
        cells = []
        for cell in function.__closure__ or ():
            try:
                cells.append(of(cell.cell_contents, depth))
            except ValueError:
                cells.append("empty")
        return (function.__module__ + "." + function.__qualname__ + ":"
                + of(function.__defaults__, depth) + ":" + of(function.__kwdefaults__, depth)
                + ":" + ",".join(cells))


def load_everything():
    """Import every sqlglot module, lazily loaded dialects included."""
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        try:
            importlib.import_module(info.name)
        except BaseException:  # noqa: BLE001 - a module that cannot load shows up as missing
            pass


SKIP_GLOBALS = ("__builtins__", "__cached__", "__spec__", "__loader__", "__file__", "__doc__",
                "__path__", "__annotations__", "__warningregistry__")
SKIP_ATTRS = ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__", "__annotations__",
              "__firstlineno__", "__static_attributes__", "__annotate__", "__annotations_cache__",
              "__annotate_func__", "__parameters__", "__orig_bases__", "__slots__")


def _plain(function):
    while isinstance(function, (classmethod, staticmethod)):
        function = function.__func__
    return function if type(function) is types.FunctionType else None


def fingerprint():
    """{key: digest} for all loaded sqlglot code, and whether anything was cut short.

    A function is filed twice: under its key for what surrounds it, and under
    key + "\\tcode" for what its own source compiles to. An edit inside a function
    body shows up only in the second, which line coverage already accounts for.
    """
    hasher = _Hasher()
    out = {}

    def put(key, item):
        function = _plain(item)
        if function is not None:
            out[key + "\tcode"] = hasher.code(function)
            out[key] = type(item).__name__ + ":" + hasher.surroundings(function)
        else:
            out[key] = hasher.of(item)

    def walk_class(cls, module_name):
        owner = f"{module_name}:{cls.__qualname__}"
        for attr, item in list(vars(cls).items()):
            if attr in SKIP_ATTRS or attr.startswith("_abc_"):
                continue
            key = f"{owner}|{attr}"
            if isinstance(item, enum.Enum) and type(item) is cls and \
                    not isinstance(item, (str, int, float, bytes)):
                out[key] = "member:" + hasher.of(item._value_)
                continue
            if isinstance(item, type):
                out[key] = f"class:{item.__module__}.{item.__qualname__}"
                if item.__module__ == module_name and item.__qualname__.startswith(cls.__qualname__ + "."):
                    walk_class(item, module_name)
            elif type(item) is dict and item:
                names = [canon_key(k) for k in item]
                if None in names or len(set(names)) != len(names):
                    out[key] = hasher.of(item)
                    continue
                out[key] = "dict"
                for name, value in zip(names, list(item.values())):
                    put(f"{key}|{name}", value)
            else:
                put(key, item)
        out[f"{owner}|(bases)"] = hasher.of(cls.__bases__)

    for name, module in sorted(sys.modules.items()):
        if module is None or not (name == "sqlglot" or name.startswith("sqlglot.")):
            continue
        for key, value in list(vars(module).items()):
            if key in SKIP_GLOBALS:
                continue
            if isinstance(value, types.ModuleType):
                out[f"{name}\t{key}"] = f"module:{value.__name__}"
            elif isinstance(value, type) and value.__module__ == name and "." not in value.__qualname__:
                out[f"{name}\t{key}"] = f"class:{name}.{value.__qualname__}"
                walk_class(value, name)
            else:
                put(f"{name}\t{key}", value)
    return out, hasher.truncated


def compare(pinned, patched):
    """(changed, added, removed) keys between two fingerprints."""
    changed = sorted(k for k, v in patched.items() if k in pinned and pinned[k] != v)
    added = sorted(k for k in patched if k not in pinned)
    removed = sorted(k for k in pinned if k not in patched)
    return changed, added, removed


def load(path):
    from . import blob

    return blob.load(path)


if __name__ == "__main__":
    # python -m br.state <sqlglot checkout> <out file>: the pinned fingerprint.
    # A second load under another hash seed shows which names are not the same from
    # one run to the next (built by walking a set); they are set aside.
    import os
    import pickle
    import subprocess
    import zlib

    root, target = sys.argv[1], sys.argv[2]
    sys.path.insert(0, root)
    sys.setrecursionlimit(20000)
    import unittest  # noqa: F401 - sqlglot notes whether it runs under tests; here it always does

    os.environ["SKIP_INTEGRATION"] = "0"
    load_everything()
    table, cut = fingerprint()
    unstable = []
    if len(sys.argv) < 4:
        for seed in ("101", "202"):
            other = target + ".other"
            subprocess.run([sys.executable, "-m", "br.state", root, other, "raw"], check=True,
                           env=dict(os.environ, PYTHONHASHSEED=seed))
            second = load(other)["table"]
            os.remove(other)
            unstable.extend(k for k in set(table) | set(second) if table.get(k) != second.get(k))
    unstable = sorted(set(unstable))
    modules = sorted({k.split("\t")[0].split(":")[0] for k in unstable})
    with open(target, "wb") as handle:
        handle.write(zlib.compress(pickle.dumps(
            {"table": {k: v for k, v in table.items() if k not in unstable}, "truncated": cut,
             "unstable": unstable, "unstable_modules": modules}, protocol=5), 9))
    if len(sys.argv) < 4:
        import json

        with open(target + ".meta", "w") as handle:
            json.dump({"unstable_modules": modules}, handle)
    print(f"{len(table)} names, cut short: {cut}, unstable: {unstable}", file=sys.stderr)
