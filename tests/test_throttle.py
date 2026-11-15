from datetime import datetime

import pytest

from radfetch.gateway.throttle import CircuitBreaker, TimeWindows, TokenBucket


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_token_bucket_charges_after_the_fact():
    clock = Clock()
    bucket = TokenBucket(rate_per_min=60, burst=10, clock=clock)
    assert bucket.ready()
    bucket.consume(40)  # a large series overdraws the budget
    assert not bucket.ready()
    clock.t = 29
    assert not bucket.ready()
    clock.t = 31  # 1 token per second: 30 s to pay off the debt
    assert bucket.ready()


def test_token_bucket_disabled():
    bucket = TokenBucket(rate_per_min=0)
    bucket.consume(10**6)
    assert bucket.ready()


def test_breaker_opens_and_backs_off():
    clock = Clock()
    breaker = CircuitBreaker(threshold=2, base_cooldown_s=10, max_cooldown_s=25, clock=clock)
    breaker.record_failure()
    assert breaker.allow()
    breaker.record_failure()
    assert not breaker.allow()
    clock.t = 10
    assert breaker.allow()  # half-open: one attempt
    breaker.record_failure()
    clock.t = 29
    assert not breaker.allow()  # cooldown doubled to 20
    clock.t = 30
    breaker.record_failure()
    assert breaker.open_until == 55  # capped at 25
    clock.t = 55
    breaker.record_success()
    breaker.record_failure()
    assert breaker.allow()  # counter was reset


def test_windows():
    w = TimeWindows("mon-fri 18:00-06:00; sat-sun 00:00-24:00")
    assert not w.is_open(datetime(2026, 11, 11, 12, 0))  # Wednesday noon
    assert w.is_open(datetime(2026, 11, 11, 18, 0))  # Wednesday evening
    assert w.is_open(datetime(2026, 11, 12, 5, 59))  # Thursday early, Wednesday's window
    assert not w.is_open(datetime(2026, 11, 12, 6, 0))
    assert w.is_open(datetime(2026, 11, 14, 12, 0))  # Saturday
    assert TimeWindows("").is_open(datetime(2026, 11, 11, 12, 0))
    assert TimeWindows("22:00-04:00").is_open(datetime(2026, 11, 11, 1, 0))


def test_monday_early_morning_belongs_to_sunday():
    w = TimeWindows("mon-fri 18:00-06:00")
    assert not w.is_open(datetime(2026, 11, 16, 3, 0))  # Monday 03:00: Sunday has no window
    assert w.is_open(datetime(2026, 11, 14, 3, 0))  # Saturday 03:00: Friday's window


@pytest.mark.parametrize("spec", ["nonsense", "mon-xyz 10:00-11:00", "10:00-10:00", "25:00-26:00"])
def test_invalid_windows(spec):
    with pytest.raises(ValueError):
        TimeWindows(spec)
