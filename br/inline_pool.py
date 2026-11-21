"""Run work that sqlglot's tests hand to ProcessPoolExecutor in this process instead.

The real tests spawn worker processes and exchange pickled arguments and results.
Pickling is kept only when the patched code is reachable through it.
"""
import concurrent.futures
import pickle

PICKLE = False


def _copy(value):
    return pickle.loads(pickle.dumps(value)) if PICKLE else value


class InlinePool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer is not None:
            initializer(*_copy(initargs))

    def submit(self, fn, *args, **kwargs):
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


def install(use_pickle):
    global PICKLE
    PICKLE = use_pickle
    concurrent.futures.ProcessPoolExecutor = InlinePool
    import concurrent.futures.process as process

    process.ProcessPoolExecutor = InlinePool
