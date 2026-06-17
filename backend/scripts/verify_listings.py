"""
Listing verifier — the repeatable "is this a real, valid, current lead?" check.

Marc's verification method is human cross-confluence: for a sample of listings,
confirm the property is real, the lead is still live, and (probate) the case is
open — using multiple independent sources so we know the data isn't fabricated.

This script does the parts that are RELIABLE and SCRIPTABLE, and emits the
Redfin/Zillow URLs for the one part that needs a human/browser eyeball (Redfin
and Zillow block scripted access via CloudFront — that check is the on-demand
/verify browser step, not an HTTP call).

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
  python scripts/verify_listings.py --sample 15                # mixed random sample, console (Cuyahoga default)
  python scripts/verify_listings.py --stratified 3             # 3 per deal source = 12 + 3 fill = 15
  python scripts/verify_listings.py --stratified 3 --html out.html   # Marc-presentable HTML walk-through
  python scripts/verify_listings.py --signal probate --limit 10

  # Shelby (Memphis) market:
  python scripts/verify_listings.py --market shelby --sample 5
  python scripts/verify_listings.py --market shelby --stratified 3 --html /tmp/shelby-verify.html
"""
from __future__ import annotations

import argparse
import asyncio
import html as _html
import os
import sys
import urllib.parse
from datetime import datetime
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

# Per-market config (verification guides, registry/source/off-market link builders, deal_sources,
# source_sites) lives in app/market_config.py — the single home for market-specific values.
# To add a market: add an entry to MARKETS there.
from app.market_config import (  # noqa: E402
    GENERAL_GUIDE,
    MARKETS,
    _trustee_parcel_url,
)

_CLOSED = ("closed", "disposed", "terminated", "dismissed")

_SELECT_COLS = """
    l.id, l.signal_type, l.source_site, l.property_address, l.property_city,
    l.property_zip, l.sale_date, l.opening_bid_usd, l.appraised_value_usd, l.case_number,
    l.case_status, l.match_confidence, l.match_method, l.status,
    l.source_listing_id, l.address_status, l.auction_status,
    l.decedent_name, l.case_title, l.decedent_dod,
    l.conviction_tier, l.blight_ticket_count, l.blight_total_balance, l.absentee_owner,
    (CURRENT_DATE - l.first_seen_at::date) AS lead_age_days,
    p.owner_name, p.current_market_value, p.current_tax_balance,
    p.land_use_code, p.last_sale_date, p.last_sale_price,
    p.native_parcel_id,
    CURRENT_DATE AS today
"""

# Parcel join is market-scoped on the county-level `market` (migration 014) so a listing
# can only enrich from a parcel in its OWN market — structurally prevents the cross-market
# join bug class even for a same-state second county whose per-county parcel numbers could
# collide (the #10 guarantee; formats don't collide across the OH/TN markets today).
_FROM_JOIN = """
    FROM tranchi.listings l
    LEFT JOIN tranchi.parcels p
      ON p.parcel_number = l.source_listing_id AND p.market = l.market
"""


# ---------------------------------------------------------- Console formatting


def _source_and_check(r: asyncpg.Record, market: dict) -> tuple[str, str]:
    """Return (source_confirmation_url, what-to-check) for CONSOLE output."""
    sig = (r["signal_type"] or "")
    parcel = r["source_listing_id"]
    case = r["case_number"]
    addr_status = r["address_status"]
    land_use_code = (r["land_use_code"] or "")
    is_commercial = land_use_code.startswith("4") if land_use_code else False
    is_vacant_land = land_use_code.startswith("5") and addr_status == "no_street_number"

    if is_commercial:
        addr_hint = "SKIP Zillow (commercial, not on residential MLS); confirm via county registry + court/source"
    elif is_vacant_land:
        addr_hint = "verify by PARCEL # (vacant land — county lists no street number)"
    elif addr_status == "no_street_number":
        addr_hint = "verify by PARCEL #, not address (unnumbered)"
    else:
        addr_hint = "address should match"

    registry_label = market["registry_label"].split(" — ")[0]  # short label for console
    native_parcel_id = r["native_parcel_id"] if "native_parcel_id" in r.keys() else None
    src = market["source_link"](sig, parcel, case, r["sale_date"], r["property_address"],
                                native_parcel_id=native_parcel_id)

    if market["state"] == "OH":
        # Cuyahoga-specific console CHECK lines (preserving original behavior)
        if sig == "probate":
            chk = (f"(1) ProWare case {case or '?'} still says OPEN  "
                   f"(2) MyPlace parcel {parcel} owner matches  "
                   f"(3) Redfin/Zillow off-market = good  -- {addr_hint}")
        elif sig in ("tax_delinquent_foreclosure", "mortgage_foreclosure"):
            chk = (f"(1) DLN legal notice still lists sale_date={r['sale_date']}  "
                   f"(2) MyPlace parcel {parcel} exists + delinquency present  "
                   f"(3) Redfin/Zillow off-market = good  -- {addr_hint}")
        elif sig == "forfeited_land":
            chk = (f"(1) Parcel {parcel} still on the Forfeited Lands locator  "
                   f"(2) MyPlace owner = 'STATE OF OHIO FORF' (already forfeited)  "
                   f"(3) equity = market value - opening bid (tax+costs) is POSITIVE  "
                   f"(4) Redfin/Zillow off-market = expected (county-controlled)  -- {addr_hint}")
        elif sig == "land_bank_inventory":
            chk = (f"(1) Land Bank still lists this property  "
                   f"(2) MyPlace parcel {parcel} confirms address  "
                   f"(3) Redfin/Zillow off-market = expected (county-owned)  -- {addr_hint}")
        elif sig == "tax_delinquent":
            chk = (f"PRE-DISTRESS LEAD: (1) MyPlace parcel {parcel}: delinquent balance owed (>=$2k/foreclosure), owner = private party  "
                   f"(2) NOT on the DLN sheriff-sale feed (else it's a buy-now deal, not a lead)  "
                   f"(3) Owner not recently transferred (still the distressed owner)  "
                   f"(4) Redfin/Zillow OFF-market = good (motivated, not yet listed)  -- {addr_hint}")
        else:
            chk = f"(1) MyPlace parcel {parcel} exists  (2) Redfin/Zillow check  -- {addr_hint}"
    elif market["state"] == "MI":
        # Wayne (Detroit) console CHECK lines — pto.waynecounty.com is the authority.
        # MI mortgage foreclosure: 6-month redemption (MCL 600.3240) — a PAST sale_date is
        # NOT stale while still inside the window (tagged 'in_redemption' in auction_status).
        # Never flag in-redemption rows as STALE based on sale_date alone.
        if sig == "mortgage_foreclosure":
            chk = (f"(1) mipublicnotices.com: foreclosure notice still published OR sale_date within 6mo (MI in-redemption = VALID)  "
                   f"(2) pto.waynecounty.com parcel {parcel}: current owner = private party (not lender/new buyer)  "
                   f"(3) NOTE: a past sale_date is NOT stale — in-redemption rows are live leads  "
                   f"(4) Redfin/Zillow off-market = good  -- {addr_hint}")
        elif sig == "tax_delinquent_foreclosure":
            chk = (f"(1) Wayne Treasurer auction: parcel {parcel} still active (STATUS_CD != RM = not removed/redeemed)  "
                   f"(2) Equity = market value − min bid (NO post-sale redemption on tax-deed — clean title)  "
                   f"(3) pto.waynecounty.com confirms delinquency still owed  "
                   f"(4) Redfin/Zillow off-market = good  -- {addr_hint}")
        elif sig == "land_bank_inventory":
            chk = (f"(1) DLBA (buildingdetroit.org) or WCLB (waynecountylandbank.com): parcel {parcel} still listed (county/city-owned)  "
                   f"(2) Off-market expected (government-owned — NOT on MLS)  "
                   f"(3) Redfin/Zillow off-market = expected (county/city-owned)  -- {addr_hint}")
        elif sig == "tax_delinquent":
            chk = (f"PRE-DISTRESS LEAD: (1) pto.waynecounty.com parcel {parcel}: balance owed (forfeiture roll), owner = private party  "
                   f"(2) NOT on the Treasurer auction (else it's a buy-now deal, not a lead)  "
                   f"(3) Owner not recently transferred (still the distressed owner)  "
                   f"(4) Redfin/Zillow OFF-market = good (motivated, not yet listed)  -- {addr_hint}")
        elif sig == "blight_violation":
            tier = r["conviction_tier"] or "?"
            tcount = r["blight_ticket_count"]
            tbal = r["blight_total_balance"]
            absentee = " absentee" if r["absentee_owner"] else ""
            tier_hint = f"[Tier {tier}: {tcount} tickets, ${tbal}{absentee}] " if tcount is not None else ""
            chk = (f"PRE-DISTRESS LEAD {tier_hint}: (1) Detroit blight record: amt_balance_due > 0, disposition 'Responsible%', "
                   f"In Collections on parcel {parcel} (the floor — confirm it holds)  "
                   f"(2) Owner not recently transferred (Detroit assessor/pto still the distressed owner)  "
                   f"(3) Redfin/Zillow OFF-market = good (not listing despite neglect)  -- {addr_hint}")
        else:
            chk = f"(1) pto.waynecounty.com parcel {parcel} exists  (2) Redfin/Zillow check  -- {addr_hint}"
    else:
        # Shelby console CHECK lines — Trustee page is the universal verifier
        trustee_url = src if "shelbycountytrustee.com/Parcel" in src else "https://apps2.shelbycountytrustee.com/PropertyClient"
        if sig == "tax_deed":
            chk = (f"(1) Trustee page: TAX DUE > $0 AND 'Tax Sale Property Notice' block present (TS code {case or '?'})  "
                   f"(2) PRIMARY OWNER = private party (not county/land bank)  "
                   f"(3) Redfin/Zillow off-market = good (pre-auction)  -- {addr_hint}")
        elif sig == "land_bank_inventory":
            source_site = (r["source_site"] or "")
            if "MMLBA" in source_site:
                chk = (f"(1) Trustee page: PRIMARY OWNER = 'MEMPHIS METROPOLITAN LAND BANK AUTHORITY'  "
                       f"(2) On MMLBA available list (mmlba.org/property-sales/)  "
                       f"(3) Redfin/Zillow off-market = expected (city-owned)  -- {addr_hint}")
            else:
                chk = (f"(1) Trustee page: PRIMARY OWNER = 'SHELBY COUNTY TAX SALE' or similar  "
                       f"(2) On Shelby Land Bank portal (ePropertyPlus — search parcel {parcel})  "
                       f"(3) Redfin/Zillow off-market = expected (county-owned)  -- {addr_hint}")
        elif sig == "tax_delinquent":
            chk = (f"PRE-DISTRESS LEAD: (1) Trustee page: TAX DUE > $0, PRIMARY OWNER = private party (not county)  "
                   f"(2) Owner not recently transferred (still the distressed owner)  "
                   f"(3) Redfin/Zillow OFF-market = good (motivated, not yet listed)  -- {addr_hint}")
        elif sig == "eviction":
            chk = (f"PRE-DISTRESS LEAD (tired landlord): (1) Trustee page: parcel {parcel} owner matches, not recently sold  "
                   f"(2) Redfin/Zillow OFF-market = good (weaker off-market than tax-distress — check this)  -- {addr_hint}")
        else:
            chk = f"(1) Trustee page: parcel {parcel} exists, owner matches  (2) Redfin/Zillow check  -- {addr_hint}"

    return src, chk


