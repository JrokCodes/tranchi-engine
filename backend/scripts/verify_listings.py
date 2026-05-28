"""
Listing verifier — the repeatable "is this a real, valid, current lead?" check.

Marc's verification method is human cross-confluence: for a sample of listings,
confirm the property is real, the lead is still live, and (probate) the case is
open — using multiple independent sources so we know the data isn't fabricated.

This script does the parts that are RELIABLE and SCRIPTABLE, and emits the
Redfin/Zillow URLs for the one part that needs a human/browser eyeball (Redfin
and Zillow block scripted access via CloudFront — that check is the on-demand
/verify Playwright step, not an HTTP call).

Per-listing verdict combines:
  1. SOURCE AUTHORITY   — the listing came from an official government feed
                          (DLN county legal journal / probate court / fiscal office).
                          Deterministic scrape of public records → cannot be
                          "hallucinated"; this is the primary validity guarantee.
  2. FRESHNESS          — status='active'; auctions: sale_date >= today; probate:
                          case_status not closed/disposed/terminated/dismissed.
  3. PROPERTY IS REAL   — the parcel exists in tranchi.parcels (independent county
                          fiscal-office record) with an owner + market value. This
                          is a SECOND independent confirmation the address is a real
                          property, separate from the deal source.
  4. JOIN CONFIDENCE    — probate only: match_confidence tier (confirmed/probable/
                          unverified). Unverified = name-only fuzzy join → human check.
  5. NOT FOR SALE       — Redfin/Zillow URL emitted for the manual browser check
                          (off-market = consistent with distress; active MLS = flag).

Usage:
  python scripts/verify_listings.py --sample 20         # mixed sample
  python scripts/verify_listings.py --signal tax_delinquent_foreclosure --limit 10
  python scripts/verify_listings.py --signal probate --limit 10
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import urllib.parse
from pathlib import Path

_here = Path(__file__).resolve().parent
_backend = _here.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_env_file = _backend / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)

import asyncpg  # noqa: E402

_CLOSED = ("closed", "disposed", "terminated", "dismissed")


def _redfin_url(addr: str, city: str | None, zip_: str | None) -> str:
    q = ", ".join(p for p in (addr, city, "OH", zip_) if p)
    return "https://www.redfin.com/?q=" + urllib.parse.quote(q)


def _zillow_url(addr: str, city: str | None, zip_: str | None) -> str:
    q = " ".join(p for p in (addr, city, "OH", zip_) if p)
    return "https://www.zillow.com/homes/" + urllib.parse.quote(q) + "_rb/"


def _myplace_url(parcel: str | None) -> str:
    # Cuyahoga MyPlace: the search box accepts a parcel number. Base URL takes you
    # to the search; paste the parcel to land on the authoritative county record.
    if not parcel:
        return "https://myplace.cuyahogacounty.gov/"
    return f"https://myplace.cuyahogacounty.gov/?parcel={urllib.parse.quote(parcel)}"


def _source_and_check(r: asyncpg.Record) -> tuple[str, str]:
    """Return (source_confirmation_url, what-to-check). Per-signal-type.

    Verification logic:
      probate                       — court case is the source of truth (must still be
                                      OPEN). Parcel + owner on MyPlace confirm the property.
      tax_delinquent_foreclosure    — DLN legal-notice journal published the sale;
                                      MyPlace confirms parcel + delinquency.
      mortgage_foreclosure          — DLN published the sheriff sale; MyPlace confirms parcel.
      land_bank_inventory           — Cuyahoga Land Bank inventory page; still listed = active.

    Commercial-aware: when land_use_code starts with '4' (commercial classes 4000-4999),
    Zillow doesn't cover the asset class — CHECK steers verification to MyPlace + Court,
    not Redfin/Zillow. Same for vacant land (5000-series) when address has no number.
    """
    sig = (r["signal_type"] or "")
    parcel = r["source_listing_id"]
    case = r["case_number"]
    addr_status = r["address_status"]
    land_use_code = (r["land_use_code"] or "")
    is_commercial = land_use_code.startswith("4") if land_use_code else False
    is_vacant_land = land_use_code.startswith("5") and addr_status == "no_street_number"

    # Choose the address hint based on land use + address completeness
    if is_commercial:
        addr_hint = "SKIP Zillow (commercial, not on residential MLS); confirm via MyPlace + Court"
    elif is_vacant_land:
        addr_hint = "verify by PARCEL # (vacant land — county lists no street number)"
    elif addr_status == "no_street_number":
        addr_hint = "verify by PARCEL #, not address (unnumbered)"
    else:
        addr_hint = "address should match"

    if sig == "probate":
        src = f"https://probate.cuyahogacounty.gov/pa/   (search case {case or '?'})"
        chk = (f"(1) ProWare case {case or '?'} still says OPEN  "
               f"(2) MyPlace parcel {parcel} owner matches  "
               f"(3) Redfin/Zillow off-market = good  -- {addr_hint}")
    elif sig in ("tax_delinquent_foreclosure", "mortgage_foreclosure"):
        src = f"https://www.dln.com/   (search '{case or parcel or ''}' or by sale_date)"
        chk = (f"(1) DLN legal notice still lists sale_date={r['sale_date']}  "
               f"(2) MyPlace parcel {parcel} exists + delinquency present  "
               f"(3) Redfin/Zillow off-market = good  -- {addr_hint}")
    elif sig == "land_bank_inventory":
        src = "https://landbank.cuyahogalandbank.org/   (property inventory)"
        chk = (f"(1) Land Bank still lists this property  "
               f"(2) MyPlace parcel {parcel} confirms address  "
               f"(3) Redfin/Zillow off-market = expected (county-owned)  -- {addr_hint}")
    else:
        src = "https://myplace.cuyahogacounty.gov/"
        chk = f"(1) MyPlace parcel {parcel} exists  (2) Redfin/Zillow check  -- {addr_hint}"
    return src, chk


def _verdict(r: asyncpg.Record) -> tuple[str, list[str]]:
    """Return (verdict, notes). VALID / REVIEW / STALE."""
    notes: list[str] = []
    status = r["status"]
    signal = r["signal_type"] or ""
    # Freshness
    fresh = status == "active"
    if not fresh:
        return "STALE", [f"status={status}"]
    if r["sale_date"] is not None and r["sale_date"] < r["today"]:
        return "STALE", ["sale_date in past"]
    cs = (r["case_status"] or "").lower()
    if signal == "probate" and cs and any(w in cs for w in _CLOSED):
        return "STALE", [f"case {r['case_status']}"]

    # Post-filing transfer check (probate only) — county records say the parcel sold
    # at-or-after the case filing year, so the asset is no longer in the estate.
    last_sale = r["last_sale_date"]
    if signal == "probate" and last_sale is not None and (r["case_number"] or "").strip():
        case_num = r["case_number"].strip()
        if len(case_num) >= 4 and case_num[:4].isdigit():
            filing_year = int(case_num[:4])
            if last_sale.year >= filing_year:
                price = r["last_sale_price"]
                price_str = f" for ${int(price):,}" if price else ""
                return "STALE", [f"TRANSFERRED — parcel sold {last_sale}{price_str} (case filed {filing_year})"]

    # Property reality (independent county registry)
    parcel_real = r["owner_name"] is not None
    if parcel_real:
        notes.append(f"parcel real (owner: {r['owner_name']}, mv=${int(r['current_market_value'] or 0):,})")
    else:
        notes.append("NO parcel registry match — confirm address")
    # Probate join confidence
    verdict = "VALID"
    if signal == "probate":
        tier = r["match_confidence"] or "legacy"
        notes.append(f"match={tier}")
        if tier == "unverified":
            verdict = "REVIEW"
            notes.append("name-only fuzzy join — verify owner==decedent")
    # Lead age — only surface for probate where filing year is meaningful
    if signal == "probate" and r["lead_age_days"] is not None:
        notes.append(f"lead age: {int(r['lead_age_days'])}d")
    if not parcel_real and verdict == "VALID":
        verdict = "REVIEW"
    return verdict, notes


async def run(args) -> None:
    url = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(url)
    try:
        where = "l.status = 'active' AND l.duplicate_of IS NULL"
        params: list = []
        if args.signal:
            where += " AND l.signal_type = $1"
            params.append(args.signal)
        limit = args.limit or args.sample or 15
        rows = await conn.fetch(
            f"""
            SELECT l.signal_type, l.source_site, l.property_address, l.property_city,
                   l.property_zip, l.sale_date, l.opening_bid_usd, l.case_number,
                   l.case_status, l.match_confidence, l.match_method, l.status,
                   l.source_listing_id, l.address_status,
                   (CURRENT_DATE - l.first_seen_at::date) AS lead_age_days,
                   p.owner_name, p.current_market_value, p.current_tax_balance,
                   p.land_use_code, p.last_sale_date, p.last_sale_price,
                   CURRENT_DATE AS today
            FROM tranchi.listings l
            LEFT JOIN tranchi.parcels p ON p.parcel_number = l.source_listing_id
            WHERE {where}
            ORDER BY random()
            LIMIT {int(limit)}
            """,
            *params,
        )
        print(f"\n=== VERIFICATION PASS — {len(rows)} listings ===\n")
        counts = {"VALID": 0, "REVIEW": 0, "STALE": 0}
        for i, r in enumerate(rows, 1):
            verdict, notes = _verdict(r)
            counts[verdict] = counts.get(verdict, 0) + 1
            bid = f" bid=${int(r['opening_bid_usd']):,}" if r["opening_bid_usd"] else ""
            sd = f" sale={r['sale_date']}" if r["sale_date"] else ""
            src_url, check = _source_and_check(r)
            print(f"[{i:>2}] {verdict:<6} {r['signal_type']:<26} {r['property_address']}, {r['property_city']} ({r['source_listing_id']}){sd}{bid}")
            print(f"      {' | '.join(notes)}")
            print(f"      Redfin:  {_redfin_url(r['property_address'], r['property_city'], r['property_zip'])}")
            print(f"      Zillow:  {_zillow_url(r['property_address'], r['property_city'], r['property_zip'])}")
            print(f"      MyPlace: {_myplace_url(r['source_listing_id'])}")
            print(f"      Source:  {src_url}")
            print(f"      CHECK:   {check}")
        print("\n" + "=" * 70)
        print(f"  VALID={counts['VALID']}  REVIEW={counts['REVIEW']}  STALE={counts['STALE']}  (of {len(rows)})")
        print("  Manual step: open each Redfin/Zillow link — off-market = consistent with distress (good); active MLS = flag.")
        print("  Confirm at SOURCE: each row's Source URL (court case OPEN / DLN sale still scheduled / Land Bank still listed).")
        print("=" * 70 + "\n")
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Tranchi listing verifier")
    ap.add_argument("--sample", type=int, default=None, help="mixed random sample size")
    ap.add_argument("--signal", type=str, default=None, help="filter to one signal_type")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
