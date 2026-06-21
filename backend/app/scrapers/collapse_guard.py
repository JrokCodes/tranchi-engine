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
"""
from __future__ import annotations

import logging

import asyncpg

from app.notify import send_telegram

logger = logging.getLogger("collapse_guard")

_DROP_PCT = 0.40
_DROP_MIN = 50


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


async def check_and_alert_collapse(pool: asyncpg.Pool) -> int:
    """Detect collapses and fire one Telegram alert listing them. Never raises."""
    try:
        async with pool.acquire() as conn:
            collapses = await detect_collapses(conn)
    except Exception as exc:  # noqa: BLE001 — a tripwire must never crash the run
        logger.error("collapse_guard query failed (non-fatal): %s", exc)
        return 0
    if not collapses:
        return 0
    lines = ["\U0001f6a8 TRANCHI COLLAPSE TRIPWIRE — source active-count dropped sharply this run:", ""]
    for c in collapses:
        pct = 100 * (c["prev_active"] - c["curr_active"]) / c["prev_active"]
        lines.append(f"• {c['source_site']}: {c['prev_active']:,} → {c['curr_active']:,}  (−{pct:.0f}%)")
    lines += ["", "Investigate before this propagates downstream. (run.py collapse_guard)"]
    msg = "\n".join(lines)
    logger.warning("collapse tripwire fired: %d source(s)", len(collapses))
    send_telegram(msg)
    return len(collapses)