# ------------------------------------------------------------------ Verdict


def _verdict(r: asyncpg.Record) -> tuple[str, list[str]]:
    """Return (verdict, notes). VALID / REVIEW / STALE."""
    notes: list[str] = []
    status = r["status"]
    signal = r["signal_type"] or ""
    fresh = status == "active"
    if not fresh:
        return "STALE", [f"status={status}"]
    if r["sale_date"] is not None and r["sale_date"] < r["today"]:
        # MI mortgage foreclosure: a sheriff-deed is NOT final at sale — the owner redeems
        # for 6 months (MCL 600.3240). wayne_foreclosure tags these auction_status=
        # 'in_redemption'; they are LIVE off-market leads, not stale. (Only MI sets this tag;
        # OH foreclosures stay 'scheduled'/NULL and still go STALE on a past sale_date.)
        if (r["auction_status"] or "") != "in_redemption":
            return "STALE", ["sale_date in past"]
        notes.append("in redemption (MI 6mo, MCL 600.3240) — live lead, sale_date past is expected")
    cs = (r["case_status"] or "").lower()
    if signal == "probate" and cs and any(w in cs for w in _CLOSED):
        return "STALE", [f"case {r['case_status']}"]

    # Post-filing transfer check (probate only).
    last_sale = r["last_sale_date"]
    if signal == "probate" and last_sale is not None and (r["case_number"] or "").strip():
        case_num = r["case_number"].strip()
        if len(case_num) >= 4 and case_num[:4].isdigit():
            filing_year = int(case_num[:4])
            if last_sale.year >= filing_year:
                price = r["last_sale_price"]
                price_str = f" for ${int(price):,}" if price else ""
                return "STALE", [f"TRANSFERRED — parcel sold {last_sale}{price_str} (case filed {filing_year})"]

    parcel_real = r["owner_name"] is not None
    if parcel_real:
        notes.append(f"parcel real (owner: {r['owner_name']}, mv=${int(r['current_market_value'] or 0):,})")
    else:
        notes.append("NO parcel registry match — confirm address")
    verdict = "VALID"
    if signal == "probate":
        tier = r["match_confidence"] or "legacy"
        notes.append(f"match={tier}")
        if tier == "unverified":
            verdict = "REVIEW"
            notes.append("name-only fuzzy join — verify owner==decedent")
    if signal == "probate" and r["lead_age_days"] is not None:
        notes.append(f"lead age: {int(r['lead_age_days'])}d")
    if not parcel_real and verdict == "VALID":
        verdict = "REVIEW"
    return verdict, notes


# --------------------------------------------------------- Structured layers


