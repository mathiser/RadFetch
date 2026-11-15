"""PACS protection primitives: rate budget, time windows, circuit breaker."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Callable

Clock = Callable[[], float]


class TokenBucket:
    """Rate budget in instances per minute.

    Work is admitted while the balance is positive and charged afterwards with the
    real instance count, so the balance can go negative; a large series is then
    followed by a proportionally long pause. rate <= 0 disables the limit.
    """

    def __init__(self, rate_per_min: float, burst: float | None = None, clock: Clock = time.monotonic):
        self.rate = rate_per_min / 60.0
        self.capacity = burst if burst is not None else max(rate_per_min, 1.0)
        self.tokens = self.capacity
        self._clock = clock
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
        self._last = now

    def ready(self) -> bool:
        if self.rate <= 0:
            return True
        self._refill()
        return self.tokens > 0

    def consume(self, n: float) -> None:
        if self.rate <= 0:
            return
        self._refill()
        self.tokens -= n


class CircuitBreaker:
    """Stops all PACS traffic after consecutive failures; cooldown doubles while failures continue."""

    def __init__(self, threshold: int, base_cooldown_s: float, max_cooldown_s: float, clock: Clock = time.monotonic):
        self.threshold = threshold
        self.base = base_cooldown_s
        self.max = max_cooldown_s
        self._clock = clock
        self.failures = 0
        self.open_until = 0.0

    def allow(self) -> bool:
        return self._clock() >= self.open_until

    @property
    def is_open(self) -> bool:
        return not self.allow()

    def record_success(self) -> None:
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            cooldown = min(self.max, self.base * 2 ** (self.failures - self.threshold))
            self.open_until = self._clock() + cooldown


_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_ITEM_RE = re.compile(r"^(?:(?P<days>[a-z,\-]+)\s+)?(?P<start>\d{1,2}:\d{2})\s*-\s*(?P<end>\d{1,2}:\d{2})$")


@dataclass(frozen=True)
class _Window:
    days: frozenset[int]
    start: int  # minutes since midnight
    end: int  # minutes since midnight, 1440 = end of day

    def contains(self, weekday: int, minute: int) -> bool:
        if self.start < self.end:
            return weekday in self.days and self.start <= minute < self.end
        # Overnight window: belongs to the day it starts on.
        if weekday in self.days and minute >= self.start:
            return True
        return (weekday - 1) % 7 in self.days and minute < self.end


def _parse_minutes(text: str) -> int:
    h, m = text.split(":")
    minutes = int(h) * 60 + int(m)
    if not (0 <= minutes <= 1440 and int(m) < 60):
        raise ValueError(f"invalid time: {text}")
    return minutes


def _parse_days(text: str | None) -> frozenset[int]:
    if not text:
        return frozenset(range(7))
    days: set[int] = set()
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-")
            i, j = _DAYS.index(a), _DAYS.index(b)
            while True:
                days.add(i)
                if i == j:
                    break
                i = (i + 1) % 7
        else:
            days.add(_DAYS.index(part))
    return frozenset(days)


class TimeWindows:
    """Windows such as "mon-fri 18:00-06:00; sat-sun 00:00-24:00". Empty spec means always open."""

    def __init__(self, spec: str = ""):
        self._windows: list[_Window] = []
        for item in filter(None, (s.strip().lower() for s in spec.split(";"))):
            m = _ITEM_RE.match(item)
            if not m:
                raise ValueError(f"invalid window: {item!r}")
            try:
                days = _parse_days(m["days"])
            except ValueError:
                raise ValueError(f"invalid days in window: {item!r}") from None
            start, end = _parse_minutes(m["start"]), _parse_minutes(m["end"])
            if start == end:
                raise ValueError(f"empty window: {item!r}")
            self._windows.append(_Window(days, start, end))

    def is_open(self, now: datetime | None = None) -> bool:
        if not self._windows:
            return True
        now = now or datetime.now()
        minute = now.hour * 60 + now.minute
        return any(w.contains(now.weekday(), minute) for w in self._windows)
