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

_CLOSED = ("closed", "disposed", "terminated", "dismissed")

# ---------------------------------------------------------------------------
# MARKETS config — all market-specific hardcoding lives here.
# To add a new market: add an entry to MARKETS and implement its callables.
# ---------------------------------------------------------------------------

# GENERAL verification guidance shown once at the top of every HTML report.
# Applies to all markets — explains the off-market and vacant-land norms that
# confuse non-experts.
GENERAL_GUIDE = (
    "A real distressed or pre-auction property being OFF-MARKET on Zillow/Redfin is "
    "normal and actually good — it means the owner is NOT trying to sell through the "
    "open market, which is exactly the distress signal we want. A Zillow 'sold' banner "
    "is often years-old noise, NOT a kill — the county current-owner record (Layer 2) "
    "is the authority on who owns the property right now. "
    "Vacant lots (land use 000 or similar, address like '0 <street>') are verified by "
    "PARCEL NUMBER, not street number, and often won't appear on Zillow at all — "
    "that is expected and is not a red flag."
)


def _make_cuyahoga_market() -> dict:
    """Cuyahoga (Cleveland, OH) market config."""

    def registry_link(parcel: str | None, address: str | None) -> str:
        if not parcel:
            return "https://myplace.cuyahogacounty.gov/"
        return f"https://myplace.cuyahogacounty.gov/?parcel={urllib.parse.quote(parcel)}"

    def source_link(signal_type: str, parcel: str | None, case: str | None,
                    sale_date, address: str | None) -> str:
        sig = signal_type or ""
        if sig == "probate":
            return f"https://probate.cuyahogacounty.gov/pa/   (search case {case or '?'})"
        elif sig in ("tax_delinquent_foreclosure", "mortgage_foreclosure"):
            return f"https://www.dln.com/   (search '{case or parcel or ''}' or by sale_date)"
        elif sig == "forfeited_land":
            return "https://cuyahogacounty.gov/fiscal-officer/departments/real-property/forfeited-lands   (Forfeited Lands Locator)"
        elif sig == "land_bank_inventory":
            return "https://landbank.cuyahogalandbank.org/   (property inventory)"
        else:
            return "https://myplace.cuyahogacounty.gov/"

    def offmarket_links(address: str, city: str | None, zip_: str | None) -> tuple[str, str]:
        return _zillow_url(address, city, "OH", zip_), _redfin_url(address, city, "OH", zip_)

    verification_guide = {
        "probate": {
            "valid": (
                "Case Status on ProWare says OPEN or PENDING. Filing year matches the case number. "
                "Decedent name on the case page matches (or reasonably maps to) the parcel owner — "
                "if they match, this is a real estate in probate where heirs may want a fast sale."
            ),
            "red_flags": (
                "Case Status says Closed / Disposed / Terminated / Dismissed (the estate resolved — "
                "lead is dead). Parcel owner name doesn't match the decedent at all (mis-join). "
                "A transfer date on MyPlace at-or-after the case filing year means the property "
                "already sold out of the estate."
            ),
        },
        "tax_delinquent_foreclosure": {
            "valid": (
                "Row still appears on DLN's upcoming-auctions feed with matching parcel and sale date. "
                "By Ohio law (ORC 5721) every row here is already delinquent ≥1 year — no extra check needed. "
                "MyPlace confirms the parcel exists and the owner hasn't paid off."
            ),
            "red_flags": (
                "Row has dropped off the DLN feed (auction cancelled / owner paid up). "
                "Sale date is in the past (STALE — the auction already ran). "
                "MyPlace shows a recent transfer to a new private owner (paid off / sold)."
            ),
        },
        "mortgage_foreclosure": {
            "valid": (
                "Row still appears on DLN's Sheriff Sales feed with matching parcel and sale date. "
                "Owner is facing forced sale by the lender — a distressed timeline. "
                "MyPlace confirms the existing owner name and no recent transfer."
            ),
            "red_flags": (
                "Row has dropped off DLN (sale cancelled / settled). Sale date is past. "
                "MyPlace shows a new owner (property already changed hands)."
            ),
        },
        "forfeited_land": {
            "valid": (
                "Parcel still appears on the Cuyahoga Forfeited Lands Locator (ArcGIS map). "
                "MyPlace owner says 'STATE OF OHIO FORF' — the county holds the deed. "
                "County market value > opening bid (positive equity = real arbitrage opportunity)."
            ),
            "red_flags": (
                "Parcel dropped off the locator (sold at a subsequent auction). "
                "MyPlace owner is a private party (title already transferred). "
                "Opening bid ≥ market value (no equity — skip)."
            ),
        },
        "land_bank_inventory": {
            "valid": (
                "Property still listed on the Cuyahoga Land Bank available-properties page. "
                "MyPlace confirms the parcel address. These are county-owned, priced for community redevelopment — "
                "expect deep discount but also deed restrictions."
            ),
            "red_flags": (
                "Property no longer on the Land Bank page (sold or under contract — "
                "our scraper marks it not_listed next cycle). "
                "MyPlace shows a private owner (already sold)."
            ),
        },
    }

    return {
        "db": "tranchi",
        "state": "OH",
        "state_filter": "l.property_state = 'OH'",
        "deal_sources": (
            "probate",
            "tax_delinquent_foreclosure",
            "forfeited_land",
            "mortgage_foreclosure",
            "land_bank_inventory",
        ),
        "source_sites": {
            "Cuyahoga Probate Court": "probate",
            "Cuyahoga Forfeited Land": "forfeited_land",
            "Cuyahoga Sheriff Sale (DLN)": "tax_delinquent_foreclosure",
            "Cuyahoga Sheriff Sales": "tax_delinquent_foreclosure",
            "Cuyahoga Land Bank": "land_bank_inventory",
        },
        "registry_link": registry_link,
        "registry_label": "Cuyahoga MyPlace — County Parcel Registry",
        "registry_search_hint": (
            "Direct link opens the parcel page for {parcel}. If the page loads but is blank, "
            "paste the parcel number in the search box at top-right."
        ),
        "registry_look_for": (
            "(1) Owner name on the page should match our stored owner. "
            "(2) Situs address should match our stored address. "
            "(3) Click the 'Transfers' tab — the most recent transfer date is our last_sale_date. "
            "If a transfer date is at-or-after the case filing year, our _mark_transferred_listings "
            "guard removes the listing automatically; the absence of one is also a valid lead signal."
        ),
        "source_link": source_link,
        "offmarket_links": offmarket_links,
        "verification_guide": verification_guide,
    }