def _layers(r: asyncpg.Record, market: dict) -> dict:
    """Build the 3-layer structured data for HTML rendering.

    Layer 1 = the deal-source page (probate court / DLN / Land Bank / Trustee).
    Layer 2 = the county parcel registry (MyPlace for Cuyahoga; Assessor for Shelby).
    Layer 3 = the off-market human eyeball (Zillow + Redfin), with commercial /
              vacant-land overrides.
    """
    sig = r["signal_type"] or ""
    source_site = r["source_site"] or ""
    parcel = r["source_listing_id"] or "?"
    case = r["case_number"]
    addr_status = r["address_status"]
    land_use_code = (r["land_use_code"] or "")
    is_commercial = land_use_code.startswith("4") if land_use_code else False
    is_vacant_land = land_use_code.startswith("5") and addr_status == "no_street_number"
    native_parcel_id = r["native_parcel_id"] if "native_parcel_id" in r.keys() else None

    first_seen_label = "—"
    if r["lead_age_days"] is not None:
        first_seen_label = f"{int(r['lead_age_days'])} days ago"

    vguide = market["verification_guide"]

    # ---- Layer 1: deal source ----
    if market["state"] == "OH":
        layer1 = _build_layer1_cuyahoga(r, sig, parcel, case, first_seen_label)
    elif market["state"] == "MI":
        layer1 = _build_layer1_wayne(r, sig, source_site, parcel, case, first_seen_label, vguide)
    else:
        layer1 = _build_layer1_shelby(r, sig, source_site, parcel, case, first_seen_label,
                                      vguide, native_parcel_id=native_parcel_id)

    # ---- Layer 2: county registry ----
    last_sale_label = "(not enriched yet)"
    if r["last_sale_date"]:
        if r["last_sale_price"]:
            last_sale_label = f"{r['last_sale_date']} for ${int(r['last_sale_price']):,}"
        else:
            last_sale_label = str(r["last_sale_date"])

    registry_url = market["registry_link"](parcel, r["property_address"],
                                           native_parcel_id=native_parcel_id)
    registry_search = market["registry_search_hint"].format(parcel=parcel)

    layer2 = {
        "title": market["registry_label"],
        "url": registry_url,
        "search_for": registry_search,
        "look_for": market["registry_look_for"],
        "stored": [
            ("Parcel Number", parcel),
            ("Owner (county registry)", r["owner_name"] or "(not enriched yet)"),
            ("Situs Address", r["property_address"] or "—"),
            ("Market Value", f"${int(r['current_market_value']):,}" if r["current_market_value"] else "—"),
            ("Land-Use Code", land_use_code or "—"),
            ("Most Recent Transfer", last_sale_label),
        ],
        "backup_links": (
            None if market["state"] == "OH"
            else [
                ("Detroit Open Data — Assessor Parcels",
                 "https://data.detroitmi.gov/datasets/parcels"),
                ("Detroit Open Data — Property Sales",
                 "https://data.detroitmi.gov/datasets/property-sales"),
                ("Wayne County Register of Deeds",
                 "https://www.waynecounty.com/elected/clerk/register-of-deeds.aspx"),
            ] if market["state"] == "MI"
            else [
                ("Trustee Search (type address if no parcel link)",
                 "https://apps2.shelbycountytrustee.com/PropertyClient"),
                ("ReGIS Parcel Viewer", "https://gis.shelbycountytn.gov/"),
                ("Register of Deeds", "https://search.register.shelby.tn.us/search/index.php"),
                ("Assessor (main)", "https://www.assessormelvinburgess.com/"),
            ]
        ),
    }

    # ---- Layer 3: off-market ----
    city = r["property_city"]
    zip_ = r["property_zip"]
    addr = r["property_address"]
    zillow_url, redfin_url = market["offmarket_links"](addr or "", city, zip_)

    if is_commercial:
        layer3 = {
            "title": "Off-Market Check — SKIP (Commercial Property)",
            "zillow_url": None,
            "redfin_url": None,
            "look_for": (
                f"Zillow and Redfin do not cover commercial properties. This parcel's land-use code "
                f"is {land_use_code} (commercial 4000-series), so a Zillow 'no result' is expected — "
                "not a stale signal. Verify instead via the county registry and the deal source (Layer 1 + 2 above)."
            ),
            "stored": [
                ("Land-Use Code", f"{land_use_code} (commercial 4000-series)"),
                ("Owner", r["owner_name"] or "—"),
            ],
        }
    elif is_vacant_land:
        layer3 = {
            "title": "Off-Market Check — Verify by Parcel (Vacant Land)",
            "zillow_url": zillow_url,
            "redfin_url": redfin_url,
            "look_for": (
                "This parcel is unnumbered vacant land (5000-series). Zillow/Redfin often return "
                f"no result — that's expected. Verify the parcel ({parcel}) on the county registry; if it exists "
                "with a valid owner, it's a real land deal."
            ),
            "stored": [
                ("Land-Use Code", f"{land_use_code} (vacant 5000-series)"),
                ("Address Status", addr_status or "—"),
            ],
        }
    else:
        layer3 = {
            "title": "Off-Market Check — Zillow + Redfin (residential MLS)",
            "zillow_url": zillow_url,
            "redfin_url": redfin_url,
            "look_for": (
                "Off-market on both Zillow and Redfin = consistent with distress (good — owner not on "
                "the open market). Active for-sale on MLS = note (owner trying to sell before "
                "auction or probate resolution — still a valid lead, worth flagging in outreach). "
                "Recently sold POST-filing date = the post-transfer guard should have caught it; "
                "if you see this, escalate so we can confirm the enrichment ran."
            ),
            "stored": [
                ("Property Address", f"{addr}, {city} {zip_ or ''}".strip()),
            ],
        }

    return {"layer1": layer1, "layer2": layer2, "layer3": layer3}


