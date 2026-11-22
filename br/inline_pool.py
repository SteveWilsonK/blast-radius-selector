"""Run work that sqlglot's tests hand to ProcessPoolExecutor in this process instead.

The real tests spawn worker processes and exchange pickled arguments and results.
Pickling is kept only when the patched code is reachable through it.
"""
import concurrent.futures
import pickle

PICKLE = False
LAZY = False     # run each submitted call only when its result is asked for


class _Lazy:
    """A future whose work runs on the first call to result() or exception()."""

    def __init__(self, fn, args, kwargs):
        self._call = (fn, args, kwargs)
        self._value = None
        self._error = None

    def _run(self):
        if self._call is None:
            return
        fn, args, kwargs = self._call
        self._call = None
        try:
            if PICKLE:
                fn, args, kwargs = _copy((fn, args, kwargs))
            self._value = _copy(fn(*args, **kwargs))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:  # noqa: BLE001 - a worker would send it back
            if type(error).__name__ == "TestTimeout":
                raise                      # the CPU limit of the test, not the call's own error
            self._error = error

    def result(self, timeout=None):
        self._run()
        if self._error is not None:
            raise self._error
        return self._value

    def exception(self, timeout=None):
        self._run()
        return self._error

    def done(self):
        return True

    def cancel(self):
        return False

    def add_done_callback(self, fn):
        fn(self)


def _copy(value):
    return pickle.loads(pickle.dumps(value)) if PICKLE else value


class InlinePool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer is not None:
            initializer(*_copy(initargs))

    def submit(self, fn, *args, **kwargs):
        if LAZY:
            return _Lazy(fn, args, kwargs)
        future = concurrent.futures.Future()
        try:
            if PICKLE:
                fn, args, kwargs = _copy((fn, args, kwargs))
            future.set_result(_copy(fn(*args, **kwargs)))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:  # noqa: BLE001 - a worker would send it back
            future.set_exception(error)
        return future

    def map(self, fn, *iterables, **kwargs):
        return [self.submit(fn, *args).result() for args in zip(*iterables)]

    def shutdown(self, wait=True, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install(use_pickle, lazy=False):
    global PICKLE, LAZY
    PICKLE = use_pickle
    LAZY = lazy
    concurrent.futures.ProcessPoolExecutor = InlinePool
    import concurrent.futures.process as process

    process.ProcessPoolExecutor = InlinePool
