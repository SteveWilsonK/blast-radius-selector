"""Serve patched files from memory: modules through an import hook, data through open()."""
import builtins
import importlib.abc
import importlib.machinery
import importlib.util
import io
import os
import sys


class _Loader(importlib.abc.SourceLoader):
    def __init__(self, name, path, source):
        self.name = name
        self.path = path
        self.source = source

    def get_filename(self, fullname):
        return self.path

    def get_data(self, path):
        if path == self.path:
            return self.source
        with open(path, "rb") as f:
            return f.read()

    def get_code(self, fullname):
        # Never trust a cached .pyc of the unpatched file.
        return compile(self.source, self.path, "exec", dont_inherit=True)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, modules):
        self.modules = modules  # module name -> (path, source bytes, is_package)

    def find_spec(self, fullname, path=None, target=None):
        entry = self.modules.get(fullname)
        if entry is None:
            return None
        file_path, source, is_package = entry
        return importlib.util.spec_from_file_location(
            fullname, file_path, loader=_Loader(fullname, file_path, source),
            submodule_search_locations=[os.path.dirname(file_path)] if is_package else None)


def module_name(rel_path):
    parts = rel_path[:-3].split("/")
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def install(root, files):
    """files: relative path -> new text (str), or None for a file the patch removes."""
    modules = {}
    data = {}
    for rel, text in files.items():
        full = os.path.join(root, rel)
        data[full] = None if text is None else text.encode("utf-8")
        if rel.endswith(".py") and text is not None:
            name, is_package = module_name(rel)
            modules[name] = (full, text.encode("utf-8"), is_package)
    if modules:
        sys.meta_path.insert(0, _Finder(modules))
    if data:
        real_open = builtins.open

        def patched_open(file, mode="r", *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                path = os.fspath(file)
                if isinstance(path, bytes):
                    path = os.fsdecode(path)
                if not path.startswith("/"):
                    path = os.path.abspath(path)
                if path in data:
                    content = data[path]
                    if content is None:
                        raise FileNotFoundError(2, "No such file or directory", path)
                    if "w" not in mode and "a" not in mode and "+" not in mode and "x" not in mode:
                        raw = io.BytesIO(content)
                        if "b" in mode:
                            return raw
                        encoding = kwargs.get("encoding") or (args[1] if len(args) > 1 else None)
                        errors = kwargs.get("errors") or (args[2] if len(args) > 2 else None)
                        newline = kwargs.get("newline") if "newline" in kwargs else (
                            args[3] if len(args) > 3 else None)
                        return io.TextIOWrapper(raw, encoding=encoding or "utf-8",
                                                errors=errors, newline=newline)
            return real_open(file, mode, *args, **kwargs)

        builtins.open = patched_open
        io.open = patched_open
