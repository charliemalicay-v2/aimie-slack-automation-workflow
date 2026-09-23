"""One retry policy shared by the Slack and Ollama clients."""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


class RetryableError(Exception):
    """Transient failure: 429, 5xx, timeout. retry_after comes from the Retry-After header."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RetriesExhausted(Exception):
    pass


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form; fall back to exponential backoff


def compute_delay(attempt: int, retry_after: float | None, base: float = 0.5, cap: float = 30.0) -> float:
    if retry_after is not None:
        return min(retry_after, cap)  # the server told us exactly how long to wait
    return min(cap, base * 2 ** (attempt - 1)) * random.uniform(0.5, 1.0)  # backoff + jitter


def call_with_retry(fn: Callable[[], T], *, max_attempts: int, label: str,
                    sleep: Callable[[float], None] = time.sleep) -> T:
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except RetryableError as exc:
            if attempt == max_attempts:
                raise RetriesExhausted(f"{label}: gave up after {attempt} attempts ({exc})") from exc
            delay = compute_delay(attempt, exc.retry_after)
            log.warning("%s: attempt %d failed (%s); retrying in %.1fs", label, attempt, exc, delay)
            sleep(delay)
    raise AssertionError("unreachable")