def _build_layer1_cuyahoga(r: asyncpg.Record, sig: str, parcel: str,
                            case: str | None, first_seen_label: str) -> dict:
    """Cuyahoga Layer 1 — identical to original code."""
    if sig == "probate":
        filing_year = None
        case_category_code = None
        case_suffix = None
        if case:
            for i, ch in enumerate(case):
                if not ch.isdigit():
                    break
            else:
                i = len(case)
            year_part = case[:i]
            rest = case[i:]
            if year_part.isdigit() and len(year_part) == 4:
                filing_year = int(year_part)
            for j, ch in enumerate(rest):
                if ch.isdigit():
                    break
            else:
                j = len(rest)
            case_category_code = rest[:j] or None
            case_suffix = rest[j:] or None
        category_label = {
            "EST": "ESTATE",
            "GDN": "GUARDIANSHIP",
            "TRU": "TRUST",
            "ML": "MARRIAGE LICENSE",
            "WIL": "WILL",
        }.get(case_category_code or "", case_category_code or "ESTATE")
        match_method = r["match_method"] or ""
        match_conf = r["match_confidence"] or "legacy"
        decedent_label = (
            r["decedent_name"]
            or (f"(not captured — confirm on ProWare; parcel owner is {r['owner_name'] or '—'})")
        )
        return {
            "title": "Cuyahoga Probate Court — ProWare",
            "url": "https://probate.cuyahogacounty.gov/pa/",
            "search_for": (
                "From the ProWare landing page, accept the disclaimer, then click "
                "'Case Search' (NOT 'Docket and Index Search' — that's a name-fuzzy "
                "box that returns wrong results). The 'Search by Case' form has "
                f"THREE separate fields. Enter Case Year = {filing_year or '?'}, "
                f"Case Category = {category_label}, Case Number = {case_suffix or '?'} "
                "(JUST the digits after the category code — NOT the full "
                f"'{case or '?'}' string)."
            ),
            "look_for": (
                "Case Status must say OPEN or PENDING (not Closed / Disposed / Terminated / Dismissed). "
                f"Filing year on the page should be {filing_year if filing_year else '(unknown — case# malformed)'}. "
                "Decedent name on the case page is the single source of truth — if the parcel owner below "
                "doesn't match the decedent name, this is a mis-joined row (REVIEW, not a real lead). "
                "Click 'Parties' on the case page to confirm the decedent."
            ),
            "stored": [
                ("Full Case Number", case or "—"),
                ("ProWare → Case Year", str(filing_year) if filing_year else "—"),
                ("ProWare → Case Category", category_label),
                ("ProWare → Case Number (just the suffix)", case_suffix or "—"),
                ("Case Status (our copy)", r["case_status"] or "—"),
                ("Decedent (our stored case data)", decedent_label),
                ("Parcel Owner (county registry — must match decedent)", r["owner_name"] or "—"),
                ("Match Method", match_method or "(legacy / pre-tiering)"),
                ("Match Confidence", match_conf),
                ("First Seen By Us", first_seen_label),
            ],
        }
    elif sig == "tax_delinquent_foreclosure":
        return {
            "title": "Daily Legal News — Delinquent Tax Auctions",
            "url": "https://www.dln.com/",
            "search_for": (
                f"Open the Delinquent Tax table on the home page. Filter by sale date "
                f"{r['sale_date']} or search for case '{case or parcel}'."
            ),
            "look_for": (
                "Row must still appear in the upcoming-auctions feed with matching parcel and sale date. "
                "By Ohio law (ORC 5721) tax-foreclosure parcels are delinquent ≥1 year — every row here "
                "already meets the aged-lien bar by construction."
            ),
            "stored": [
                ("Case Number", case or "—"),
                ("Sale Date", str(r["sale_date"] or "—")),
                ("Opening Bid", f"${int(r['opening_bid_usd']):,}" if r["opening_bid_usd"] else "—"),
                ("Parcel", parcel),
            ],
        }
    elif sig == "mortgage_foreclosure":
        return {
            "title": "Daily Legal News — Sheriff Sales (Mortgage Foreclosure)",
            "url": "https://www.dln.com/",
            "search_for": (
                f"Open the Sheriff Sales table on the home page. Filter by sale date "
                f"{r['sale_date']} or search for case '{case or parcel}'."
            ),
            "look_for": (
                "Row must still appear in the upcoming-sales feed with matching parcel and sale date. "
                "Owner facing forced sale by the lender — distressed timeline regardless of outcome."
            ),
            "stored": [
                ("Case Number", case or "—"),
                ("Sale Date", str(r["sale_date"] or "—")),
                ("Opening Bid", f"${int(r['opening_bid_usd']):,}" if r["opening_bid_usd"] else "—"),
                ("Parcel", parcel),
            ],
        }
    elif sig == "forfeited_land":
        return {
            "title": "Cuyahoga Forfeited Land Sale — Fiscal Officer",
            "url": "https://cuyahogacounty.gov/fiscal-officer/departments/real-property/forfeited-lands",
            "search_for": (
                f"Open the Forfeited Lands Locator (ArcGIS map) and find parcel {parcel} or "
                f"address '{r['property_address']}'."
            ),
            "look_for": (
                "Parcel must still appear on the locator. These FAILED at sheriff's tax-foreclosure "
                "auction and were forfeited to the State of Ohio — the deepest-discount tax deed "
                "(minimum bid ≈ back taxes + costs). HIGH-SIGNAL when county market value > opening bid "
                "(positive equity). The deeded owner is literally 'STATE OF OHIO FORF' — you buy from "
                "the county, not a distressed owner."
            ),
            "stored": [
                ("Case", case or "—"),
                ("Opening Bid (tax+costs)", f"${int(r['opening_bid_usd']):,}" if r["opening_bid_usd"] else "—"),
                ("County Market Value", f"${int(r['appraised_value_usd']):,}" if r["appraised_value_usd"] else "—"),
                ("Parcel", parcel),
            ],
        }
    elif sig == "land_bank_inventory":
        return {
            "title": "Cuyahoga Land Bank — Available Properties",
            "url": "https://landbank.cuyahogalandbank.org/all-available-properties/",
            "search_for": (
                f"Scroll the inventory list or use Ctrl+F to find parcel {parcel} or "
                f"address '{r['property_address']}'."
            ),
            "look_for": (
                "Property must still appear on the available-properties list. Land Bank is a FULL_RESCAN "
                "source — if it drops off the live page, our scraper marks it not_listed next cycle "
                "(meaning sold or under contract)."
            ),
            "stored": [
                ("Parcel", parcel),
                ("Address", r["property_address"] or "—"),
            ],
        }
    elif sig == "tax_delinquent":
        # Pre-Distress LEAD — MyPlace is the authority (owner + delinquent tax balance).
        return {
            "title": "Cuyahoga MyPlace — Parcel Page (Tax-Delinquent Lead)",
            "url": f"https://myplace.cuyahogacounty.gov/?parcel={parcel}",
            "search_for": (
                "PRE-DISTRESS LEAD (owner under pressure, NOT yet for sale). Open the MyPlace "
                f"parcel page for {parcel}. Confirm a DELINQUENT tax balance is owed and the owner "
                "is a private party (not the county / a public body). The 'Tax Information' tab "
                "shows the unpaid balance + years delinquent."
            ),
            "look_for": (
                "VALID: parcel owes delinquent taxes (>= $2k or flagged for foreclosure), owner is a "
                "private party, and it is NOT already on the DLN sheriff-sale feed (that would be a "
                "buy-now deal, not a pre-distress lead). Off-market on Zillow/Redfin = consistent with "
                "a distressed owner (good). "
                "RED FLAGS: balance paid off / $0 due (resolved — not distress); a recent transfer to a "
                "new private owner on MyPlace (sold — should auto-retire via last_sale_date); actively "
                "listed for sale on MLS (owner already selling normally — note in outreach)."
            ),
            "stored": [
                ("Signal Type", "tax_delinquent (Pre-Distress Lead)"),
                ("Parcel", parcel),
                ("Property Address", r["property_address"] or "—"),
                ("Owner (MyPlace — should be the distressed private owner)", r["owner_name"] or "(not enriched)"),
                ("First Seen By Us", first_seen_label),
            ],
        }
    else:
        return {
            "title": "Source not classified",
            "url": "https://myplace.cuyahogacounty.gov/",
            "search_for": f"Paste parcel {parcel} in the search box.",
            "look_for": "Confirm the parcel exists and address matches.",
            "stored": [("Signal Type", sig or "—")],
        }


