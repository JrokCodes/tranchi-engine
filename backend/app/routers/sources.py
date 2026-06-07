"""
Tranchi Engine — Sources router
GET /api/v1/sources — latest scrape_run per source_site with online/minutes_since fields.
No auth required (Cloudflare gates the public hostname).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.database import get_db
from app.market_config import merged_source_meta

router = APIRouter()
logger = logging.getLogger(__name__)

# Cron is every 3 hours; allow a 60-min buffer before marking offline.
_ONLINE_THRESHOLD_SECONDS = 4 * 3600

# Per-source metadata: the real public site each scraper pulls from, and the
# source's role. "deal" sources create listings; "signal" sources tag parcels with
# distress signals; "registry" is the parcel identity/enrichment spine; "lead" are
# pre-distress signals surfaced as listings (migration 012). The per-source (url,
# category) lives in each market's config (market_config.py "source_meta"); this is
# the merge across all markets, so adding a market is a config edit, not an edit here.
_SOURCE_META: dict[str, tuple[str | None, str]] = merged_source_meta()


class SourceCard(BaseModel):
    source_site: str
    source_url: str | None
    category: str  # "deal" | "signal" | "registry"
    status: str
    online: bool
    started_at: datetime | None
    completed_at: datetime | None
    minutes_since: int | None
    found: int
    passed: int
    active: int
    filtered: int
    dupes: int
    delisted: int
    expired: int
    new_today: int
    error_message: str | None


class SourcesResponse(BaseModel):
    sources: list[SourceCard]


@router.get("", response_model=SourcesResponse)
async def get_sources(
    conn: asyncpg.Connection = Depends(get_db),
) -> SourcesResponse:
    """Latest scrape_run per source_site.

    online = status=='success' AND started_at within last 4 hours.
    minutes_since = minutes elapsed since started_at (UTC now - started_at).
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (source_site)
            source_site,
            status,
            started_at,
            completed_at,
            found,
            passed,
            active,
            filtered,
            dupes,
            delisted,
            expired,
            new_today,
            error_message
        FROM tranchi.scrape_runs
        ORDER BY source_site, started_at DESC
        """
    )

    now = datetime.now(timezone.utc)
    sources: list[SourceCard] = []

    for r in rows:
        started_at: datetime | None = r["started_at"]
        if started_at is not None and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)

        minutes_since: int | None = None
        online = False
        if started_at is not None:
            elapsed = (now - started_at).total_seconds()
            minutes_since = int(elapsed // 60)
            online = r["status"] == "success" and elapsed <= _ONLINE_THRESHOLD_SECONDS

        meta = _SOURCE_META.get(r["source_site"], (None, "deal"))
        sources.append(
            SourceCard(
                source_site=r["source_site"],
                source_url=meta[0],
                category=meta[1],
                status=r["status"] or "unknown",
                online=online,
                started_at=started_at,
                completed_at=r["completed_at"],
                minutes_since=minutes_since,
                found=int(r["found"] or 0),
                passed=int(r["passed"] or 0),
                active=int(r["active"] or 0),
                filtered=int(r["filtered"] or 0),
                dupes=int(r["dupes"] or 0),
                delisted=int(r["delisted"] or 0),
                expired=int(r["expired"] or 0),
                new_today=int(r["new_today"] or 0),
                error_message=r["error_message"],
            )
        )

    return SourcesResponse(sources=sources)
