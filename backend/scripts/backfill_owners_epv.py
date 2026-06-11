"""
One-time EPV bulk owner backfill — Cuyahoga spine.

Fills owner_name / situs_address / market value / delinquent flag for every
Cuyahoga parcel in tranchi.parcels that is missing owner_name, by streaming the
county's own bulk EPV ArcGIS layer (the authoritative ~570K-parcel dataset
behind MyPlace — same service delinquent_tax.py already uses) and filtering to
our missing pins. One pass, ~minutes, no per-parcel MyPlace scraping.

Companion to enrich_parcels.py (the daily per-parcel drip cron): this closes
the Lever-B backlog in bulk; the cron remains the steady-state gap-closer for
fields EPV doesn't carry.

Usage:
  python scripts/backfill_owners_epv.py --dry-run   # report match counts, no writes
  python scripts/backfill_owners_epv.py             # full run (detach for safety)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

_here = Path(__file__).resolve().parent
_backend = _here.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_env_file = _backend / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("scripts.backfill_owners_epv")

import asyncpg  # noqa: E402

from app.scrapers.arcgis_client import query_features  # noqa: E402
from app.scrapers.db import normalize_parcel_number  # noqa: E402
from app.scrapers.fiscal_officer import upsert_parcels  # noqa: E402

_EPV_URL = "https://gis.cuyahogacounty.us/server/rest/services/CCFO/EPV_Prod/FeatureServer/2"
_BATCH_SIZE = 5000  # service maxRecordCount (proven in delinquent_tax.py)
_OUT_FIELDS = ",".join([
    "parcelpin", "parcel_id", "parcel_owner", "par_addr_all",
    "tax_market_total", "grand_total_balance", "foreclosure_flag",
])
_UPSERT_CHUNK = 1000
_MARKET = "cuyahoga"


def _to_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_hit(pin: str, attrs: dict[str, Any]) -> dict[str, Any]:
    owner = (attrs.get("parcel_owner") or "").strip() or None
    situs = (attrs.get("par_addr_all") or "").strip() or None
    return {
        "parcel_number": pin,
        "owner_name": owner,
        "situs_address": situs,
        "current_market_value": _to_float(attrs.get("tax_market_total")),
        "tax_balance_due": _to_float(attrs.get("grand_total_balance")),
        "flag_foreclosure": bool(attrs.get("foreclosure_flag")),
        "source_url": _EPV_URL,
    }


async def run(dry_run: bool) -> None:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3)
    try:
        rows = await pool.fetch(
            "SELECT parcel_number FROM tranchi.parcels "
            "WHERE market = $1 AND owner_name IS NULL",
            _MARKET,
        )
        missing: set[str] = {r["parcel_number"] for r in rows}
        logger.info("Targets: %d Cuyahoga parcels missing owner_name", len(missing))
        if not missing:
            return

        scanned = 0
        matched: set[str] = set()
        pending: list[dict[str, Any]] = []
        totals = {"inserted": 0, "updated": 0, "errors": 0}

        async def _flush() -> None:
            nonlocal pending
            if not pending:
                return
            if dry_run:
                pending = []
                return
            counts = await upsert_parcels(pool, pending, market=_MARKET)
            for k in totals:
                totals[k] += counts.get(k, 0)
            pending = []

        async for batch in query_features(
            _EPV_URL, where="1=1", out_fields=_OUT_FIELDS, batch_size=_BATCH_SIZE
        ):
            for attrs in batch:
                scanned += 1
                raw = attrs.get("parcelpin") or attrs.get("parcel_id")
                pin = normalize_parcel_number(str(raw).strip()) if raw else None
                if not pin or pin not in missing or pin in matched:
                    continue
                if not (attrs.get("parcel_owner") or "").strip():
                    continue  # EPV row exists but carries no owner — leave for the cron
                matched.add(pin)
                pending.append(_to_hit(pin, attrs))
                if len(pending) >= _UPSERT_CHUNK:
                    await _flush()
            if scanned % 50000 < _BATCH_SIZE:
                logger.info("...scanned %d EPV rows, matched %d/%d", scanned, len(matched), len(missing))

        await _flush()

        logger.info("=== EPV OWNER BACKFILL SUMMARY%s ===", " (DRY RUN)" if dry_run else "")
        logger.info("  EPV rows scanned:      %d", scanned)
        logger.info("  targets matched:       %d / %d", len(matched), len(missing))
        logger.info("  not found in EPV:      %d", len(missing) - len(matched))
        if not dry_run:
            logger.info("  upserts:               %s", totals)
            sample = sorted(missing - matched)[:10]
            if sample:
                logger.info("  unmatched sample:      %s", sample)
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="One-time EPV bulk owner backfill (Cuyahoga)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    main()