def _build_layer1_shelby(r: asyncpg.Record, sig: str, source_site: str,
                          parcel: str, case: str | None, first_seen_label: str,
                          vguide: dict, *, native_parcel_id: str | None = None) -> dict:
    """Shelby (Memphis, TN) Layer 1 — per signal type.

    The Shelby County Trustee parcel page is the UNIVERSAL verifier for tax_deed.
    It shows PRIMARY OWNER, TAX DUE, BILLING HISTORY, and the TAX SALE PROPERTY
    NOTICE block (when scheduled) — everything needed to confirm a real pre-auction lead.
    The URL requires the native spaced PARCELID (native_parcel_id), not the 14-char canonical.
    """
    trustee_url = _trustee_parcel_url(native_parcel_id)
    trustee_fallback = "https://apps2.shelbycountytrustee.com/PropertyClient"
    trustee_search_hint = (
        f"type street number + street name (no suffix) — e.g. '{(r['property_address'] or '').split(',')[0]}'"
        if not trustee_url else ""
    )
    native_id_label = native_parcel_id or "(not yet populated — re-run shelby_parcels spine)"

    if sig == "tax_deed":
        guide = vguide.get("tax_deed", {})
        layer_url = trustee_url or trustee_fallback
        search_for = (
            f"Click the button to open the Trustee parcel page directly. "
            f"On the page, look for the 'TAX SALE PROPERTY NOTICE' block — it shows "
            f"Tax Sale ID '{case or '?'}' when this parcel is scheduled. "
            f"TAX DUE / TOTAL BALANCE should be > $0."
            if trustee_url else
            f"No direct link yet (native_parcel_id not populated). Open the Trustee PropertyClient "
            f"search at {trustee_fallback} and {trustee_search_hint}. "
            f"Look for the 'TAX SALE PROPERTY NOTICE' block with Tax Sale ID '{case or '?'}'."
        )
        return {
            "title": "Shelby County Trustee — Parcel Page (Tax Sale Confirmation)",
            "url": layer_url,
            "search_for": search_for,
            "look_for": (
                f"{guide.get('valid', '')} "
                f"Red flags: {guide.get('red_flags', '')}"
            ),
            "stored": [
                ("Parcel (14-digit canonical)", parcel),
                ("Native Parcel ID (for Trustee URL)", native_id_label),
                ("TS Code / Case Number", case or "—"),
                ("Source Site", source_site),
                ("Property Address", r["property_address"] or "—"),
                ("First Seen By Us", first_seen_label),
                ("Owner (Trustee — should be private party)", r["owner_name"] or "(not enriched)"),
            ],
        }
    elif sig == "land_bank_inventory" and "MMLBA" in source_site:
        guide = vguide.get("mmlba", {})
        return {
            "title": "MMLBA — Memphis Metropolitan Land Bank Authority",
            "url": "https://mmlba.org/property-sales/",
            "search_for": (
                f"Open the MMLBA property-sales gallery and search / Ctrl+F for "
                f"address '{r['property_address']}'. Then verify the owner on the Trustee parcel page "
                f"(Layer 2 below) to confirm MMLBA holds the title."
            ),
            "look_for": (
                f"{guide.get('valid', '')} "
                f"Red flags: {guide.get('red_flags', '')}"
            ),
            "stored": [
                ("Parcel (14-digit canonical)", parcel),
                ("Native Parcel ID (for Trustee URL)", native_id_label),
                ("Source Site", source_site),
                ("Property Address", r["property_address"] or "—"),
                ("First Seen By Us", first_seen_label),
                ("Owner (Trustee — should be MMLBA)", r["owner_name"] or "(not enriched)"),
            ],
        }
    elif sig == "land_bank_inventory":
        guide = vguide.get("land_bank_inventory", {})
        return {
            "title": "Shelby County Land Bank — ePropertyPlus Portal",
            "url": "https://public-sctn.epropertyplus.com/landmgmtpub/app/base/landing",
            "search_for": (
                f"Open the Shelby County Land Bank portal and search for parcel {parcel} "
                f"or address '{r['property_address']}'. Properties listed FOR SALE are the active inventory. "
                f"Then verify ownership on the Trustee parcel page (Layer 2 below)."
            ),
            "look_for": (
                f"{guide.get('valid', '')} "
                f"Red flags: {guide.get('red_flags', '')}"
            ),
            "stored": [
                ("Parcel (14-digit canonical)", parcel),
                ("Native Parcel ID (for Trustee URL)", native_id_label),
                ("Source Site", source_site),
                ("Property Address", r["property_address"] or "—"),
                ("First Seen By Us", first_seen_label),
                ("Owner (Trustee — should be Shelby County)", r["owner_name"] or "(not enriched)"),
            ],
        }
    elif sig in ("tax_delinquent", "eviction"):
        # Pre-Distress LEAD — Trustee parcel page is the authority (shows owner + tax balance).
        guide = vguide.get(sig, {})
        layer_url = trustee_url or trustee_fallback
        is_tax = sig == "tax_delinquent"
        return {
            "title": ("Shelby County Trustee — Parcel Page (Tax-Delinquent Lead)"
                      if is_tax else "Shelby County Trustee — Parcel Page (Eviction Lead)"),
            "url": layer_url,
            "search_for": (
                (f"PRE-DISTRESS LEAD. Open the Trustee parcel page. "
                 + ("Confirm TAX DUE / TOTAL BALANCE > $0 and PRIMARY OWNER is a private party. "
                    "A 'TAX SALE PROPERTY NOTICE' block = it has escalated to a scheduled auction."
                    if is_tax else
                    "Confirm the PRIMARY OWNER still matches our stored owner (the distressed landlord) "
                    "and the parcel hasn't recently transferred (sold)."))
                if trustee_url else
                f"No direct link yet (native_parcel_id not populated). Open Trustee PropertyClient at "
                f"{trustee_fallback} and {trustee_search_hint}."
            ),
            "look_for": (
                f"{guide.get('valid', '')} "
                f"Red flags: {guide.get('red_flags', '')}"
            ),
            "stored": [
                ("Signal Type", sig),
                ("Parcel (14-digit canonical)", parcel),
                ("Native Parcel ID (for Trustee URL)", native_id_label),
                ("Source Site", source_site),
                ("Property Address", r["property_address"] or "—"),
                ("First Seen By Us", first_seen_label),
                ("Owner (Trustee — should be the distressed private owner)", r["owner_name"] or "(not enriched)"),
            ],
        }
    else:
        layer_url = trustee_url or trustee_fallback
        return {
            "title": "Shelby County Trustee — Parcel Page",
            "url": layer_url,
            "search_for": (
                f"Open the Trustee parcel page and confirm owner + address match."
                if trustee_url else
                f"Open Trustee PropertyClient at {trustee_fallback} and {trustee_search_hint}."
            ),
            "look_for": "Confirm the parcel exists and address / owner match our stored data.",
            "stored": [
                ("Signal Type", sig or "—"),
                ("Parcel (14-digit canonical)", parcel),
                ("Native Parcel ID (for Trustee URL)", native_id_label),
                ("Source Site", source_site),
            ],
        }


def _build_layer1_wayne(r: asyncpg.Record, sig: str, source_site: str,
                         parcel: str, case: str | None, first_seen_label: str,
                         vguide: dict) -> dict:
    """Wayne County (Detroit, MI) Layer 1 — per signal type.

    The Wayne County Treasurer property-tax lookup (pto.waynecounty.com) is the
    UNIVERSAL cross-check authority for all Wayne categories: it shows current owner,
    live delinquency/forfeiture status, and payment history in one place.

    MI MORTGAGE FORECLOSURE CRITICAL NOTE (MCL 600.3240): a PAST sale_date is NOT a
    kill here — the original owner has 6 months post-sale to redeem. Rows tagged
    'in_redemption' in auction_status are LIVE leads, not stale ones. The Layer-1 source
    is the mipublicnotices notice (pre-sale) or the in-redemption status (post-sale).
    Never flag a Wayne mortgage_foreclosure row STALE solely because sale_date is past.
    """
    pto_base = "https://pto.waynecounty.com/"

    if sig == "mortgage_foreclosure":
        guide = vguide.get("mortgage_foreclosure", {})
        auction_status = (r["auction_status"] if "auction_status" in r.keys() else None) or ""
        in_redemption = "in_redemption" in auction_status.lower()
        status_note = (
            "POST-SALE IN-REDEMPTION — owner has up to 6 months from sale_date to redeem (MCL 600.3240). "
            "This row is a LIVE lead (not stale). sale_date in the past is expected."
            if in_redemption else
            "PRE-SALE — foreclosure notice published on mipublicnotices (Detroit Legal News area=82). "
            "Owner has until the sale date to redeem."
        )
        return {
            "title": "Detroit Legal News / mipublicnotices — Mortgage Foreclosure Notice",
            "url": "https://www.mipublicnotices.com/",
            "search_for": (
                "Search mipublicnotices.com (area=82, Wayne County) for the mortgagor name or property address. "
                "Confirm the notice is still published (pre-sale) OR verify the sale_date and compute whether "
                "the 6-month MI redemption window is still open (sale_date + 180 days >= today). "
                f"STATUS: {status_note}"
            ),
            "look_for": (
                f"{guide.get('valid', '')} "
                f"Red flags: {guide.get('red_flags', '')}"
            ),
            "stored": [
                ("Signal Type", "mortgage_foreclosure (MI 6-month redemption)"),
                ("Parcel", parcel),
                ("Source Site", source_site),
                ("Sale Date (6-mo redemption window starts here)", str(r["sale_date"] or "—")),
                ("In-Redemption / Auction Status", auction_status or "(pre-sale)"),
                ("Property Address", r["property_address"] or "—"),
                ("First Seen By Us", first_seen_label),
                ("Owner (pto — should be original owner, not lender)", r["owner_name"] or "(not enriched)"),
            ],
        }
    elif sig == "tax_delinquent_foreclosure":
        guide = vguide.get("tax_delinquent_foreclosure", {})
        return {
            "title": "Wayne County Treasurer — Tax-Foreclosure Auction",
            "url": "https://www.waynecountytreasurermi.com/",
            "search_for": (
                f"Open the Wayne County Treasurer auction and search for parcel '{parcel}'. "
                "Confirm the row is still ACTIVE (STATUS_CD != RM). STATUS_CD=RM = removed/redeemed. "
                "NOTE: Wayne tax auction issues a CLEAN fee-simple deed — NO post-sale redemption "
                "(unlike MI mortgage foreclosures). Equity = market value − minimum bid."
            ),
            "look_for": (
                f"{guide.get('valid', '')} "
                f"Red flags: {guide.get('red_flags', '')}"
            ),
            "stored": [
                ("Parcel", parcel),
                ("Source Site", source_site),
                ("Sale Date", str(r["sale_date"] or "—")),
                ("Opening Bid (min bid)", f"${int(r['opening_bid_usd']):,}" if r["opening_bid_usd"] else "—"),
                ("Case / Reference", case or "—"),
                ("Property Address", r["property_address"] or "—"),
                ("First Seen By Us", first_seen_label),
                ("Owner (pto — confirm still delinquent)", r["owner_name"] or "(not enriched)"),
            ],
        }
    elif sig == "land_bank_inventory":
        guide = vguide.get("land_bank_inventory", {})
        is_wclb = "Wayne County Land Bank" in source_site
        if is_wclb:
            layer_url = "https://www.waynecountylandbank.com/"
            search_hint = (
                f"Open the Wayne County Land Bank (ePropertyPlus portal) and search for parcel {parcel} "
                f"or address '{r['property_address']}'. Confirm it is still listed FOR SALE (available='Y')."
            )
        else:
            layer_url = "https://buildingdetroit.org/"
            search_hint = (
                f"Open buildingdetroit.org and search for address '{r['property_address']}' or parcel {parcel}. "
                "Confirm the property is still in the for-sale inventory (Auction / Own It Now / Rehabbed & Ready, "
                "or a buyable lot) and NOT in /pastlistings or marked 'Under Contract'."
            )
        return {
            "title": ("Wayne County Land Bank — ePropertyPlus"
                      if is_wclb else "Detroit Land Bank Authority (buildingdetroit.org)"),
            "url": layer_url,
            "search_for": search_hint,
            "look_for": (
                f"{guide.get('valid', '')} "
                f"Red flags: {guide.get('red_flags', '')}"
            ),
            "stored": [
                ("Parcel", parcel),
                ("Source Site", source_site),
                ("Property Address", r["property_address"] or "—"),
                ("First Seen By Us", first_seen_label),
                ("Owner (pto — should be DLBA / WCLB / City of Detroit)", r["owner_name"] or "(not enriched)"),
            ],
        }
    elif sig == "tax_delinquent":
        # Pre-Distress LEAD — pto.waynecounty.com is the authority (owner + forfeiture status).
        guide = vguide.get("tax_delinquent", {})
        return {
            "title": "Wayne County Treasurer (pto) — Forfeiture / Delinquency Record",
            "url": pto_base,
            "search_for": (
                f"PRE-DISTRESS LEAD (owner on Wayne Treasurer forfeiture roll, ≥2yr delinquent). "
                f"Open pto.waynecounty.com and search for parcel '{parcel}' (keep trailing '.' or '-' exactly). "
                "Confirm: balance owed > $0, owner is a PRIVATE party, parcel is NOT yet on the auction list "
                "(if it is, it has escalated to a buy-now deal — note it)."
            ),
            "look_for": (
                f"{guide.get('valid', '')} "
                f"Red flags: {guide.get('red_flags', '')}"
            ),
            "stored": [
                ("Signal Type", "tax_delinquent (Pre-Distress Lead — forfeiture roll)"),
                ("Parcel", parcel),
                ("Source Site", source_site),
                ("Property Address", r["property_address"] or "—"),
                ("First Seen By Us", first_seen_label),
                ("Owner (pto — should be the distressed private owner)", r["owner_name"] or "(not enriched)"),
            ],
        }
    elif sig == "blight_violation":
        # Pre-Distress LEAD — Detroit Open Data blight tickets.
        guide = vguide.get("blight_violation", {})
        return {
            "title": "Detroit Open Data — Blight Violations",
            "url": "https://data.detroitmi.gov/datasets/blight-violations",
            "search_for": (
                f"PRE-DISTRESS LEAD (Detroit blight judgments unpaid). "
                f"Search the Detroit blight-violation dataset for parcel {parcel} or address '{r['property_address']}'. "
                "Confirm: amt_balance_due > 0, disposition is 'Responsible…', owner is an LLC or absentee. "
                "Cross-check current owner on pto.waynecounty.com to confirm the same party still holds the parcel."
            ),
            "look_for": (
                f"{guide.get('valid', '')} "
                f"Red flags: {guide.get('red_flags', '')}"
            ),
            "stored": [
                ("Signal Type", "blight_violation (Pre-Distress Lead)"),
                ("Parcel", parcel),
                ("Source Site", source_site),
                ("Property Address", r["property_address"] or "—"),
                ("First Seen By Us", first_seen_label),
                ("Owner (pto — should be LLC/absentee, still on title)", r["owner_name"] or "(not enriched)"),
            ],
        }
    else:
        return {
            "title": "Wayne County Treasurer (pto) — Parcel Lookup",
            "url": pto_base,
            "search_for": f"Search pto.waynecounty.com for parcel '{parcel}' and confirm owner + address.",
            "look_for": "Confirm the parcel exists and address / owner match our stored data.",
            "stored": [
                ("Signal Type", sig or "—"),
                ("Parcel", parcel),
                ("Source Site", source_site),
            ],
        }


