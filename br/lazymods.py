"""Stand-ins for heavy libraries that load the real thing on first use.

numpy and pandas take about a second of CPU to import, and sqlglot's test modules
import them at the top (tests/test_optimizer.py takes `assert_frame_equal` from
pandas.testing) although only a few tests ever call them. With these stand-ins in
sys.modules, `import pandas as pd`, `import numpy as np` and
`from pandas.testing import assert_frame_equal` succeed at once; the real library is
imported the first time anything is looked up on it or the function is called.
"""
import importlib
import sys
import types

_real = {}


def _load(name):
    module = _real.get(name)
    if module is None:
        # Take every stand-in out of the way so the real packages import normally.
        for key in [k for k, v in sys.modules.items() if isinstance(v, _Lazy)]:
            del sys.modules[key]
        module = importlib.import_module(name)
        _real[name] = module
    return module


class _Lazy(types.ModuleType):
    def __getattr__(self, attr):
        name = self.__name__
        if attr.startswith("__") and attr.endswith("__"):
            return getattr(_load(name), attr)
        if name in _real:
            return getattr(_real[name], attr)
        sub = f"{name}.{attr}"
        if sub in _STUBS:
            return sys.modules.get(sub) or _make(sub)
        if attr[:1].isalpha() and _CALLABLE_FROM.get(name) and attr in _CALLABLE_FROM[name]:
            # Taken by name at import time (`from pandas.testing import x`): hand back a
            # function that loads the library when it is first called.
            def call(*args, _attr=attr, _name=name, **kwargs):
                return getattr(_load(_name), _attr)(*args, **kwargs)

            call.__name__ = attr
            return call
        return getattr(_load(name), attr)


_STUBS = ("numpy", "pandas", "pandas.testing")
_CALLABLE_FROM = {"pandas.testing": {"assert_frame_equal", "assert_series_equal"}}


def _make(name):
    module = _Lazy(name)
    module.__path__ = []           # lets `import pandas.testing` treat it as a package
    module.__spec__ = None
    sys.modules[name] = module
    return module


def install():
    """Put the stand-ins in place unless the real libraries are already loaded."""
    if any(name in sys.modules for name in _STUBS):
        return
    for name in _STUBS:
        _make(name)
