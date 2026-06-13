"""
Summit County (OH) sale-history enrichment — populate tranchi.parcels.last_sale_date.

WHY: Summit's GIS spine carries NO sale history (the Tax_Parcel_Sales layer is a dead
0-row layer), so last_sale_date was NULL for every Summit parcel — which silently disabled
the engine's two off-market guards for this market:
  - run.py::_mark_transferred_listings — a probate (or any) listing whose parcel sold
    at/after its filing year is marked 'transferred' (the estate already sold it).
  - surface_distress.py::_SOLD_GUARD — a pre-distress lead on a parcel sold within 12mo
    is not surfaced / is retired.
Both already work for Cuyahoga/Shelby; they just needed Summit's last_sale_date fed in.

SOURCE: the county CAMA bulk file SC706_SALES (full sale history, ~979K rows, monthly tape)
  https://fiscaloffice.summitoh.net/index.php/documents-a-forms/finish/10-cama/237-sc706sales
  Columns: PARCEL, SALEDATE ('DD-MON-YYYY'), PRICE, OWN1 (new owner), OLDOWN, SALETYPE, ...
  We reduce it to the LATEST sale per parcel and write that date + price onto the spine.
  Matches Cuyahoga's intent (last_sale_date = most recent ownership change); the transfer
  guard decides validity (sold-after-filing), not this loader.

WRITE only to tranchi.parcels (last_sale_date, last_sale_price) for market='summit'. Never
touches listings/signals. Bulk UPDATE via a TEMP table + UPDATE...FROM (≈260K parcels in
one statement — do NOT loop per row). Idempotent: re-running just refreshes to the latest tape.

Run:  python -m scripts.enrich_sales_summit [--dry-run]
Cron: monthly (the tape is monthly), e.g. 30 5 1 * *
"""
from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
import sys
import zipfile
from datetime import datetime, date
from pathlib import Path

_backend = Path(__file__).resolve().parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_env = _backend / ".env"
if _env.exists():
    from dotenv import load_dotenv
    load_dotenv(_env)

import csv

import asyncpg  # noqa: E402
import httpx  # noqa: E402

from app.scrapers.db import normalize_parcel_number  # noqa: E402
from app.scrapers.user_agents import random_ua  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("enrich_sales_summit")

_SC706_URL = (
    "https://fiscaloffice.summitoh.net/index.php/documents-a-forms/finish/10-cama/237-sc706sales"
)
_MARKET = "summit"


def _parse_saledate(s: str | None) -> date | None:
    """'06-OCT-2025' -> date(2025,10,6); tolerant of blanks/garbage."""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d-%b-%Y").date()
    except ValueError:
        return None


def _parse_price(s: str | None) -> float | None:
    if not s:
        return None
    s = s.replace("$", "").replace(",", "").strip()
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def _download_latest_sales() -> dict[str, tuple[date, float | None]]:
    """Download SC706_SALES and reduce to the LATEST sale per normalized parcel.

    Returns {parcel_number(7-digit): (latest_sale_date, price_at_that_sale)}.
    """
    headers = {"User-Agent": random_ua(), "Accept-Language": "en-US,en;q=0.9"}
    logger.info("Downloading SC706_SALES ...")
    with httpx.Client(headers=headers, timeout=120.0, follow_redirects=True) as client:
        resp = client.get(_SC706_URL)
        resp.raise_for_status()
        raw = resp.content
    logger.info("Downloaded %.1f MB; unzipping + parsing ...", len(raw) / 1e6)

    z = zipfile.ZipFile(io.BytesIO(raw))
    name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
    latest: dict[str, tuple[date, float | None]] = {}
    rows = 0
    with z.open(name) as fh:
        reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace"))
        for r in reader:
            rows += 1
            pid = normalize_parcel_number((r.get("PARCEL") or "").strip())
            sd = _parse_saledate(r.get("SALEDATE"))
            if not pid or sd is None:
                continue
            cur = latest.get(pid)
            if cur is None or sd > cur[0]:
                latest[pid] = (sd, _parse_price(r.get("PRICE")))
    logger.info("Parsed %d sale rows -> %d parcels with a usable latest sale.", rows, len(latest))
    return latest


async def run(dry_run: bool) -> int:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        from app.config import settings
        dsn = settings.DATABASE_URL

    latest = _download_latest_sales()
    if not latest:
        logger.warning("No sales parsed — aborting (no writes).")
        return 1

    records = [(pid, sd, price) for pid, (sd, price) in latest.items()]

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        async with pool.acquire() as conn:
            # How many Summit parcels would this touch (intersect with the spine)?
            present = await conn.fetchval(
                "SELECT count(*) FROM tranchi.parcels WHERE market=$1", _MARKET
            )
            if dry_run:
                # Count parcels we have a sale for that exist in the Summit spine.
                got = await conn.fetchval(
                    "SELECT count(*) FROM tranchi.parcels p "
                    "WHERE p.market=$1 AND p.parcel_number = ANY($2::text[])",
                    _MARKET, list(latest.keys()),
                )
                logger.info("[DRY] Summit spine parcels=%d; would set last_sale_date on %d.",
                            present, got)
                return 0

            # Bulk path: stage into a TEMP table, then one UPDATE...FROM scoped to market.
            await conn.execute(
                "CREATE TEMP TABLE _summit_sales (parcel_number text PRIMARY KEY, "
                "sale_date date, price numeric) ON COMMIT DROP"
            )
            await conn.copy_records_to_table("_summit_sales", records=records,
                                             columns=["parcel_number", "sale_date", "price"])
            tag = await conn.execute(
                """
                UPDATE tranchi.parcels p
                   SET last_sale_date  = t.sale_date,
                       last_sale_price = COALESCE(t.price, p.last_sale_price)
                  FROM _summit_sales t
                 WHERE p.parcel_number = t.parcel_number
                   AND p.market = $1
                   AND (p.last_sale_date IS DISTINCT FROM t.sale_date)
                """,
                _MARKET,
            )
            updated = int((tag or "0").split()[-1])
            covered = await conn.fetchval(
                "SELECT count(*) FROM tranchi.parcels WHERE market=$1 AND last_sale_date IS NOT NULL",
                _MARKET,
            )
            logger.info("Summit spine parcels=%d; updated %d this run; total with last_sale_date=%d.",
                        present, updated, covered)
    finally:
        await pool.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