# ----------------------------------------------------------------- Fetching


# INVARIANT — sample scope is l.market, NOT property_state. Cuyahoga and Summit are
# both OH; a state filter leaks Cuyahoga rows into a Summit sample (and vice-versa).
# market_filter (market_config) pins each sample to its own market column.
async def _fetch_signal(conn, signal: str, limit: int, market: dict) -> list:
    market_filter = market["market_filter"]
    return await conn.fetch(
        f"""
        SELECT {_SELECT_COLS}
        {_FROM_JOIN}
        WHERE l.status = 'active' AND l.duplicate_of IS NULL
          AND {market_filter}
          AND l.signal_type = $1
        ORDER BY random()
        LIMIT {int(limit)}
        """,
        signal,
    )


async def _fetch_random(conn, limit: int, exclude_ids: set, market: dict) -> list:
    market_filter = market["market_filter"]
    rows = await conn.fetch(
        f"""
        SELECT {_SELECT_COLS}
        {_FROM_JOIN}
        WHERE l.status = 'active' AND l.duplicate_of IS NULL
          AND {market_filter}
        ORDER BY random()
        LIMIT {int(limit) * 4}
        """,
    )
    out = []
    for r in rows:
        if r["id"] in exclude_ids:
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


# -------------------------------------------------------------- HTML render


def _esc(s) -> str:
    if s is None:
        return ""
    return _html.escape(str(s))


def _stored_rows_html(items: list[tuple[str, str]]) -> str:
    return "\n".join(
        f'<div class="row"><span class="label">{_esc(lbl)}</span><br><span class="val">{_esc(val)}</span></div>'
        for lbl, val in items
    )


def _guide_box_html(guide_entry: dict | None, category_key: str | None = None) -> str:
    """Render a teal guidance box for a category's valid/red_flags content."""
    if not guide_entry:
        return ""
    valid = guide_entry.get("valid", "")
    red = guide_entry.get("red_flags", "")
    if not valid and not red:
        return ""
    parts = []
    if valid:
        parts.append(f'<div class="guide-valid"><strong>Valid looks like:</strong> {_esc(valid)}</div>')
    if red:
        parts.append(f'<div class="guide-red"><strong>Red flags:</strong> {_esc(red)}</div>')
    return f'<div class="guide-box">{"".join(parts)}</div>'


def _general_guide_html() -> str:
    return f"""
  <div class="general-guide">
    <div class="general-guide-title">How to read these results — for any market</div>
    <div class="general-guide-body">{_esc(GENERAL_GUIDE)}</div>
  </div>
"""


def _layer_html(layer: dict, title: str, guide_entry: dict | None = None) -> str:
    backup_links_html = ""
    if layer.get("backup_links"):
        links = " · ".join(
            f'<a href="{_esc(url)}" target="_blank" rel="noopener">{_esc(label)} ↗</a>'
            for label, url in layer["backup_links"]
        )
        backup_links_html = f'<div class="backup-links">Backup lookup links: {links}</div>'

    guide_html = _guide_box_html(guide_entry) if guide_entry else ""

    return f"""
    <div class="layer">
      <div class="layer-title">{_esc(title)}</div>
      {guide_html}
      <div class="layer-grid">
        <div class="col-action">
          <strong>Open & verify:</strong>
          <a class="btn" href="{_esc(layer['url'])}" target="_blank" rel="noopener">{_esc(layer['title'])} ↗</a>
          <div class="search-for"><em>{_esc(layer['search_for'])}</em></div>
          <div class="look-for"><strong>What to look for:</strong> {_esc(layer['look_for'])}</div>
          {backup_links_html}
        </div>
        <div class="col-stored">
          <div class="stored-title">Our stored data (must match the page)</div>
          {_stored_rows_html(layer['stored'])}
        </div>
      </div>
    </div>
"""


