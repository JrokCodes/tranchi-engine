"""
Tranchi — Sale-data enrichment backfill.

For parcels with NULL last_sale_date, call the MyPlace Playwright extractor
(fiscal_officer._enrich_sale_data_playwright) and UPDATE tranchi.parcels with
last_sale_date + last_sale_price. Concurrent for speed (default 3 in-flight).

Targets, in order of preference:
  1. Active-probate parcels (the Marc-cared-about set: 5,095).
  2. Active-non-probate parcels.
  3. All remaining (the long tail).

Usage:
  python scripts/enrich_sales.py --limit 50              # quick validation pass
  python scripts/enrich_sales.py --signal probate --limit 200
  python scripts/enrich_sales.py --all-probate-now       # full probate backfill (5,095)
  python scripts/enrich_sales.py --parcels 203-28-051 741-07-013   # ad-hoc

INVARIANT (DO NOT regress): READ from tranchi.listings + tranchi.parcels,
WRITE only to tranchi.parcels (last_sale_date, last_sale_price). No listings
mutations here — that's a separate post-run step (`run.py:_mark_transferred_listings`).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

_here = Path(__file__).resolve().parent
_backend = _here.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

_env = _backend / ".env"
if _env.exists():
    from dotenv import load_dotenv
    load_dotenv(_env)

import asyncpg  # noqa: E402

from app.scrapers.fiscal_officer import _enrich_sale_data_playwright  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("enrich_sales")


async def _pick_targets(
    conn: asyncpg.Connection,
    *,
    signal: str | None,
    all_probate_now: bool,
    limit: int,
) -> list[str]:
    if all_probate_now:
        rows = await conn.fetch(
            """
            SELECT DISTINCT l.source_listing_id AS parcel
            FROM tranchi.listings l
            JOIN tranchi.parcels p ON p.parcel_number = l.source_listing_id
            WHERE l.status = 'active' AND l.signal_type = 'probate'
              -- CROSS-COUNTY GUARD: sale enrichment is Cuyahoga MyPlace (Playwright).
              -- TN parcels would only miss or risk corruption — scope to OH.
              AND l.property_state = 'OH'
              AND l.source_listing_id IS NOT NULL
              AND p.last_sale_date IS NULL
            ORDER BY l.first_seen_at DESC
            """
        )
        return [r["parcel"] for r in rows]

    # CROSS-COUNTY GUARD: sale enrichment is Cuyahoga MyPlace — scope to OH (see above).
    where = "l.status = 'active' AND l.property_state = 'OH' AND l.source_listing_id IS NOT NULL AND p.last_sale_date IS NULL"
    params: list = []
    if signal:
        where += " AND l.signal_type = $1"
        params.append(signal)
    rows = await conn.fetch(
        f"""
        SELECT l.source_listing_id AS parcel
        FROM tranchi.listings l
        JOIN tranchi.parcels p ON p.parcel_number = l.source_listing_id
        WHERE {where}
        ORDER BY random()
        LIMIT {int(limit) * 3}
        """,
        *params,
    )
    # Dedupe preserving order, take first `limit` unique
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        p = r["parcel"]
        if p not in seen:
            seen.add(p)
            out.append(p)
            if len(out) >= limit:
                break
    return out


async def _enrich_one(parcel: str, semaphore: asyncio.Semaphore, conn: asyncpg.Connection) -> dict:
    async with semaphore:
        t0 = time.time()
        try:
            data = await _enrich_sale_data_playwright(parcel)
        except Exception as exc:
            return {"parcel": parcel, "error": str(exc)[:120], "elapsed_s": round(time.time() - t0, 2)}
        result = {
            "parcel": parcel,
            "transfers_present": data.get("transfers_present", False),
            "last_sale_date": data.get("last_sale_date"),
            "last_sale_price": data.get("last_sale_price"),
            "transfer_count": data.get("transfer_count", 0),
            "elapsed_s": round(time.time() - t0, 2),
        }
        if data.get("last_sale_date"):
            try:
                await conn.execute(
                    """
                    UPDATE tranchi.parcels
                    SET last_sale_date = $2,
                        last_sale_price = $3
                    WHERE parcel_number = $1
                    """,
                    parcel,
                    data.get("last_sale_date"),
                    data.get("last_sale_price"),
                )
                result["written"] = True
            except Exception as exc:
                result["error"] = f"write_failed: {str(exc)[:80]}"
        return result


async def run(args) -> int:
    url = os.environ["DATABASE_URL"]
    write_conn = await asyncpg.connect(url)
    read_conn = await asyncpg.connect(url)
    try:
        if args.parcels:
            targets = list(args.parcels)
        else:
            targets = await _pick_targets(
                read_conn,
                signal=args.signal,
                all_probate_now=args.all_probate_now,
                limit=args.limit,
            )
        logger.info("Targets: %d parcels", len(targets))
        if args.dry_run:
            for p in targets[:10]:
                print(p)
            if len(targets) > 10:
                print(f"... ({len(targets) - 10} more)")
            return 0

        sem = asyncio.Semaphore(args.concurrency)
        results = await asyncio.gather(*[_enrich_one(p, sem, write_conn) for p in targets])

        # Summary
        wrote = sum(1 for r in results if r.get("written"))
        transfers = sum(1 for r in results if r.get("transfers_present"))
        no_data = sum(1 for r in results if not r.get("transfers_present") and not r.get("error"))
        errors = sum(1 for r in results if r.get("error"))
        avg = sum(r["elapsed_s"] for r in results) / max(1, len(results))

        print("\n=== ENRICH_SALES SUMMARY ===")
        print(f"  parcels processed:  {len(results)}")
        print(f"  wrote last_sale:    {wrote}")
        print(f"  transfers_present:  {transfers}")
        print(f"  no transfers data:  {no_data}")
        print(f"  errors:             {errors}")
        print(f"  avg per parcel:     {avg:.1f}s   (concurrency={args.concurrency})")

        # Show errors and a sample of the wrote rows
        if errors:
            print("\n  Errors (first 5):")
            for r in [x for x in results if x.get("error")][:5]:
                print(f"    {r['parcel']}: {r['error']}")
        if wrote:
            print("\n  Sample writes (first 5):")
            for r in [x for x in results if x.get("written")][:5]:
                print(f"    {r['parcel']}: last_sale={r['last_sale_date']}  price={r['last_sale_price']}  count={r['transfer_count']}")
        return 0
    finally:
        await write_conn.close()
        await read_conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Tranchi sale-data enrichment backfill")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--signal", type=str, default=None,
                    help="probate | tax_delinquent_foreclosure | mortgage_foreclosure | land_bank_inventory")
    ap.add_argument("--all-probate-now", action="store_true",
                    help="Backfill ALL active probate parcels missing last_sale_date (no limit)")
    ap.add_argument("--parcels", nargs="*", default=None, help="Ad-hoc parcel list")
    ap.add_argument("--concurrency", type=int, default=3, help="Max in-flight Playwright sessions")
    ap.add_argument("--dry-run", action="store_true", help="Print targets, no writes")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
