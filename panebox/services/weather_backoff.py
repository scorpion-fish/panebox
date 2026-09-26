"""Refresh backoff policy (port of Services/WeatherRefreshBackoffPolicy.cs)."""

from __future__ import annotations

from datetime import timedelta

FAILURE_DELAYS = (
    timedelta(minutes=2),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=30),
)

LOCATION_REUSE_DURATION = timedelta(hours=24)
LOCATION_FAILURE_DELAY = timedelta(minutes=30)


def can_attempt(now, automatic_refresh_not_before_utc, user_triggered: bool, force_refresh: bool) -> bool:
    return user_triggered or force_refresh or now >= automatic_refresh_not_before_utc


def get_failure_delay(consecutive_failures: int) -> timedelta:
    index = min(max(consecutive_failures - 1, 0), len(FAILURE_DELAYS) - 1)
    return FAILURE_DELAYS[index]