def _offmarket_html(layer: dict) -> str:
    if layer["zillow_url"]:
        links = (
            f'<a class="btn" href="{_esc(layer["zillow_url"])}" target="_blank" rel="noopener">Open Zillow ↗</a>'
            f'<a class="btn btn-secondary" href="{_esc(layer["redfin_url"])}" target="_blank" rel="noopener">Open Redfin ↗</a>'
        )
    else:
        links = "<em>(Skipped — commercial property)</em>"
    return f"""
    <div class="layer">
      <div class="layer-title">Layer 3 — {_esc(layer['title'])}</div>
      <div class="layer-grid">
        <div class="col-action">
          {links}
          <div class="look-for"><strong>What to look for:</strong> {_esc(layer['look_for'])}</div>
        </div>
        <div class="col-stored">
          <div class="stored-title">Context</div>
          {_stored_rows_html(layer['stored'])}
        </div>
      </div>
    </div>
"""


def _get_guide_for_layer1(r: asyncpg.Record, market: dict) -> dict | None:
    """Resolve which verification_guide entry applies to this listing's Layer 1."""
    sig = r["signal_type"] or ""
    source_site = r["source_site"] or ""
    vguide = market["verification_guide"]

    if sig == "tax_deed":
        return vguide.get("tax_deed")
    elif sig == "land_bank_inventory" and "MMLBA" in source_site:
        return vguide.get("mmlba")
    elif sig == "land_bank_inventory":
        return vguide.get("land_bank_inventory")
    elif sig == "probate":
        return vguide.get("probate")
    elif sig in ("tax_delinquent_foreclosure", "forfeited_land", "mortgage_foreclosure"):
        return vguide.get(sig)
    elif sig in ("tax_delinquent", "eviction"):
        return vguide.get(sig)
    return None


def _card_html(i: int, r: asyncpg.Record, layers: dict, verdict: str,
               notes: list[str], market: dict) -> str:
    addr = f"{r['property_address']}, {r['property_city']}"
    sig_label = (r["signal_type"] or "").replace("_", " ")
    source_site = r["source_site"] or ""
    parcel = r["source_listing_id"] or "—"
    meta_parts = [f"parcel {parcel}"]
    if source_site:
        meta_parts.append(source_site)
    if r["sale_date"]:
        meta_parts.append(f"sale {r['sale_date']}")
    if r["opening_bid_usd"]:
        meta_parts.append(f"opening bid ${int(r['opening_bid_usd']):,}")
    if r["signal_type"] == "probate" and r["lead_age_days"] is not None:
        meta_parts.append(f"lead age {int(r['lead_age_days'])}d")
    meta_str = " · ".join(meta_parts)
    notes_str = " · ".join(notes) if notes else ""

    guide_entry = _get_guide_for_layer1(r, market)

    return f"""
  <div class="card">
    <div class="card-header">
      <div class="card-header-text">
        <div class="card-title">[#{i}] {_esc(sig_label)} — {_esc(addr)}</div>
        <div class="card-meta">{_esc(meta_str)}</div>
        {f'<div class="card-notes">{_esc(notes_str)}</div>' if notes_str else ''}
      </div>
      <span class="verdict {verdict}">{verdict}</span>
    </div>
    {_layer_html(layers['layer1'], 'Layer 1 — Source Page (the deal pipeline)', guide_entry)}
    {_layer_html(layers['layer2'], f"Layer 2 — {market['registry_label']} (independent second source)")}
    {_offmarket_html(layers['layer3'])}
  </div>
"""


