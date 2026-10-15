"""Time helpers and a small injectable clock.

The controller and the service never call ``time.time`` directly: they use a
:class:`Clock`. Production code uses :class:`SystemClock`; tests use
:class:`ManualClock` to advance time deterministically without sleeping.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def format_timestamp(value: datetime) -> str:
    """Format a datetime the way the dataset/reference uses (second precision)."""
    return value.strftime(TIMESTAMP_FORMAT)


def parse_timestamp(value: str) -> datetime:
    """Parse a dataset/JSON timestamp; raise ValueError with a clear message."""
    try:
        return datetime.strptime(str(value).strip(), TIMESTAMP_FORMAT)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid timestamp {value!r}; expected {TIMESTAMP_FORMAT}") from exc


def now_timestamp() -> str:
    return format_timestamp(datetime.now())


def seconds_between(later: str, earlier: str) -> float:
    """Whole-second difference between two dataset timestamps (later - earlier)."""
    return (parse_timestamp(later) - parse_timestamp(earlier)).total_seconds()


def add_seconds(timestamp: str, seconds: float) -> str:
    return format_timestamp(parse_timestamp(timestamp) + timedelta(seconds=seconds))


class Clock:
    """Monotonic seconds source used to drive phase countdowns."""

    def now(self) -> float:  # pragma: no cover - interface
        raise NotImplementedError


class SystemClock(Clock):
    """Wall-clock (monotonic) implementation for the running service."""

    def now(self) -> float:
        import time

        return time.monotonic()


class ManualClock(Clock):
    """Deterministic clock for tests: advance explicitly, never real-sleeps."""

    def __init__(self, start: float = 0.0) -> None:
        self._value = float(start)

    def now(self) -> float:
        return self._value

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("cannot advance a clock backwards")
        self._value += float(seconds)
        return self._value

    def set(self, value: float) -> float:
        self._value = float(value)
        return self._value
