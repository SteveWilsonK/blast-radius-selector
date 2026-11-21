"""Tracing twin of br/inline_pool.py: runs pool work in-process, always pickling,
and tells the tracer where each piece of work starts and ends."""
import concurrent.futures
import pickle

_hooks = None


def _roundtrip(value):
    _hooks.pickle_enter()
    try:
        return pickle.loads(pickle.dumps(value))
    finally:
        _hooks.pickle_exit()


class _Future(concurrent.futures.Future):
    tag = None

    def result(self, timeout=None):
        _hooks.result(self.tag)
        return super().result(timeout)


class InlinePool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer is not None:
            initializer(*_roundtrip(initargs))

    def submit(self, fn, *args, **kwargs):
        future = _Future()
        state = _hooks.submit_enter(args, kwargs)
        future.tag = state[0]
        try:
            fn2, args2, kwargs2 = _roundtrip((fn, args, kwargs))
            result = fn2(*args2, **kwargs2)
            future.set_result(_roundtrip(result))
        except BaseException as error:  # noqa: BLE001 - mirror a worker's failure
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            future.set_exception(error)
        finally:
            _hooks.submit_exit(state)
        return future

    def map(self, fn, *iterables, **kwargs):
        return [self.submit(fn, *args).result() for args in zip(*iterables)]

    def shutdown(self, wait=True, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install(hooks):
    global _hooks
    _hooks = hooks
    concurrent.futures.ProcessPoolExecutor = InlinePool
    import concurrent.futures.process as process

    process.ProcessPoolExecutor = InlinePool
