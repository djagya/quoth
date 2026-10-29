"""Linux CLI wall-clock budget; leave time for checkpoint unwind and receipt."""
from contextlib import contextmanager
import math
import signal


class DeadlineReached(BaseException):
    """Do not let generic evaluator sanitization swallow the runner deadline."""


@contextmanager
def budget(seconds):
    if not math.isfinite(seconds) or not 0 < seconds <= 3000:
        raise ValueError('deadline_seconds_invalid')
    previous = signal.getsignal(signal.SIGALRM)
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ValueError('deadline_timer_in_use')

    def expired(*args):
        raise DeadlineReached()

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
