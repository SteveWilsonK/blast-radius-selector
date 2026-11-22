"""Apply a patch that only changes function bodies without loading sqlglot again.

A test child is forked from a process that already holds unpatched sqlglot. When a
patch touches a loaded module the child used to throw every sqlglot module away and
import them all again. For a change inside function bodies that is not needed: the
function objects stay and only their code is replaced by what the patched source
compiles to, so every reference to them (class tables, wrappers) runs the new code.

`plan` compares the compiled module of the pinned file (its .pyc) with the compiled
patched file. It returns the replacements, or None whenever the result would not be
exactly what a fresh import gives: any difference in code that runs while the module
loads (module level, class bodies, decorators, defaults), a function that already ran
while sqlglot loaded, or a function object that cannot be found unchanged.
"""
import importlib.util
import marshal
import os
import sys
import types

CODE = types.CodeType
FUNCTION = 0x0001  # CO_OPTIMIZED: set for functions, lambdas, comprehensions; not class bodies


def _pinned_code(path, text):
    """The compiled pinned module: from its .pyc when that matches the source."""
    try:
        with open(importlib.util.cache_from_source(path), "rb") as handle:
            data = handle.read()
        stat = os.stat(path)
        if int.from_bytes(data[4:8], "little") == 0 and \
                int.from_bytes(data[8:12], "little") == int(stat.st_mtime) & 0xFFFFFFFF and \
                int.from_bytes(data[12:16], "little") == stat.st_size & 0xFFFFFFFF:
            code = marshal.loads(data[16:])
            if isinstance(code, CODE):
                return code
    except Exception:  # noqa: BLE001
        pass
    return compile(text, path, "exec", dont_inherit=True)


def _walk(old, new, out):
    """Collect (old code, new code) of changed functions; False if anything that
    runs at load time differs. `old` and `new` are module or class-body code."""
    if old == new:
        return True
    if (old.co_code != new.co_code or old.co_names != new.co_names
            or old.co_flags != new.co_flags or old.co_firstlineno != new.co_firstlineno
            or old.co_linetable != new.co_linetable
            or old.co_exceptiontable != new.co_exceptiontable
            or old.co_varnames != new.co_varnames or old.co_freevars != new.co_freevars
            or old.co_cellvars != new.co_cellvars or len(old.co_consts) != len(new.co_consts)):
        return False
    for a, b in zip(old.co_consts, new.co_consts):
        if isinstance(a, CODE) != isinstance(b, CODE):
            return False
        if not isinstance(a, CODE):
            if type(a) is not type(b) or a != b or repr(a) != repr(b):
                return False
        elif a != b:
            if a.co_flags & FUNCTION:
                out.append((a, b))
            elif not _walk(a, b, out):
                return False
    return True


def _lines(code, into):
    for _, _, line in code.co_lines():
        if line is not None:
            into.add(line)
    for const in code.co_consts:
        if isinstance(const, CODE):
            _lines(const, into)
    return into


INDEX = {}


def build_index(root):
    """Every function object sqlglot holds once loaded, by (file, first line, name).
    Built once in the process the children are forked from."""
    import gc

    prefix = os.path.join(root, "sqlglot") + os.sep
    index = {}
    for item in gc.get_objects():
        if type(item) is types.FunctionType:
            code = item.__code__
            if code.co_filename.startswith(prefix):
                index.setdefault((code.co_filename, code.co_firstlineno, code.co_name), []).append(item)
    INDEX.clear()
    INDEX.update(index)


def plan(root, files, module_name, load_lines):
    """[(function, new code)] for every changed function of the loaded modules, or
    None when the patch cannot be applied this way. files: rel path -> new text."""
    swaps = []
    for rel, new_text in files.items():
        if not rel.endswith(".py"):
            continue                      # a data file: served from memory as before
        if new_text is None or not rel.startswith("sqlglot/"):
            return None
        name, _ = module_name(rel)
        module = sys.modules.get(name)
        if module is None:
            continue                      # not loaded yet: its import will see the patch
        full = os.path.join(root, rel)
        if getattr(module, "__file__", None) != full:
            return None
        with open(full, encoding="utf-8") as handle:
            old_text = handle.read()
        old_module = _pinned_code(full, old_text)
        new_module = compile(new_text, full, "exec", dont_inherit=True)
        changed = []
        if not _walk(old_module, new_module, changed):
            return None
        ran_at_load = set(load_lines.get(rel) or ())
        for old, new in changed:
            if new.co_name != old.co_name or old.co_freevars != new.co_freevars:
                return None               # another function, or its closure layout differs
            if ran_at_load and not ran_at_load.isdisjoint(_lines(old, set())):
                return None               # it ran while sqlglot loaded
            # Every live function made from this code: methods, lambdas held in class
            # tables, wrapped functions. None means it was consumed while loading.
            found = [f for f in INDEX.get((full, old.co_firstlineno, old.co_name), ())
                     if f.__code__ == old]
            if not found:
                return None
            for function in found:
                if function.__doc__ is not None and old.co_consts[:1] != new.co_consts[:1]:
                    return None           # the docstring changed: it is kept on the function
                swaps.append((function, new))
    return swaps


def apply(swaps):
    for function, code in swaps:
        function.__code__ = code
