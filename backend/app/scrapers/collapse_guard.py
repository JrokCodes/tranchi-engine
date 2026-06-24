"""Immediate per-source collapse tripwire (runs every scrape cycle from run.py).

Fires a Telegram alert the moment a source's active-listing count drops sharply between
its two most recent successful runs. This is the safety net that was MISSING on
2026-06-21 when Wayne blight silently went 43,718 -> 6: audit_scrapers runs once daily
(08:05 UTC) and missed the 15:22 collapse, so the loss went unnoticed for a day. This
runs at the end of every main run.py cycle, so a mass-retire pages within one cycle.

A collapse = active fell >40% AND by >=50 listings vs the previous run. Conservative
enough that routine sold/redeemed churn won't trip it; sensitive enough to catch a wipe.
Thresholds are intentionally blunt — a false positive is a glance at a dashboard; a false
negative is another silent 43k-lead loss.

ZERO-YIELD tripwire (added 2026-06-24): detect_collapses only compares `active` between
the two most recent SUCCESSFUL runs inside a 7-DAY window. It therefore CANNOT catch two
real failure modes the Lucas audit surfaced:
  (1) a scraper that hits a broken source and returns [] is logged status='success',
      found=0 — a silent outage that looks identical to "nothing new"; and
  (2) a MONTHLY signal source (e.g. lucas_areis_delinquent → 11,314 tax leads) whose prior
      run is >7 days old, so it never enters the 7-day comparison at all. A failed monthly
      pull would let its ~11k leads age out over the 45-day freshness window with NO single
      run showing a >40% drop — the slow-motion version of the blight wipe.
detect_zero_yield closes both: it fires when a source's MOST RECENT run yielded found=0
while that source RELIABLY returns a full count every run. "Reliably" = the MEDIAN of its
recent successful `found` is >= _ZY_FLOOR. The median is the load-bearing discriminator:
delta-pull sources (code_violations, wayne_blight) and any source where found=0 is a normal
outcome have a low/zero median, so they never trip; only true full-rescan / full-pull
sources (AREIS roll, RealAuction roster, parcel spine, TLN sheriff roster) qualify, and for
those a 0 genuinely means a silent outage. A short recency window (_ZY_RECENT_HOURS) on the
latest run means a failed monthly source alerts once — on the run that just failed (that
source's own --site cron invokes run.py, which calls this) — not every cycle for a month.
"""
from __future__ import annotations

import logging

import asyncpg

from app.notify import send_telegram

logger = logging.getLogger("collapse_guard")

_DROP_PCT = 0.40
_DROP_MIN = 50

# Zero-yield tripwire knobs. _ZY_FLOOR: the source's MEDIAN recent `found` must be at least
# this for a drop-to-zero to be alarming — this is what excludes delta/cursor sources whose
# found=0 is a normal outcome (their median is ~0) while keeping full-pull sources.
# _ZY_LOOKBACK_DAYS: window for the median + recency of the prior data (covers a monthly
# cadence). _ZY_MIN_RUNS: need at least this many successful runs to trust the median.
# _ZY_RECENT_HOURS: only alert when the failing run JUST happened (one alert per failure,
# not one per 3h cycle for the whole month the source stays at zero).
_ZY_FLOOR = 30
_ZY_LOOKBACK_DAYS = 75
_ZY_MIN_RUNS = 2
_ZY_RECENT_HOURS = 4


async def detect_collapses(conn: asyncpg.Connection) -> list[dict]:
    """Return sources whose latest run's active count collapsed vs the prior run."""
    rows = await conn.fetch(
        """
        WITH ranked AS (
            SELECT source_site, active, started_at,
                   row_number() OVER (PARTITION BY source_site ORDER BY started_at DESC) AS rn
            FROM tranchi.scrape_runs
            WHERE status = 'success' AND started_at > now() - interval '7 days'
        )
        SELECT c.source_site, p.active AS prev_active, c.active AS curr_active
        FROM ranked c
        JOIN ranked p ON p.source_site = c.source_site AND p.rn = 2
        WHERE c.rn = 1 AND p.active > 0
          AND c.active < p.active * (1 - $1::numeric)
          AND (p.active - c.active) >= $2
        ORDER BY (p.active - c.active) DESC
        """,
        _DROP_PCT, _DROP_MIN,
    )
    return [dict(r) for r in rows]


async def detect_zero_yield(conn: asyncpg.Connection) -> list[dict]:
    """Return sources whose most-recent (just-completed) run found 0 while a recent run had data.

    Catches a silent source outage (broken HTML/GIS/maintenance → []) that the active-count
    collapse check misses, including monthly sources outside its 7-day window. Only considers
    the latest run when it is recent (_ZY_RECENT_HOURS) so the alert fires once, on the run
    that failed, rather than every cycle for as long as the source stays at zero.
    """
    rows = await conn.fetch(
        """
        WITH latest AS (
            SELECT DISTINCT ON (source_site)
                   source_site, found, started_at
            FROM tranchi.scrape_runs
            ORDER BY source_site, started_at DESC
        ),
        stats AS (
            SELECT source_site,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY found) AS med_found,
                   count(*) AS n
            FROM tranchi.scrape_runs
            WHERE status = 'success'
              AND started_at > now() - ($2::int * interval '1 day')
            GROUP BY source_site
        )
        SELECT l.source_site, s.med_found::bigint AS typical_found
        FROM latest l
        JOIN stats s ON s.source_site = l.source_site
        WHERE l.found = 0
          AND l.started_at > now() - ($4::int * interval '1 hour')
          AND s.n >= $3
          AND s.med_found >= $1
        ORDER BY s.med_found DESC
        """,
        _ZY_FLOOR, _ZY_LOOKBACK_DAYS, _ZY_MIN_RUNS, _ZY_RECENT_HOURS,
    )
    return [dict(r) for r in rows]


async def check_and_alert_collapse(pool: asyncpg.Pool) -> int:
    """Detect collapses + zero-yield outages and fire one Telegram alert. Never raises.

    Returns the number of flagged sources (collapses + zero-yield) so the caller can log it.
    """
    try:
        async with pool.acquire() as conn:
            collapses = await detect_collapses(conn)
            zero_yield = await detect_zero_yield(conn)
    except Exception as exc:  # noqa: BLE001 — a tripwire must never crash the run
        logger.error("collapse_guard query failed (non-fatal): %s", exc)
        return 0
    if not collapses and not zero_yield:
        return 0
    lines: list[str] = ["\U0001f6a8 TRANCHI TRIPWIRE — investigate before this propagates:", ""]
    if collapses:
        lines.append("Active-count collapse (vs previous run):")
        for c in collapses:
            pct = 100 * (c["prev_active"] - c["curr_active"]) / c["prev_active"]
            lines.append(f"• {c['source_site']}: {c['prev_active']:,} → {c['curr_active']:,}  (−{pct:.0f}%)")
        lines.append("")
    if zero_yield:
        lines.append("Zero-yield (source returned 0 but normally returns a full count — likely a silent outage):")
        for z in zero_yield:
            lines.append(f"• {z['source_site']}: found=0 this run (typically ~{z['typical_found']:,})")
        lines.append("")
    lines.append("(run.py collapse_guard)")
    msg = "\n".join(lines)
    logger.warning("tripwire fired: %d collapse, %d zero-yield", len(collapses), len(zero_yield))
    send_telegram(msg)
    return len(collapses) + len(zero_yield)
