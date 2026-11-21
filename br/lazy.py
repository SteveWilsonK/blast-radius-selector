"""Put off class-level set-up work until a test asks for it.

sqlglot's optimizer and executor test classes open duckdb connections and load the
TPC tables in setUpClass, for every test of the class. Most tests never touch them.
Here the connections opened during class set-up become stand-ins that remember what
was run on them and only connect, and replay it, when a test first uses them.
"""
import io


class _LazyConnection:
    def __init__(self, state, connect, args, kwargs):
        self.__dict__["_state"] = state
        self.__dict__["_connect"] = (connect, args, kwargs)
        self.__dict__["_log"] = []
        self.__dict__["_real"] = None

    def _open(self):
        real = self.__dict__["_real"]
        if real is None:
            connect, args, kwargs = self.__dict__["_connect"]
            real = connect(*args, **kwargs)
            for call_args, call_kwargs in self.__dict__["_log"]:
                real.execute(*call_args, **call_kwargs)
            self.__dict__["_real"] = real
        return real

    def execute(self, *args, **kwargs):
        if self.__dict__["_real"] is None and self.__dict__["_state"]["setup"]:
            self.__dict__["_log"].append((args, kwargs))
            return self
        return self._open().execute(*args, **kwargs)

    def close(self):
        if self.__dict__["_real"] is not None:
            self.__dict__["_real"].close()

    def __getattr__(self, name):
        return getattr(self._open(), name)

    def __enter__(self):
        return self._open().__enter__()

    def __exit__(self, *exc):
        return self._open().__exit__(*exc)


class _Module:
    """Stands in for the duckdb module inside one test module."""

    def __init__(self, real, state):
        self.__dict__["_real"] = real
        self.__dict__["_state"] = state

    def connect(self, *args, **kwargs):
        if self.__dict__["_state"]["setup"]:
            return _LazyConnection(self.__dict__["_state"], self.__dict__["_real"].connect,
                                   args, kwargs)
        return self.__dict__["_real"].connect(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.__dict__["_real"], name)


def install(module, cls, load_rows):
    """Defer the duckdb work of cls.setUpClass; with load_rows False, also leave the
    row tables that only the TPC execution tests read empty."""
    state = {"setup": False}
    real = getattr(module, "duckdb", None)
    if real is not None and not isinstance(real, _Module):
        module.duckdb = _Module(real, state)
    original = cls.__dict__.get("setUpClass")
    if original is None or not hasattr(original, "__func__"):
        return
    function = original.__func__
    opener = getattr(module, "open_file", None)

    def set_up(klass):
        state["setup"] = True
        if not load_rows and opener is not None:
            def header_only(file_name):
                with opener(file_name) as handle:
                    return io.StringIO(handle.readline())

            module.open_file = header_only
        try:
            function(klass)
        finally:
            state["setup"] = False
            if opener is not None:
                module.open_file = opener

    cls.setUpClass = classmethod(set_up)
