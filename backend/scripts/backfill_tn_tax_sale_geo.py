"""
One-shot backfill: fill Shelby (TN) tax-sale listings' city/zip from the spine.

WHY: the Trustee TaxSaleExtract.csv (shelby_tax_sale source) carries the parcel + a
LOCATION but no clean city/zip, so those listings show NULL property_city/property_zip.
The ReGIS spine (tranchi.parcels.situs_address) has the full "STREET, MEMPHIS, TN 38xxx"
form. This script parses city/zip from the matched parcel's situs_address and writes them
onto the listing — read-only-safe, idempotent (only fills NULLs).

Usage:
  python scripts/backfill_tn_tax_sale_geo.py            # apply
  python scripts/backfill_tn_tax_sale_geo.py --dry-run  # report only
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_tn_tax_sale_geo")

import asyncpg  # noqa: E402

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_CITY_RE = re.compile(r",\s*([A-Za-z .]+?),\s*(?:TN|TENNESSEE)\b", re.IGNORECASE)
_SITES = ("Shelby County Tax Sale",)


def _city_zip(situs: str | None) -> tuple[str | None, str | None]:
    if not situs:
        return None, None
    z = _ZIP_RE.search(situs)
    cm = _CITY_RE.search(situs)
    return (cm.group(1).strip() if cm else None), (z.group(1) if z else None)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        from app.config import settings
        database_url = settings.DATABASE_URL

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    filled_city = filled_zip = scanned = 0
    try:
        rows = await pool.fetch(
            """
            SELECT l.id, l.source_listing_id, p.situs_address
            FROM tranchi.listings l
            JOIN tranchi.parcels p ON p.parcel_number = l.source_listing_id
            WHERE l.source_site = ANY($1::text[])
              AND (l.property_city IS NULL OR l.property_zip IS NULL)
              AND p.situs_address IS NOT NULL AND p.situs_address <> ''
            """,
            list(_SITES),
        )
        logger.info("Scanning %d tax-sale listings missing city/zip with a spine situs", len(rows))
        for r in rows:
            scanned += 1
            city, zip5 = _city_zip(r["situs_address"])
            if not city and not zip5:
                continue
            if args.dry_run:
                if city:
                    filled_city += 1
                if zip5:
                    filled_zip += 1
                continue
            res = await pool.execute(
                """
                UPDATE tranchi.listings
                SET property_city = COALESCE(property_city, $2),
                    property_zip  = COALESCE(property_zip, $3)
                WHERE id = $1
                """,
                r["id"], city, zip5,
            )
            if res and res.split()[-1] == "1":
                if city:
                    filled_city += 1
                if zip5:
                    filled_zip += 1
        logger.info(
            "%sbackfill done: scanned %d, city filled %d, zip filled %d",
            "[DRY RUN] " if args.dry_run else "", scanned, filled_city, filled_zip,
        )
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
