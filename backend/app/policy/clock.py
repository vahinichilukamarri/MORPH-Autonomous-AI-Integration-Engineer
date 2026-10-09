"""An injectable clock, so expiry and ordering can be tested without waiting."""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """A clock that only moves when told to. Times are timezone-aware UTC."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
