"""Wall-clock abstraction and deterministic advanceable simulation clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class WallClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SimClock:
    def __init__(self, initial: datetime) -> None:
        self._now = self._validate(initial)

    @staticmethod
    def _validate(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("simulation time must be timezone-aware")
        return value.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, *, hours: int = 0, days: int = 0) -> datetime:
        delta = timedelta(hours=hours, days=days)
        if delta <= timedelta(0):
            raise ValueError("simulation time can only advance by a positive duration")
        self._now += delta
        return self._now

    def set(self, value: datetime) -> datetime:
        new_value = self._validate(value)
        if new_value < self._now:
            raise ValueError("simulation time cannot move backwards")
        self._now = new_value
        return self._now
