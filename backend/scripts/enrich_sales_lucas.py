"""
Lucas (OH / Toledo) sales enrichment — fills tranchi.parcels.last_sale_date + last_sale_price
from the public AREIS database SALES table (the same .mdb item lucas_areis_delinquent reads).

WHY: the universal scripts/enrich_sales.py is Cuyahoga-MyPlace-HARDCODED and cannot enrich
Lucas. Without last_sale_date the surface_distress SOLD-GUARD is a no-op for Lucas, so a parcel
that already transferred can still surface as a distress lead. Per Jayden's handoff §5, the
sold-guard is "auto-kill anything already transferred" — this is its Lucas data source.

Latest transfer per parcel wins (max SalesDate). MONTHLY cadence (the AREIS DB updates ~monthly).

Run:  python scripts/enrich_sales_lucas.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import logging
import os
import subprocess
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path

_backend = Path(__file__).resolve().parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_env = _backend / ".env"
if _env.exists():
    from dotenv import load_dotenv
    load_dotenv(_env)

import asyncpg  # noqa: E402
import httpx  # noqa: E402

from app.scrapers.db import normalize_parcel_for_market  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("enrich_sales_lucas")

_ITEM = "8e00e957fcc04a81aac77e8bfc17b2dc"
_DATA_URL = f"https://www.arcgis.com/sharing/rest/content/items/{_ITEM}/data"


def _parse_date(s: str | None) -> date | None:
    s = (s or "").strip()
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def _download_and_parse_sales() -> dict[str, tuple[date, float]]:
    """{ parcel_number : (latest_sale_date, sale_amount) } for Lucas parcels."""
    with tempfile.TemporaryDirectory(prefix="areis_sales_") as td:
        zp = Path(td) / "a.zip"
        with httpx.stream("GET", _DATA_URL, timeout=300, follow_redirects=True) as r:
            r.raise_for_status()
            with open(zp, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
        with zipfile.ZipFile(zp) as z:
            mdb = next((n for n in z.namelist() if n.lower().endswith((".mdb", ".accdb"))), None)
            if not mdb:
                raise RuntimeError("no .mdb in AREIS zip")
            z.extract(mdb, td)
        proc = subprocess.run(
            ["mdb-export", str(Path(td) / mdb), "SALES"],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"mdb-export SALES failed: {(proc.stderr or '')[:200]}")
        reader = csv.DictReader(io.StringIO(proc.stdout))
        latest: dict[str, tuple[date, float]] = {}
        rows = 0
        for row in reader:
            rows += 1
            raw = (row.get("Parcel") or "").strip()
            d = _parse_date(row.get("SalesDate"))
            if not raw or not d:
                continue
            parcel = normalize_parcel_for_market(raw, "lucas")
            if not parcel:
                continue
            try:
                amt = float(row.get("SaleAmount") or 0)
            except (TypeError, ValueError):
                amt = 0.0
            cur = latest.get(parcel)
            if cur is None or d > cur[0]:
                latest[parcel] = (d, amt)
        logger.info("SALES: %d rows -> %d parcels with a latest transfer", rows, len(latest))
        return latest


async def main(dry_run: bool) -> None:
    latest = await asyncio.to_thread(_download_and_parse_sales)
    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        async with pool.acquire() as conn:
            spine = await conn.fetchval("SELECT count(*) FROM tranchi.parcels WHERE market='lucas'")
            in_spine = await conn.fetchval(
                "SELECT count(*) FROM tranchi.parcels WHERE market='lucas' AND parcel_number = ANY($1::text[])",
                list(latest.keys()),
            )
            logger.info("spine lucas parcels=%d ; SALES parcels matching spine=%d", spine, in_spine)
            if dry_run:
                # how many leads sit on a parcel transferred within 12 months (the sold-guard target)
                recent = [p for p, (d, _a) in latest.items()
                          if (d.toordinal() > (date.today().toordinal() - 365))]
                hits = await conn.fetchval(
                    "SELECT count(*) FROM tranchi.listings l WHERE l.market='lucas' AND l.status='active' "
                    "AND l.distress_stage='distress_signal' AND l.source_listing_id = ANY($1::text[])",
                    recent,
                )
                logger.info("[DRY] parcels transferred <12mo: %d ; active leads on them (sold-guard would drop on next surface): %d",
                            len(recent), hits)
                return
            async with conn.transaction():
                await conn.execute(
                    "CREATE TEMP TABLE _lucas_sales (parcel text, sale_date date, sale_price numeric) ON COMMIT DROP"
                )
                await conn.copy_records_to_table(
                    "_lucas_sales",
                    records=[(p, d, amt) for p, (d, amt) in latest.items()],
                )
                res = await conn.execute(
                    "UPDATE tranchi.parcels p SET last_sale_date=s.sale_date, last_sale_price=s.sale_price "
                    "FROM _lucas_sales s "
                    "WHERE p.market='lucas' AND p.parcel_number=s.parcel "
                    "  AND (p.last_sale_date IS DISTINCT FROM s.sale_date OR p.last_sale_price IS DISTINCT FROM s.sale_price)"
                )
                logger.info("UPDATE parcels: %s", res)
    finally:
        await pool.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run))