def _make_shelby_market() -> dict:
    """Shelby County (Memphis, TN) market config."""

    def registry_link(parcel: str | None, address: str | None) -> str:
        # Shelby County Assessor deep-link by parcel (14-char numeric).
        # The Assessor site accepts parcel in the query string; if it doesn't resolve,
        # the human can paste the parcel into the ReGIS viewer or Register of Deeds.
        if parcel:
            assessor_url = (
                "https://www.assessormelvinburgess.com/property_detail.aspx"
                f"?parcelid={urllib.parse.quote(parcel)}"
            )
            return assessor_url
        # Fallback: Assessor homepage — human can search by address.
        return "https://www.assessormelvinburgess.com/"

    def source_link(signal_type: str, parcel: str | None, case: str | None,
                    sale_date, address: str | None) -> str:
        sig = signal_type or ""
        if sig == "tax_deed":
            ts_code = case or parcel or "?"
            return (
                f"https://www.shelbycountytrustee.com/161/Properties-Available-for-Sale"
                f"   (Tax Sale; find parcel {parcel or '?'} / TS code {ts_code})"
            )
        elif sig == "land_bank_inventory":
            return (
                "https://public-sctn.epropertyplus.com/landmgmtpub/app/base/landing"
                f"   (Shelby County Land Bank — search parcel {parcel or '?'})"
            )
        else:
            return "https://www.assessormelvinburgess.com/"

    def offmarket_links(address: str, city: str | None, zip_: str | None) -> tuple[str, str]:
        return _zillow_url(address, city, "TN", zip_), _redfin_url(address, city, "TN", zip_)

    verification_guide = {
        "tax_deed": {
            "valid": (
                "Parcel appears on the Shelby County Trustee's current delinquent tax-sale list. "
                "The Assessor registry owner is a PRIVATE party (a person or LLC) who is behind on taxes — "
                "that is the motivated seller. The property exists at the address in the registry. "
                "TN NOTE: These are PRE-sale listings — the property is 'about to be lost for back taxes.' "
                "After the tax sale, TN gives the original owner ~1 year to redeem (pay back taxes + costs), "
                "so post-sale the deal is not fully locked yet. Phase-1 is the highest-leverage window: "
                "approach BEFORE the sale while the owner is still motivated."
            ),
            "red_flags": (
                "Owner in the Assessor registry is already the county or land bank (not a pre-sale — "
                "the county already took it; treat as a land-bank deal instead). "
                "Parcel was recently transferred to a new private owner (likely paid off or sold — "
                "the back-tax pressure is gone). "
                "Address or parcel doesn't exist in the registry (bad data — skip). "
                "TS code on the Trustee site doesn't match what we stored."
            ),
        },
        "land_bank_inventory": {
            "valid": (
                "Property is currently FOR SALE in the Shelby County Land Bank ePropertyPlus portal. "
                "Assessor registry owner contains 'SHELBY COUNTY TAX SALE' or similar — this confirms "
                "the county acquired the property via tax sale and is now reselling it cheaply for "
                "redevelopment. An asking price is shown on the portal."
            ),
            "red_flags": (
                "Registry owner is a private party (the county no longer holds it — already sold). "
                "Status in the portal is not FOR SALE (under contract, withdrawn, or pending). "
                "Property not found in the portal at all (our data may be stale)."
            ),
        },
        "mmlba": {
            "valid": (
                "Property is on MMLBA's available-properties list at mmlba.org/property-sales/. "
                "Assessor registry owner is 'MEMPHIS METROPOLITAN LAND BANK AUTHORITY' (or close variant) — "
                "this confirms the city genuinely holds the title, acquired for blight removal and "
                "sold to community developers at a discount."
            ),
            "red_flags": (
                "Registry owner is not MMLBA (already sold to a private buyer — our data is stale). "
                "Property is not on the MMLBA available list (sold, withdrawn, or never listed). "
                "Assessor shows a recent transfer date (title changed hands after our scrape)."
            ),
        },
    }

    return {
        "db": "tranchi",
        "state": "TN",
        "state_filter": "l.property_state = 'TN'",
        "deal_sources": (
            "tax_deed",
            "land_bank_inventory",
        ),
        "source_sites": {
            "Shelby County Tax Sale": "tax_deed",
            "Shelby County Land Bank": "land_bank_inventory",
            "Memphis MMLBA": "land_bank_inventory",
        },
        "registry_link": registry_link,
        "registry_label": "Shelby County Assessor — Parcel Registry",
        "registry_search_hint": (
            "Direct link attempts to open the Assessor record for parcel {parcel}. "
            "If it doesn't load, paste the parcel into the ReGIS viewer "
            "(https://gis.shelbycountytn.gov/) or the Register of Deeds search "
            "(https://search.register.shelby.tn.us/search/index.php). "
            "The 14-digit parcel number is the authoritative ID."
        ),
        "registry_look_for": (
            "(1) Owner name on the page should match our stored owner. "
            "(2) Situs address should match our stored address. "
            "(3) For tax_deed leads: owner should be a PRIVATE party (individual or LLC) — "
            "if it already says 'SHELBY COUNTY TAX SALE' the county took it and it belongs in the land-bank category. "
            "(4) For land_bank leads: owner should include 'SHELBY COUNTY TAX SALE' or 'MEMPHIS METROPOLITAN LAND BANK'. "
            "(5) Look for a transfer date — if a new private owner appears after our scrape date, the lead is gone. "
            "Backup parcel lookup links: "
            "ReGIS viewer https://gis.shelbycountytn.gov/ · "
            "Register of Deeds https://search.register.shelby.tn.us/search/index.php · "
            "Assessor https://www.assessormelvinburgess.com/"
        ),
        "source_link": source_link,
        "offmarket_links": offmarket_links,
        "verification_guide": verification_guide,
    }


