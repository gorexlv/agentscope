"""Budgets and signal restoration for bounded interactive operations."""
import contextlib
import signal
import time


def remaining(deadline, maximum):
    if deadline is None:
        return maximum
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError('operation budget exhausted')
    return min(value, maximum)


@contextlib.contextmanager
def exit_on_signals():
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    saved = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            saved[sig] = signal.signal(sig, stop)
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)
