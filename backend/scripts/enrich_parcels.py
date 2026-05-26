"""
Parcel enrichment / backfill — closes the registry-coverage gap.

Fills tranchi.parcels for active listings that reference a parcel which is MISSING
or a STUB (owner_name IS NULL), so every listing is independently cross-confirmable
against the county's own record (owner / market value / tax balance / delinquent
flag) and parcel-keyed signals (probate, code violations) can land.

For each target parcel it does a MyPlace **Parcel-mode** search + PropertyData
detail fetch (httpx, server-rendered — no Playwright) and upserts via
fiscal_officer.upsert_parcels (COALESCE-based, never clobbers good data).

Usage:
  python scripts/enrich_parcels.py --limit 400     # bounded (cron-friendly)
  python scripts/enrich_parcels.py --all           # full backfill (run detached)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
_backend = _here.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_env_file = _backend / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("scripts.enrich_parcels")

import asyncpg  # noqa: E402
import httpx  # noqa: E402

from app.scrapers.fiscal_officer import (  # noqa: E402
    _MODE_PARCEL, _ENTIRE_COUNTY_CODE, _TIMEOUT, _DETAIL_DELAY,
    _default_headers, _jitter, _fetch_search_page, _parse_hit_list,
    _fetch_property_data, upsert_parcels,
)


async def _targets(conn: asyncpg.Connection, limit: int | None) -> list[str]:
    sql = """
        SELECT DISTINCT l.source_listing_id AS parcel
        FROM tranchi.listings l
        LEFT JOIN tranchi.parcels p ON p.parcel_number = l.source_listing_id
        WHERE l.status = 'active'
          AND l.source_listing_id IS NOT NULL AND l.source_listing_id <> ''
          AND (p.parcel_number IS NULL OR p.owner_name IS NULL)
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [r["parcel"] for r in await conn.fetch(sql)]


async def run(limit: int | None) -> None:
    url = os.environ["DATABASE_URL"]
    pool = await asyncpg.create_pool(url, min_size=1, max_size=3)
    enriched = 0
    misses = 0
    try:
        async with pool.acquire() as conn:
            parcels = await _targets(conn, limit)
        logger.info("Enrich targets: %d parcels (missing or stub)", len(parcels))

        headers = _default_headers()
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=_TIMEOUT) as client:
            for i, parcel in enumerate(parcels, 1):
                try:
                    # Parcel-mode search; MyPlace accepts the display form DDD-NN-NNN.
                    html = await _fetch_search_page(client, parcel, _ENTIRE_COUNTY_CODE, mode=_MODE_PARCEL)
                    hits = _parse_hit_list(html) if html else []
                    match = next((h for h in hits if h.get("parcel_number") == parcel), hits[0] if hits else None)
                    if not match:
                        misses += 1
                        logger.debug("no hit for parcel %s", parcel)
                        continue
                    detail = await _fetch_property_data(client, match, parcel, _ENTIRE_COUNTY_CODE)
                    match.update({k: v for k, v in detail.items() if not k.startswith("_")})
                    await upsert_parcels(pool, [match])
                    enriched += 1
                except Exception as exc:
                    misses += 1
                    logger.debug("parcel %s enrich error: %s", parcel, exc)
                await asyncio.sleep(_jitter(*_DETAIL_DELAY))
                if i % 100 == 0:
                    logger.info("...progress: %d/%d processed (%d enriched, %d misses)", i, len(parcels), enriched, misses)
        logger.info("Enrich complete: %d enriched, %d misses (of %d)", enriched, misses, len(parcels))
    finally:
        await pool.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill/enrich tranchi.parcels for listing parcels")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--all", action="store_true", help="process all targets (ignore --limit)")
    args = ap.parse_args()
    asyncio.run(run(None if args.all else args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