MARKETS: dict[str, dict] = {
    "cuyahoga": _make_cuyahoga_market(),
    "shelby": _make_shelby_market(),
}

_SELECT_COLS = """
    l.id, l.signal_type, l.source_site, l.property_address, l.property_city,
    l.property_zip, l.sale_date, l.opening_bid_usd, l.appraised_value_usd, l.case_number,
    l.case_status, l.match_confidence, l.match_method, l.status,
    l.source_listing_id, l.address_status,
    l.decedent_name, l.case_title, l.decedent_dod,
    (CURRENT_DATE - l.first_seen_at::date) AS lead_age_days,
    p.owner_name, p.current_market_value, p.current_tax_balance,
    p.land_use_code, p.last_sale_date, p.last_sale_price,
    CURRENT_DATE AS today
"""

_FROM_JOIN = """
    FROM tranchi.listings l
    LEFT JOIN tranchi.parcels p ON p.parcel_number = l.source_listing_id
"""


# ---------------------------------------------------------------- URL helpers


def _redfin_url(addr: str, city: str | None, state: str, zip_: str | None) -> str:
    q = ", ".join(p for p in (addr, city, state, zip_) if p)
    return "https://www.redfin.com/?q=" + urllib.parse.quote(q)


def _zillow_url(addr: str, city: str | None, state: str, zip_: str | None) -> str:
    q = " ".join(p for p in (addr, city, state, zip_) if p)
    return "https://www.zillow.com/homes/" + urllib.parse.quote(q) + "_rb/"


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
    src = market["source_link"](sig, parcel, case, r["sale_date"], r["property_address"])

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
        else:
            chk = f"(1) MyPlace parcel {parcel} exists  (2) Redfin/Zillow check  -- {addr_hint}"
    else:
        # Shelby console CHECK lines
        if sig == "tax_deed":
            chk = (f"(1) Parcel {parcel} on Trustee tax-sale list (TS code {case or '?'})  "
                   f"(2) Assessor owner = PRIVATE party (not county/land bank)  "
                   f"(3) Redfin/Zillow off-market = good (pre-auction)  -- {addr_hint}")
        elif sig == "land_bank_inventory":
            source_site = (r["source_site"] or "")
            if "MMLBA" in source_site:
                chk = (f"(1) On MMLBA available list (mmlba.org)  "
                       f"(2) Assessor owner = 'MEMPHIS METROPOLITAN LAND BANK AUTHORITY'  "
                       f"(3) Redfin/Zillow off-market = expected (city-owned)  -- {addr_hint}")
            else:
                chk = (f"(1) On Shelby Land Bank portal (parcel {parcel})  "
                       f"(2) Assessor owner = 'SHELBY COUNTY TAX SALE' or similar  "
                       f"(3) Redfin/Zillow off-market = expected (county-owned)  -- {addr_hint}")
        else:
            chk = f"(1) Assessor parcel {parcel} exists  (2) Redfin/Zillow check  -- {addr_hint}"

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
        return "STALE", ["sale_date in past"]
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

    first_seen_label = "—"
    if r["lead_age_days"] is not None:
        first_seen_label = f"{int(r['lead_age_days'])} days ago"

    vguide = market["verification_guide"]

    # ---- Layer 1: deal source ----
    if market["state"] == "OH":
        layer1 = _build_layer1_cuyahoga(r, sig, parcel, case, first_seen_label)
    else:
        layer1 = _build_layer1_shelby(r, sig, source_site, parcel, case, first_seen_label, vguide)

    # ---- Layer 2: county registry ----
    last_sale_label = "(not enriched yet)"
    if r["last_sale_date"]:
        if r["last_sale_price"]:
            last_sale_label = f"{r['last_sale_date']} for ${int(r['last_sale_price']):,}"
        else:
            last_sale_label = str(r["last_sale_date"])

    registry_url = market["registry_link"](parcel, r["property_address"])
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
                          vguide: dict) -> dict:
    """Shelby (Memphis, TN) Layer 1 — per signal type."""
    if sig == "tax_deed":
        guide = vguide.get("tax_deed", {})
        return {
            "title": "Shelby County Trustee — Tax Sale (Pre-Auction / Delinquent Tax)",
            "url": "https://www.shelbycountytrustee.com/161/Properties-Available-for-Sale",
            "search_for": (
                f"Open the Trustee's Properties Available for Sale page. "
                f"Search or Ctrl+F for parcel {parcel} or TS code '{case or '?'}'. "
                f"The TS code (e.g. TS2302) identifies the tax-sale batch."
            ),
            "look_for": (
                f"{guide.get('valid', '')} "
                f"Red flags: {guide.get('red_flags', '')}"
            ),
            "stored": [
                ("Parcel (14-digit)", parcel),
                ("TS Code / Case Number", case or "—"),
                ("Source Site", source_site),
                ("Property Address", r["property_address"] or "—"),
                ("First Seen By Us", first_seen_label),
                ("Owner (Assessor — should be private party)", r["owner_name"] or "(not enriched)"),
            ],
        }
    elif sig == "land_bank_inventory" and "MMLBA" in source_site:
        guide = vguide.get("mmlba", {})
        return {
            "title": "MMLBA — Memphis Metropolitan Land Bank Authority",
            "url": "https://mmlba.org/property-sales/",
            "search_for": (
                f"Open the MMLBA property-sales page. Search or Ctrl+F for "
                f"address '{r['property_address']}' or parcel {parcel}."
            ),
            "look_for": (
                f"{guide.get('valid', '')} "
                f"Red flags: {guide.get('red_flags', '')}"
            ),
            "stored": [
                ("Parcel (14-digit)", parcel),
                ("Source Site", source_site),
                ("Property Address", r["property_address"] or "—"),
                ("First Seen By Us", first_seen_label),
                ("Owner (Assessor — should be MMLBA)", r["owner_name"] or "(not enriched)"),
            ],
        }
    elif sig == "land_bank_inventory":
        guide = vguide.get("land_bank_inventory", {})
        return {
            "title": "Shelby County Land Bank — ePropertyPlus Portal",
            "url": "https://public-sctn.epropertyplus.com/landmgmtpub/app/base/landing",
            "search_for": (
                f"Open the Shelby County Land Bank portal. Search for parcel {parcel} "
                f"or address '{r['property_address']}'. Properties listed FOR SALE are the active inventory."
            ),
            "look_for": (
                f"{guide.get('valid', '')} "
                f"Red flags: {guide.get('red_flags', '')}"
            ),
            "stored": [
                ("Parcel (14-digit)", parcel),
                ("Source Site", source_site),
                ("Property Address", r["property_address"] or "—"),
                ("First Seen By Us", first_seen_label),
                ("Owner (Assessor — should be Shelby County)", r["owner_name"] or "(not enriched)"),
            ],
        }
    else:
        return {
            "title": "Source not classified (Shelby)",
            "url": "https://www.assessormelvinburgess.com/",
            "search_for": f"Paste parcel {parcel} in the Assessor search.",
            "look_for": "Confirm the parcel exists and address matches.",
            "stored": [
                ("Signal Type", sig or "—"),
                ("Source Site", source_site),
            ],
        }


# ----------------------------------------------------------------- Fetching


async def _fetch_signal(conn, signal: str, limit: int, market: dict) -> list:
    state_filter = market["state_filter"]
    return await conn.fetch(
        f"""
        SELECT {_SELECT_COLS}
        {_FROM_JOIN}
        WHERE l.status = 'active' AND l.duplicate_of IS NULL
          AND {state_filter}
          AND l.signal_type = $1
        ORDER BY random()
        LIMIT {int(limit)}
        """,
        signal,
    )


async def _fetch_random(conn, limit: int, exclude_ids: set, market: dict) -> list:
    state_filter = market["state_filter"]
    rows = await conn.fetch(
        f"""
        SELECT {_SELECT_COLS}
        {_FROM_JOIN}
        WHERE l.status = 'active' AND l.duplicate_of IS NULL
          AND {state_filter}
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
        zillow_url, redfin_url = market["offmarket_links"](addr, city, zip_)
        registry_url = market["registry_link"](parcel, addr)
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
