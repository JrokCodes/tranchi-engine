"""
One-time / on-demand owner backfill from the Shelby tax-delinquency record.

Fills owner_name on owner-less Shelby parcels (alpha-suffix sub-parcels + address-only
foreclosures that never join the ReGIS spine) using the Trustee delinquent-tax lawsuit
'Name' (owner of record), already captured in the tax_delinquent signal payload.

This is the same pass that runs automatically every scrape cycle inside
app.scrapers.run (_backfill_owner_from_delinquency) — this script just lets you run it
once immediately. COALESCE-null-only + market='shelby' scoped (see that function's
docstring for the safety invariants). Idempotent.

Usage:
  python scripts/backfill_owner_from_delinquency.py
  python scripts/backfill_owner_from_delinquency.py --dry-run   # count only, no writes
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
logger = logging.getLogger("scripts.backfill_owner_from_delinquency")

import asyncpg  # noqa: E402

from app.scrapers.run import _backfill_owner_from_delinquency  # noqa: E402

_COUNT_SQL = """
    SELECT count(DISTINCT p.parcel_number)
    FROM tranchi.parcels p
    JOIN tranchi.signals s
      ON s.parcel_number = p.parcel_number AND s.signal_type = 'tax_delinquent'
    WHERE p.market = 'shelby'
      AND (p.owner_name IS NULL OR p.owner_name = '')
      AND NULLIF(btrim(s.payload->>'owner'), '') IS NOT NULL
"""


async def run(dry_run: bool) -> None:
    url = os.environ["DATABASE_URL"]
    pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            recoverable = await conn.fetchval(_COUNT_SQL)
        logger.info("Owner-less Shelby parcels recoverable from delinquency name: %d", recoverable or 0)
        if dry_run:
            logger.info("--dry-run: no writes performed.")
            return
        filled = await _backfill_owner_from_delinquency(pool)
        logger.info("Backfill complete: filled %d parcels.", filled)
    finally:
        await pool.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill owner_name from Shelby tax-delinquency record")
    ap.add_argument("--dry-run", action="store_true", help="count recoverable parcels without writing")
    args = ap.parse_args()
    asyncio.run(run(args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