def _render_html(rows: list, command: str, market: dict, market_name: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    counts = {"VALID": 0, "REVIEW": 0, "STALE": 0}
    by_source: dict[str, dict[str, int]] = {}
    cards = []

    for i, r in enumerate(rows, 1):
        verdict, notes = _verdict(r)
        counts[verdict] += 1
        sig = r["signal_type"] or "unknown"
        bs = by_source.setdefault(sig, {"VALID": 0, "REVIEW": 0, "STALE": 0, "total": 0})
        bs[verdict] += 1
        bs["total"] += 1
        cards.append(_card_html(i, r, _layers(r, market), verdict, notes, market))

    by_source_str = " · ".join(
        f"{k.replace('_', ' ')}: {v['VALID']}/{v['total']} VALID"
        + (f" ({v['REVIEW']} REVIEW)" if v["REVIEW"] else "")
        + (f" ({v['STALE']} STALE)" if v["STALE"] else "")
        for k, v in sorted(by_source.items())
    )

    cards_html = "\n".join(cards)
    market_label = f"{market_name.capitalize()} ({market['state']})"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tranchi — {_esc(market_label)} Verify Pass {ts}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      max-width: 1180px; margin: 2rem auto; padding: 0 1.25rem; color: #1c1c1c;
      background: #fafbfc;
    }}
    h1 {{ font-size: 1.55rem; margin: 0 0 0.3rem; letter-spacing: -0.01em; }}
    .subtitle {{ color: #666; margin-bottom: 1.25rem; font-size: 0.9rem; }}
    .summary {{
      background: #fff; padding: 1rem 1.15rem; border-radius: 8px; margin-bottom: 1.25rem;
      border-left: 4px solid #0066cc; border: 1px solid #d8dde2; border-left-width: 4px;
    }}
    .summary .big {{ font-size: 1.05rem; font-weight: 600; margin-bottom: 0.4rem; }}
    .summary .src-line {{ font-size: 0.85rem; color: #333; }}
    .summary .help {{ margin-top: 0.55rem; font-size: 0.85rem; color: #555; }}
    .general-guide {{
      background: #f0f8ff; border: 1px solid #b8d4ed; border-left: 4px solid #2178b5;
      border-radius: 8px; padding: 0.85rem 1.1rem; margin-bottom: 1.75rem;
    }}
    .general-guide-title {{
      font-size: 0.75rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.07em; color: #2178b5; margin-bottom: 0.45rem;
    }}
    .general-guide-body {{ font-size: 0.88rem; color: #1c3a55; line-height: 1.55; }}
    .guide-box {{
      background: #f6fcf7; border: 1px solid #b8e2c0; border-left: 4px solid #2a9d50;
      border-radius: 6px; padding: 0.65rem 0.85rem; margin-bottom: 0.75rem;
      font-size: 0.85rem; line-height: 1.5;
    }}
    .guide-valid {{ color: #1a5a2a; margin-bottom: 0.35rem; }}
    .guide-red {{ color: #6d1119; }}
    .card {{
      border: 1px solid #d8dde2; border-radius: 10px; margin-bottom: 1.75rem;
      overflow: hidden; background: #fff;
      box-shadow: 0 1px 2px rgba(16, 22, 26, 0.04);
    }}
    .card-header {{
      background: #fafbfc; padding: 0.85rem 1.1rem; border-bottom: 1px solid #d8dde2;
      display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem;
    }}
    .card-title {{ font-size: 0.98rem; font-weight: 600; }}
    .card-meta {{ font-size: 0.8rem; color: #666; margin-top: 0.2rem; }}
    .card-notes {{ font-size: 0.8rem; color: #444; margin-top: 0.2rem; font-style: italic; }}
    .verdict {{
      padding: 0.22rem 0.7rem; border-radius: 4px; font-size: 0.74rem;
      font-weight: 700; letter-spacing: 0.06em; flex-shrink: 0; white-space: nowrap;
    }}
    .verdict.VALID {{ background: #d4edda; color: #0e5223; }}
    .verdict.REVIEW {{ background: #fff3cd; color: #6c4d00; }}
    .verdict.STALE {{ background: #f8d7da; color: #6d1119; }}
    .layer {{ padding: 0.95rem 1.1rem; border-top: 1px solid #eef1f4; }}
    .layer-title {{
      font-weight: 700; font-size: 0.74rem; text-transform: uppercase;
      color: #555; letter-spacing: 0.07em; margin-bottom: 0.65rem;
    }}
    .layer-grid {{
      display: grid; grid-template-columns: 1.1fr 1fr; gap: 1.25rem; align-items: start;
    }}
    @media (max-width: 720px) {{ .layer-grid {{ grid-template-columns: 1fr; }} }}
    .col-action {{ font-size: 0.88rem; }}
    .col-stored {{
      font-size: 0.83rem; background: #f9fafb; padding: 0.65rem 0.8rem;
      border-radius: 6px; border: 1px solid #eef1f4;
    }}
    .stored-title {{
      font-size: 0.7rem; color: #555; text-transform: uppercase;
      letter-spacing: 0.06em; margin-bottom: 0.55rem; font-weight: 700;
    }}
    .col-stored .row {{ margin-bottom: 0.5rem; }}
    .col-stored .row:last-child {{ margin-bottom: 0; }}
    .col-stored .label {{ color: #666; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; }}
    .col-stored .val {{ font-weight: 500; color: #1a1a1a; }}
    .search-for {{ margin: 0.3rem 0 0.55rem; color: #555; font-size: 0.82rem; }}
    .look-for {{
      background: #f4f8fc; border-left: 3px solid #0066cc;
      padding: 0.5rem 0.7rem; margin-top: 0.5rem; font-size: 0.85rem; line-height: 1.5;
      border-radius: 0 4px 4px 0;
    }}
    .backup-links {{
      margin-top: 0.45rem; font-size: 0.8rem; color: #555;
    }}
    a {{ color: #0066cc; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .btn {{
      display: inline-block; background: #0066cc; color: #fff !important;
      padding: 0.4rem 0.75rem; border-radius: 5px; font-weight: 600;
      font-size: 0.8rem; margin-right: 0.4rem; margin-bottom: 0.2rem;
    }}
    .btn:hover {{ background: #004fa3; text-decoration: none; }}
    .btn-secondary {{ background: #fff; color: #0066cc !important; border: 1px solid #0066cc; }}
    .btn-secondary:hover {{ background: #f4f8fc; }}
    code {{ background: #f0f2f5; padding: 0.1rem 0.35rem; border-radius: 3px; font-size: 0.85em; }}
    .footer {{
      margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid #d8dde2;
      color: #666; font-size: 0.82rem;
    }}
  </style>
</head>
<body>
  <h1>Tranchi — {_esc(market_label)} Verify Pass</h1>
  <div class="subtitle">Generated {ts} · <code>{_esc(command)}</code></div>

  <div class="summary">
    <div class="big">Result: {counts['VALID']} VALID · {counts['REVIEW']} REVIEW · {counts['STALE']} STALE (of {len(rows)})</div>
    <div class="src-line">By source — {by_source_str}</div>
    <div class="help">
      Each card shows three independent verification layers. <strong>Click the link</strong> in each layer to open the live source page,
      and compare what you see there against the <em>stored data</em> column on the right. Layer 1 = the deal pipeline; Layer 2 = the
      county registry (independent second source); Layer 3 = the off-market human eyeball.
      The green guidance box at Layer 1 teaches what valid vs. suspicious looks like for that deal category.
    </div>
  </div>

  {_general_guide_html()}

  {cards_html}

  <div class="footer">
    Re-run: <code>{_esc(command)}</code><br>
    Full method writeup: <code>Clients/Marc/tranchi/VALIDATION-DIGEST.md</code>
  </div>
</body>
</html>
"""


# -------------------------------------------------------------------- Main


def _print_console(rows: list, market: dict) -> None:
    print(f"\n=== VERIFICATION PASS — {len(rows)} listings ({market['state']}) ===\n")
    counts = {"VALID": 0, "REVIEW": 0, "STALE": 0}
    for i, r in enumerate(rows, 1):
        verdict, notes = _verdict(r)
        counts[verdict] += 1
        bid = f" bid=${int(r['opening_bid_usd']):,}" if r["opening_bid_usd"] else ""
        sd = f" sale={r['sale_date']}" if r["sale_date"] else ""
        src_url, check = _source_and_check(r, market)
        city = r["property_city"] or ""
        zip_ = r["property_zip"]
        addr = r["property_address"] or ""
        parcel = r["source_listing_id"] or ""
        native_parcel_id = r["native_parcel_id"] if "native_parcel_id" in r.keys() else None
        zillow_url, redfin_url = market["offmarket_links"](addr, city, zip_)
        registry_url = market["registry_link"](parcel, addr, native_parcel_id=native_parcel_id)
        registry_short = market["registry_label"].split(" — ")[0]
        print(f"[{i:>2}] {verdict:<6} {r['signal_type']:<26} {addr}, {city} ({parcel}){sd}{bid}")
        print(f"      {' | '.join(notes)}")
        print(f"      Redfin:   {redfin_url}")
        print(f"      Zillow:   {zillow_url}")
        print(f"      Registry: {registry_url}  [{registry_short}]")
        print(f"      Source:   {src_url}")
        print(f"      CHECK:    {check}")
    print("\n" + "=" * 70)
    print(f"  VALID={counts['VALID']}  REVIEW={counts['REVIEW']}  STALE={counts['STALE']}  (of {len(rows)})")
    print("  Manual step: open each Redfin/Zillow link — off-market = consistent with distress; active MLS = flag.")
    print("=" * 70 + "\n")


async def run(args) -> None:
    url = os.environ["DATABASE_URL"]
    market_name = args.market
    market = MARKETS[market_name]
    conn = await asyncpg.connect(url)
    try:
        rows: list = []

        deal_sources = market["deal_sources"]

        if args.stratified:
            for sig in deal_sources:
                got = await _fetch_signal(conn, sig, args.stratified, market)
                rows.extend(got)
            target_total = args.sample or (args.stratified * len(deal_sources) + 3)
            if len(rows) < target_total:
                fill = await _fetch_random(conn, target_total - len(rows),
                                           {r["id"] for r in rows}, market)
                rows.extend(fill)
        elif args.signal:
            rows = await _fetch_signal(conn, args.signal, args.limit or args.sample or 15, market)
        else:
            n = args.limit or args.sample or 15
            rows = await _fetch_random(conn, n, set(), market)

        if args.html:
            html = _render_html(rows, _command_repr(args), market, market_name)
            out_path = Path(args.html).expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(html, encoding="utf-8")
            counts = {"VALID": 0, "REVIEW": 0, "STALE": 0}
            for r in rows:
                v, _ = _verdict(r)
                counts[v] += 1
            print(f"\nHTML written: {out_path}")
            print(f"  Market: {market_name} ({market['state']})")
            print(f"  Result: {counts['VALID']} VALID · {counts['REVIEW']} REVIEW · {counts['STALE']} STALE (of {len(rows)})")
            print(f"  Open in browser:  file://{out_path}\n")
        else:
            _print_console(rows, market)
    finally:
        await conn.close()


def _command_repr(args) -> str:
    parts = ["scripts/verify_listings.py"]
    if args.market and args.market != "cuyahoga":
        parts.append(f"--market {args.market}")
    if args.stratified:
        parts.append(f"--stratified {args.stratified}")
    if args.sample:
        parts.append(f"--sample {args.sample}")
    if args.signal:
        parts.append(f"--signal {args.signal}")
    if args.limit:
        parts.append(f"--limit {args.limit}")
    if args.html:
        parts.append(f"--html {args.html}")
    return " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Tranchi listing verifier")
    ap.add_argument(
        "--market", type=str, default="cuyahoga", choices=list(MARKETS.keys()),
        help="Market to verify: cuyahoga (OH, default) or shelby (TN/Memphis)",
    )
    ap.add_argument("--sample", type=int, default=None, help="mixed random sample size (default 15 if no other selector)")
    ap.add_argument("--stratified", type=int, default=None,
                    help="N per deal source (probate / tax-deed / mortgage / land bank). Random-fills to --sample total.")
    ap.add_argument("--signal", type=str, default=None, help="filter to one signal_type")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--html", type=str, default=None,
                    help="write HTML walk-through to PATH and print short summary; also opens cleanly in a browser")
    args = ap.parse_args()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
