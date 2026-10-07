"""Bounded retry with exponential backoff, a Retry-After cap and a total time budget."""

import random
import time
from dataclasses import dataclass
from typing import Protocol

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 8.0
    max_retry_after: float = 30.0
    total_budget: float = 60.0
    jitter: float = 0.25

    def backoff(self, attempt: int, rng: random.Random) -> float:
        """Delay before attempt ``attempt + 1`` (``attempt`` counts from 1)."""
        delay = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        return float(delay * (1 + self.jitter * rng.random()))


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header; HTTP-dates are not honoured (treated as absent)."""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None
