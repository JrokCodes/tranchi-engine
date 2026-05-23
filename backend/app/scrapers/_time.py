"""
Eastern Time helpers for scraper date math.

All "today" calculations inside scrapers MUST go through these helpers
so cron runs (which fire in UTC on EC2) produce Cleveland-local dates.
Cleveland is in America/New_York (ET: UTC-5 EST / UTC-4 EDT).

Usage:
    from app.scrapers._time import today_et, n_days_ago_et

    since = n_days_ago_et(7)   # datetime.date 7 days ago in ET
    today = today_et()         # datetime.date for today in ET
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    """Current datetime in Eastern Time."""
    return datetime.now(tz=ET)


def today_et() -> date:
    """Today's date in Eastern Time (Cleveland local)."""
    return now_et().date()


def n_days_ago_et(n: int) -> date:
    """Date n days ago in Eastern Time.

    Args:
        n: Number of days to look back. n=7 → last 7 days of ET dates.
    """
    return today_et() - timedelta(days=n)
