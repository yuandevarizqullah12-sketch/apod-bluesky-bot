"""Small retry helper with exponential backoff and jitter."""
import logging
import random
import time
from typing import Callable, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


def retry(
    fn: Callable[[], T],
    *,
    is_retryable: Callable[[Exception], bool],
    what: str,
    attempts: int = 4,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call fn(); on a retryable error wait base_delay * 2**n (+/-25% jitter) and retry."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classified by is_retryable
            if attempt == attempts or not is_retryable(exc):
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1)) * random.uniform(0.75, 1.25)
            log.warning("%s failed (%s); retry %d/%d in %.1fs",
                        what, type(exc).__name__, attempt, attempts - 1, delay)
            sleep(delay)
    raise AssertionError("unreachable")
