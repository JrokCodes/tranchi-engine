"""
Tranchi — Shelby (Memphis, TN) sale-date enrichment backfill.

Shelby has NO bulk sale source (ReGIS spine has no sale field; county GIS +
data.shelbycountytn.gov are Cloudflare-403; the Trustee page has no sale date).
The reliable source is the Assessor (assessormelvinburgess.com): a per-parcel
httpx GET (NO Playwright/JS needed) returns the parcel's full sale history as
raw HTML inside <tbody id="salesBody">.

  GET https://www.assessormelvinburgess.com/propertyDetails?IR=true&parcelid=<SPACED native_parcel_id>
      headers: browser User-Agent + Referer  (else 404 / WAF block)

salesBody columns (newest-first): Date of Sale (MM/DD/YYYY) | Sales Price ($N,NNN)
| Deed# | Instrument Type. The Instrument Type is a DEED-TYPE code (WD/SW/QC/A/DN/
CH/CA/TD/UN...), NOT an arms-length flag — the recipe's "A=arms-length" was a
misread: live 'A' rows are $0 admin/split deeds, real arms-length sales are WD with
price>0. So the load-bearing signal is PRICE>0: we take the most-recent sale with
price>0 as last_sale_date+last_sale_price. This naturally skips $0 intra-family QC /
admin transfers so a probate estate's $0 transfer to heirs does NOT falsely retire
the lead. If no price>0 sale exists, leave last_sale_date NULL (parcel stays a valid
lead; surface _SOLD_GUARD keeps NULL). (mulch: tranchi "Shelby Assessor salesBody
instrument codes (recipe correction)".)

TARGETED to active Shelby listing parcels + Shelby signal (pre-distress lead)
parcels only (~26K), NEVER the 353K spine (--all-spine would take ~2yr). The shared
3h guards (run.py:_mark_transferred_listings + surface_distress _SOLD_GUARD) then
auto-kill sold ones once last_sale_date lands — no guard code change here.

INVARIANT (DO NOT regress, mirrors enrich_sales.py): READ from tranchi.listings +
tranchi.signals + tranchi.parcels, WRITE only to tranchi.parcels (last_sale_date,
last_sale_price). No listings mutations here.

Usage:
  python scripts/enrich_sales_shelby.py --limit 50            # quick validation pass
  python scripts/enrich_sales_shelby.py --dry-run --limit 20  # show targets, no fetch
  python scripts/enrich_sales_shelby.py --all                 # full backfill (unbounded)
  python scripts/enrich_sales_shelby.py --parcels 04105500033000 ...  # ad-hoc (canonical pn)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
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
import httpx  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("enrich_sales_shelby")

_MARKET = "shelby"
_ASSESSOR_URL = "https://www.assessormelvinburgess.com/propertyDetails"
_HEADERS = {
    # Browser UA + Referer are REQUIRED — the Assessor WAF 404s a bare client.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.assessormelvinburgess.com/",
}

_SALESBODY_RE = re.compile(r'<tbody[^>]*id="salesBody"[^>]*>(.*?)</tbody>', re.S | re.I)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _cell_text(raw: str) -> str:
    return _TAG_RE.sub("", raw).replace("&nbsp;", " ").strip()


def _parse_price(s: str) -> Decimal | None:
    """'$2,100' -> Decimal('2100'); '$0' -> Decimal('0'); '' -> None."""
    cleaned = re.sub(r"[^0-9.]", "", s or "")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_date(s: str) -> date | None:
    """'08/26/2005' -> date(2005, 8, 26); junk -> None.

    Rejects IMPLAUSIBLE dates (source typos seen live: year 2525, 0953, 0210, or a
    future date like 2026-07-05) -> None. A garbage future/absurd date would
    otherwise sort as the 'latest' priced sale in _pick_last_sale and falsely retire
    a live lead via the transfer guard.
    """
    s = (s or "").strip()
    try:
        d = datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        return None
    if d.year < 1900 or d > date.today():
        return None
    return d


def _parse_salesbody(html_text: str) -> list[dict]:
    """Extract salesBody rows as dicts: {sale_date, price, deed, instrument}."""
    m = _SALESBODY_RE.search(html_text)
    if not m:
        return []
    out: list[dict] = []
    for tr in _TR_RE.findall(m.group(1)):
        cells = [_cell_text(td) for td in _TD_RE.findall(tr)]
        if len(cells) < 2:
            continue
        out.append(
            {
                "sale_date": _parse_date(cells[0]),
                "price": _parse_price(cells[1]) if len(cells) > 1 else None,
                "deed": cells[2] if len(cells) > 2 else None,
                "instrument": cells[3] if len(cells) > 3 else None,
            }
        )
    return out


def _pick_last_sale(rows: list[dict]) -> tuple[date | None, Decimal | None]:
    """Most-recent sale with price>0 (the arms-length 'sold' signal).

    Keying on price>0 (not the instrument code) excludes $0 intra-family QC / admin
    'A' transfers so a probate estate's $0 transfer to heirs does NOT falsely retire
    the lead. Returns (None, None) when no priced sale exists -> last_sale_date stays
    NULL (kept as a valid lead by the off-market guard).
    """
    priced = [r for r in rows if r["sale_date"] and r["price"] and r["price"] > 0]
    if not priced:
        return None, None
    best = max(priced, key=lambda r: r["sale_date"])
    return best["sale_date"], best["price"]


async def _pick_targets(
    conn: asyncpg.Connection, *, limit: int
) -> list[tuple[str, str]]:
    """Active Shelby listing parcels + Shelby signal parcels missing last_sale_date.

    Returns (parcel_number, native_parcel_id). Probate-listing parcels first (where a
    false retirement hurts most), then the rest. native_parcel_id is required to drive
    the Assessor request, so parcels without it are skipped (should be <0.1%).
    limit<=0 => unbounded (the full backfill / daily cron mode).
    """
    lim = "" if limit <= 0 else f"LIMIT {int(limit)}"
    rows = await conn.fetch(
        f"""
        WITH targets AS (
            SELECT source_listing_id AS pn
            FROM tranchi.listings
            WHERE market = $1 AND status = 'active' AND source_listing_id IS NOT NULL
            UNION
            SELECT parcel_number AS pn
            FROM tranchi.signals
            WHERE market = $1
        )
        SELECT p.parcel_number AS parcel, p.native_parcel_id AS native
        FROM targets t
        JOIN tranchi.parcels p
          ON p.parcel_number = t.pn AND p.market = $1
        WHERE p.last_sale_date IS NULL
          AND p.native_parcel_id IS NOT NULL
        ORDER BY (EXISTS (
                   SELECT 1 FROM tranchi.listings l
                   WHERE l.source_listing_id = p.parcel_number
                     AND l.market = $1 AND l.status = 'active'
                     AND l.signal_type = 'probate')) DESC,
                 p.parcel_number
        {lim}
        """,
        _MARKET,
    )
    return [(r["parcel"], r["native"]) for r in rows]


async def _native_ids_for(
    conn: asyncpg.Connection, parcels: list[str]
) -> list[tuple[str, str]]:
    rows = await conn.fetch(
        """
        SELECT parcel_number AS parcel, native_parcel_id AS native
        FROM tranchi.parcels
        WHERE market = $1 AND parcel_number = ANY($2::text[])
          AND native_parcel_id IS NOT NULL
        """,
        _MARKET,
        parcels,
    )
    return [(r["parcel"], r["native"]) for r in rows]


async def _enrich_one(
    parcel: str,
    native: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    conn: asyncpg.Connection,
    write_lock: asyncio.Lock,
    *,
    throttle_s: float,
) -> dict:
    async with semaphore:
        t0 = time.time()
        try:
            r = await client.get(_ASSESSOR_URL, params={"IR": "true", "parcelid": native})
        except Exception as exc:
            return {"parcel": parcel, "error": f"fetch:{str(exc)[:90]}", "elapsed_s": round(time.time() - t0, 2)}
        finally:
            if throttle_s:
                await asyncio.sleep(throttle_s)
        if r.status_code != 200:
            return {"parcel": parcel, "error": f"http_{r.status_code}", "elapsed_s": round(time.time() - t0, 2)}
        rows = _parse_salesbody(r.text)
        last_date, last_price = _pick_last_sale(rows)
        result = {
            "parcel": parcel,
            "rows": len(rows),
            "last_sale_date": last_date,
            "last_sale_price": last_price,
            "elapsed_s": round(time.time() - t0, 2),
        }
        if last_date is not None:
            try:
                # asyncpg forbids concurrent ops on one connection; serialize the
                # writes (fetches stay concurrent under the semaphore).
                async with write_lock:
                    await conn.execute(
                        """
                        UPDATE tranchi.parcels
                        SET last_sale_date = $2,
                            last_sale_price = $3
                        WHERE parcel_number = $1 AND market = $4
                        """,
                        parcel,
                        last_date,
                        last_price,
                        _MARKET,
                    )
                result["written"] = True
            except Exception as exc:
                result["error"] = f"write:{str(exc)[:80]}"
        return result


def _summarize(results: list[dict], concurrency: int) -> None:
    wrote = sum(1 for r in results if r.get("written"))
    no_sale = sum(1 for r in results if not r.get("error") and not r.get("written"))
    errors = sum(1 for r in results if r.get("error"))
    avg = sum(r.get("elapsed_s", 0) for r in results) / max(1, len(results))
    print("\n=== ENRICH_SALES_SHELBY SUMMARY ===")
    print(f"  parcels processed:  {len(results)}")
    print(f"  wrote last_sale:    {wrote}")
    print(f"  no priced sale:     {no_sale}   (left NULL — stays a lead)")
    print(f"  errors:             {errors}")
    print(f"  avg per parcel:     {avg:.2f}s   (concurrency={concurrency})")
    if errors:
        print("\n  Errors (first 5):")
        for r in [x for x in results if x.get("error")][:5]:
            print(f"    {r['parcel']}: {r['error']}")
    if wrote:
        print("\n  Sample writes (first 5):")
        for r in [x for x in results if x.get("written")][:5]:
            print(f"    {r['parcel']}: last_sale={r['last_sale_date']}  price={r['last_sale_price']}  rows={r['rows']}")


async def run(args) -> int:
    url = os.environ["DATABASE_URL"]
    # asyncpg needs a plain postgres:// URL — strip the SQLAlchemy +asyncpg driver tag.
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    write_conn = await asyncpg.connect(url)
    read_conn = await asyncpg.connect(url)
    try:
        if args.parcels:
            targets = await _native_ids_for(read_conn, list(args.parcels))
        else:
            targets = await _pick_targets(read_conn, limit=args.limit)
        logger.info("Targets: %d parcels (market=%s)", len(targets), _MARKET)
        if args.dry_run:
            for parcel, native in targets[:10]:
                print(f"{parcel}  (native={native!r})")
            if len(targets) > 10:
                print(f"... ({len(targets) - 10} more)")
            return 0
        if not targets:
            print("No eligible parcels (all have last_sale_date or lack native_parcel_id).")
            return 0

        sem = asyncio.Semaphore(args.concurrency)
        write_lock = asyncio.Lock()
        results: list[dict] = []
        async with httpx.AsyncClient(
            headers=_HEADERS, timeout=args.timeout, follow_redirects=True
        ) as client:
            # Chunked so a multi-hour backfill logs steady progress (and bounds
            # the number of live coroutines).
            chunk = max(args.concurrency * 25, 100)
            for i in range(0, len(targets), chunk):
                batch = targets[i : i + chunk]
                batch_results = await asyncio.gather(
                    *[
                        _enrich_one(p, n, client, sem, write_conn, write_lock, throttle_s=args.throttle)
                        for p, n in batch
                    ]
                )
                results.extend(batch_results)
                wrote = sum(1 for r in results if r.get("written"))
                logger.info(
                    "Progress: %d/%d processed, %d written",
                    len(results), len(targets), wrote,
                )

        _summarize(results, args.concurrency)
        return 0
    finally:
        await write_conn.close()
        await read_conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Tranchi Shelby sale-date enrichment (Assessor salesBody, httpx)")
    ap.add_argument("--limit", type=int, default=50, help="Max parcels (default 50; use --all for unbounded)")
    ap.add_argument("--all", dest="all_targets", action="store_true",
                    help="Backfill ALL eligible Shelby target parcels (limit 0 = unbounded). Cron mode.")
    ap.add_argument("--parcels", nargs="*", default=None, help="Ad-hoc canonical parcel_number list")
    ap.add_argument("--concurrency", type=int, default=4, help="Max in-flight Assessor requests (default 4)")
    ap.add_argument("--throttle", type=float, default=0.25, help="Per-request sleep, seconds (politeness; default 0.25)")
    ap.add_argument("--timeout", type=float, default=30.0, help="Per-request HTTP timeout, seconds")
    ap.add_argument("--dry-run", action="store_true", help="Print targets, no fetch/writes")
    args = ap.parse_args()
    if args.all_targets:
        args.limit = 0
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
