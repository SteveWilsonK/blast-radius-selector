"""Put off class-level set-up work until a test asks for it.

sqlglot's optimizer and executor test classes open duckdb connections and load the
TPC tables in setUpClass, for every test of the class. Most tests never touch them.
Here the connections opened during class set-up become stand-ins that remember what
was run on them and only connect, and replay it, when a test first uses them.
"""
import io
import os

# On: query results the optimizer tests compare as pandas frames are compared as numpy
# columns instead, with the same rules (names, dtypes, null positions, values, floats
# to a relative 1e-5), so pandas is never imported for them.
NO_PANDAS = os.environ.get("BR_NO_PANDAS", "0") == "1"


class _Frame:
    """What `.df()` returns here: column names, SQL types and the rows."""

    def __init__(self, description, rows):
        self.columns = [d[0] for d in description]
        self.types = [str(d[1]).upper() for d in description]
        self.rows = rows


class _Cursor:
    """A duckdb result whose `.df()` gives a _Frame; everything else is the real one."""

    def __init__(self, real):
        self.__dict__["_real"] = real

    def df(self, *args, **kwargs):
        real = self.__dict__["_real"]
        description = list(real.description or ())
        return _Frame(description, real.fetchall())

    fetchdf = df

    def __getattr__(self, name):
        return getattr(self.__dict__["_real"], name)


_INTS = {"TINYINT": "int8", "SMALLINT": "int16", "INTEGER": "int32", "BIGINT": "int64",
         "UTINYINT": "uint8", "USMALLINT": "uint16", "UINTEGER": "uint32", "UBIGINT": "uint64"}
_FLOATS = ("DOUBLE", "DECIMAL", "HUGEINT", "UHUGEINT")


def _kind(sql_type, has_null):
    """The dtype pandas gives a duckdb column of this SQL type."""
    base = sql_type.split("(", 1)[0].strip()
    if sql_type.endswith("]") or base in ("STRUCT", "MAP", "UNION"):
        return "object"
    if base in _INTS:
        return "float64" if has_null else _INTS[base]
    if base in _FLOATS:
        return "float64"
    if base in ("FLOAT", "REAL"):
        return "float32"
    if base == "BOOLEAN":
        return "object" if has_null else "bool"
    if base in ("DATE", "TIMESTAMP", "DATETIME"):
        return "datetime64[us]"
    if base.startswith("TIMESTAMP") or base in ("INTERVAL", "TIME"):
        return base
    return "object"


def _number(value):
    return float("nan") if value is None else float(value)


def _same(x, y):
    if x is y:
        return True
    try:
        if x != x and y != y:       # both NaN
            return True
        return bool(x == y)
    except Exception:  # noqa: BLE001
        return False


def frames_equal(left, right):
    """assert_frame_equal for two _Frames, by pandas' default rules: same column names,
    same dtypes, nulls in the same places, equal values (floats to a relative 1e-5)."""
    import math

    if left.columns != right.columns:
        raise AssertionError(f"DataFrame.columns are different: {left.columns} != {right.columns}")
    if len(left.rows) != len(right.rows):
        raise AssertionError(f"DataFrame shape mismatch: {len(left.rows)} != {len(right.rows)} rows")
    for index, name in enumerate(left.columns):
        a = [row[index] for row in left.rows]
        b = [row[index] for row in right.rows]
        ka = _kind(left.types[index], any(v is None for v in a))
        kb = _kind(right.types[index], any(v is None for v in b))
        if ka != kb:
            raise AssertionError(f'Attributes of DataFrame.iloc[:, "{name}"] are different: dtype {ka} != {kb}')
        if ka in ("float64", "float32"):
            for x, y in zip(a, b):
                x, y = _number(x), _number(y)
                if math.isnan(x) or math.isnan(y):
                    ok = math.isnan(x) and math.isnan(y)
                else:
                    ok = x == y or abs(x - y) <= 1e-8 + 1e-5 * abs(y)
                if not ok:
                    raise AssertionError(f'DataFrame.iloc[:, "{name}"] values are different')
        elif not all(_same(x, y) for x, y in zip(a, b)):
            raise AssertionError(f'DataFrame.iloc[:, "{name}"] values are different')


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
        result = self._open().execute(*args, **kwargs)
        return _Cursor(result) if NO_PANDAS else result

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
    row tables that only the TPC execution tests read empty.

    Safe to call again for the same class (it is set up once per group of tests):
    every call wraps the class's own setUpClass afresh, never an earlier wrapper.
    """
    real = getattr(module, "duckdb", None)
    if isinstance(real, _Module):
        state = real.__dict__["_state"]
    else:
        state = {"setup": False}
        if real is not None:
            module.duckdb = _Module(real, state)
    if NO_PANDAS and "assert_frame_equal" in module.__dict__ and "_br_frame_equal" not in module.__dict__:
        pandas_equal = module.assert_frame_equal

        def assert_frame_equal(left, right, *args, **kwargs):
            if isinstance(left, _Frame) and isinstance(right, _Frame):
                return frames_equal(left, right)
            return pandas_equal(left, right, *args, **kwargs)

        module._br_frame_equal = pandas_equal
        module.assert_frame_equal = assert_frame_equal
    original = cls.__dict__.get("_br_set_up_class")
    if original is None:
        original = cls.__dict__.get("setUpClass")
        if original is None or not hasattr(original, "__func__"):
            return
        cls._br_set_up_class = original
    function = original.__func__
    opener = module.__dict__.get("_br_open_file")
    if opener is None:
        opener = getattr(module, "open_file", None)
        if opener is not None:
            module._br_open_file = opener

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
