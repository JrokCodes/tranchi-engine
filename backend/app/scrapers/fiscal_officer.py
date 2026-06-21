"""
Cuyahoga County Fiscal Officer (MyPlace) scraper.

Site: https://myplace.cuyahogacounty.gov/
Structure: ASP.NET MVC app wrapping ArcGIS/ESRI data. Despite appearing to be a
           React SPA, search results are SERVER-SIDE RENDERED into the initial HTML
           response. Raw httpx GET with BeautifulSoup parses all hit-list rows without
           Playwright. Detail-page property data (General Information) requires a
           follow-up POST to /MainPage/PropertyData with hidden form fields extracted
           from the search result HTML.

           Tax By Year flags (Foreclosure / Cert. Pending / Cert. Sold / Payment Plan /
           Balance Due) require a further POST chain to /MainPage/LegacyTaxes, but that
           endpoint renders its data via a secondary JS call in a browser session. For
           the cron bulk-pull path, tax flag enrichment uses Playwright only on parcels
           where the hit-list fields suggest distress (balance_due > 0 or any flag set).
           For the on-demand search_by_owner path, tax enrichment is opt-in via
           enrich_tax=True (default False, too slow for interactive lookups).

URL pattern (base64-encoded):
    /<b64(term)>?city=<b64(cityCode)>&searchBy=<b64(mode)>
    e.g. owner "SMITH" entire county:
    /U01JVEg=?city=OTk=&searchBy=T3duZXI=

Search hit-list HTML structure (AddressInfo <ul>):
    Groups of 7 <li> items per parcel:
      [0] <a onclick="selectParcel('<9-digit-pin>')">DDD-NN-NNN</a>  ← parcel link
      [1] OWNER NAME
      [2] SITUS ADDRESS LINE 1
      [3] CITY, STATE ZIP
      [4] (empty spacer)
      [5] (empty spacer)
      [6] (empty spacer)  -- separator before next group

PropertyData POST fields extracted from hit-list form:
    hdnParcelId, hdnSearchPropertyNumber, hdnSearchDeededOwner,
    hdnSearchPhysicalAddress, hdnSearchParcelCity, hdnSearchParcelZip,
    hdnSearchPropertyClass, hdnSearchTaxLuc, hdnSearchTaxLucDescription,
    hdnSearchNeighborhoodCode, hdnSearchLegalDescription, (others)

Detail page returns via dataBody div:
    Description, Tax District, School District, Property Class, Land Use,
    Zoning Use, Neighborhood Code, Total Buildings, utilities, and more.

LegacyTaxes POST chain (tax flags):
    Requires Playwright because the flag values are rendered by a secondary
    in-browser XHR after the initial HTML loads. Playwright used only when
    enrich_tax=True (on-demand) or in bulk-distress-flag-enrichment cron pass.

INVARIANT: parcel_number format in tranchi.parcels is DDD-NN-NNN (with hyphens).
           The 9-digit form without hyphens (e.g. '02213042') is used for POST
           params. Both are stored: parcel_number (hyphened) as PK,
           full_record['parcel_pin_raw'] (no hyphens) for convenience.

INVARIANT: search_by_owner returns ALL candidate parcels including ambiguous
           multi-owner matches. Caller (probate.py) is responsible for filtering
           on confidence and ambiguous flag. Do not pre-filter here.

Playwright dependency: playwright + chromium (installed in .venv).
  Install: pip install playwright && playwright install chromium
  Added to requirements.txt under optional [scraper] group.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.market_config import state_for_market
from app.scrapers.base import ListingScraper
from app.scrapers.models import RawListing
from app.scrapers.user_agents import random_ua
from app.scrapers._time import today_et
from app.scrapers.oh_cities import CUYAHOGA_MUNICIPALITIES

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_BASE_URL = "https://myplace.cuyahogacounty.gov"
_PROPERTY_DATA_URL = f"{_BASE_URL}/MainPage/PropertyData"
_LEGACY_TAXES_URL = f"{_BASE_URL}/MainPage/LegacyTaxes"

# City code for "Entire County" in the MyPlace dropdown
_ENTIRE_COUNTY_CODE = "99"

# Search mode names (base64-encoded in the URL)
_MODE_OWNER = "Owner"
_MODE_PARCEL = "Parcel"
_MODE_ADDRESS = "Address"

# Request delays (seconds)
_SEARCH_DELAY = (0.8, 1.5)    # jitter range between owner-sweep searches
_DETAIL_DELAY = (0.5, 1.2)    # jitter range between detail page fetches
_TIMEOUT = 30.0

# Levenshtein fuzzy match threshold (0.0–1.0) for search_by_owner
_FUZZY_THRESHOLD = 0.75

# Per-token similarity at/above which a single name part counts as a STRONG match.
# _name_confidence requires ≥2 strong token matches (given + surname) so a shared
# surname alone can never confirm a join. See reference/JOIN-PRECISION.md.
_STRONG_TOKEN = 0.85

# For the bulk delinquent sweep: letters A-Z to cover all owner names
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Maximum results to enrich with detail+tax for a single search_by_owner call
# (avoids runaway Playwright sessions on very common names like "SMITH")
_MAX_DETAIL_ENRICH = 50


# ─────────────────────────────────────────────────────────────────────────────
# Public data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParcelMatch:
    """Result from search_by_owner. Used by probate.py for the name→parcel join."""
    parcel_number: str          # DDD-NN-NNN (hyphened, PK in tranchi.parcels)
    owner_name: str             # As listed on MyPlace (may include co-owners)
    situs_address: str          # Physical location of the parcel
    property_city: str | None
    property_zip: str | None
    market_value: float | None  # Current market value from Values tab (if enriched)
    tax_balance: float | None   # Balance Due from Tax By Year (if enriched)
    confidence: float           # 0.0–1.0: name match quality
    ambiguous: bool             # True when multiple owners match or name is non-exact
    full_record: dict[str, Any] # All available fields (varies by enrichment level)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _b64(s: str) -> str:
    """Base64-encode a string as used in the MyPlace URL pattern."""
    return base64.b64encode(s.encode()).decode()


def _search_url(term: str, city_code: str = _ENTIRE_COUNTY_CODE, mode: str = _MODE_OWNER) -> str:
    """Construct the MyPlace search URL for the given term, city, and mode."""
    return f"{_BASE_URL}/{_b64(term)}?city={_b64(city_code)}&searchBy={_b64(mode)}"


def _default_headers() -> dict[str, str]:
    return {
        "User-Agent": random_ua(),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": _BASE_URL + "/",
    }


def _jitter(lo: float, hi: float) -> float:
    return random.uniform(lo, hi)


def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for name comparison."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    return re.sub(r"\s+", " ", name).strip()


def _levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(
                prev[j] + 1,
                curr[-1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            ))
        prev = curr
    return prev[-1]


def _name_confidence(query: str, candidate: str) -> float:
    """
    Compute a 0.0–1.0 confidence that candidate owner name matches the query.

    PRECISION-FIRST (see Babel reference/JOIN-PRECISION.md). A probate case must join
    a parcel only when the FULL name aligns — both the given name AND the surname.
    The old logic AVERAGED per-token similarity (and short-circuited any token-subset
    to 0.9), so a candidate sharing only the surname scored ~0.83 (surname=1.0 dragging
    up a weak given-name match) and cleared the 0.75 bar. That attached one decedent to
    every same-surname owner county-wide — case 2026EST304870 → 775 listings, 68 real.

    Fix: require at least TWO significant query tokens to each find a STRONG match
    (≥ _STRONG_TOKEN) in the candidate, and score on those strong matches. A surname-only
    (single-token) query, or a candidate that aligns on only one token, returns a score
    below threshold — so "DARVIS HARRIS" no longer matches "HARRIS, MARY". Extra tokens
    (middle names, suffixes) don't penalize: we require ≥2 strong, not all.
    """
    qn = _normalize_name(query)
    cn = _normalize_name(candidate)

    if qn == cn:
        return 1.0

    # Significant tokens only: drop 1-char initials and generational suffixes so a
    # missing middle initial / "JR" neither helps nor hurts the comparison.
    _SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
    q_tokens = [t for t in qn.split() if len(t) >= 2 and t not in _SUFFIXES]
    c_tokens = [t for t in cn.split() if len(t) >= 2 and t not in _SUFFIXES]

    # Cannot confirm identity on a single token (surname alone) — this IS the over-match
    # trap. Refuse rather than matching every same-surname owner.
    if len(q_tokens) < 2 or not c_tokens:
        return 0.0

    # Best fuzzy similarity of each query token against any candidate token.
    per_token = [
        max(1.0 - _levenshtein(qt, ct) / max(len(qt), len(ct), 1) for ct in c_tokens)
        for qt in q_tokens
    ]
    strong = [s for s in per_token if s >= _STRONG_TOKEN]

    # Require ≥2 name parts to align strongly (given + family). One strong token
    # (the shared surname) is NOT enough.
    if len(strong) < 2:
        return round(max(per_token) * 0.5, 4)  # always < threshold → filtered out

    return round(sum(strong) / len(strong), 4)


def _parse_parcel_number(raw: str) -> str:
    """Normalize parcel number to DDD-NN-NNN format (hyphened)."""
    raw = raw.strip().upper()
    # Already hyphened (e.g. '022-13-042')
    if re.match(r"^\d{3}-\d{2}-\d{3}$", raw):
        return raw
    # 8- or 9-digit (e.g. '02213042' or '022130042')
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 8:
        return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    if len(digits) == 9:
        return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    return raw  # return as-is if we can't parse


def _parse_city_zip(city_state_zip: str) -> tuple[str | None, str | None]:
    """
    Parse 'CLEVELAND, OH.  44111' → ('Cleveland', '44111').
    Returns (None, None) if unparseable.
    """
    s = city_state_zip.strip().replace("OH.", "OH").replace("..", ".")
    # Pattern: CITY, OH  ZIP
    m = re.match(r"^([^,]+),?\s*OH\s*(\d{5}(?:-\d{4})?)?", s, re.IGNORECASE)
    if m:
        city = m.group(1).strip().title() if m.group(1) else None
        zipcode = m.group(2).strip() if m.group(2) else None
        return city, zipcode
    return None, None


def _parse_dollar(s: str) -> float | None:
    """Parse '$1,894.00' → 1894.0. Returns None on failure."""
    if not s:
        return None
    try:
        return float(re.sub(r"[^0-9.]", "", s))
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Hit-list HTML parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_hit_list(html: str) -> list[dict[str, Any]]:
    """
    Parse the AddressInfo <ul> from a MyPlace search result page.

    Returns a list of raw hit dicts with keys:
        parcel_number, parcel_pin_raw, owner_name, situs_address,
        property_city, property_zip, property_class, land_use_code,
        land_use_description, legal_description, neighborhood_code,
        property_type, search_result_count

    Each block in the <ul> is 7 <li> items:
      [0] parcel ID link  (onclick="selectParcel('<9-digit-pin>')")
      [1] owner name
      [2] situs address line 1
      [3] CITY, OH  ZIP
      [4–6] spacer / separator rows (empty or blank)

    Hidden form fields in the page embed additional per-parcel metadata:
    hdnSearchPropertyClass, hdnSearchTaxLuc, hdnSearchTaxLucDescription,
    hdnSearchNeighborhoodCode, hdnSearchLegalDescription — but those are
    populated only for the LAST selected parcel in a Playwright session.
    For the bulk httpx path, we only get what's in the list HTML.
    """
    soup = BeautifulSoup(html, "lxml")
    addr_ul = soup.find("ul", id="AddressInfo")
    if not addr_ul:
        logger.warning("AddressInfo ul not found in search result HTML")
        return []

    lis = addr_ul.find_all("li")
    results: list[dict[str, Any]] = []

    # Parse result count from "N record(s) found" text if present
    count_text = soup.get_text()
    count_m = re.search(r"(\d+)\s+records?\s+(?:is\s+|are\s+)?found", count_text, re.I)
    total_count = int(count_m.group(1)) if count_m else None

    # Each parcel block starts with a <li> containing an <a> with onclick=selectParcel(...)
    i = 0
    while i < len(lis):
        li = lis[i]
        anchor = li.find("a", onclick=re.compile(r"selectParcel"))
        if anchor:
            # Extract parcel pin from onclick
            m = re.search(r"selectParcel\('(\d+)'\)", anchor.get("onclick", ""))
            if not m:
                i += 1
                continue
            pin_raw = m.group(1)
            parcel_display = anchor.get_text(strip=True)  # e.g. '022-13-042'

            # Next 3 li items: owner, address line 1, city/state/zip
            owner = lis[i + 1].get_text(strip=True) if (i + 1) < len(lis) else ""
            addr1 = lis[i + 2].get_text(strip=True) if (i + 2) < len(lis) else ""
            city_zip_raw = lis[i + 3].get_text(strip=True) if (i + 3) < len(lis) else ""

            city, zipcode = _parse_city_zip(city_zip_raw)
            parcel_num = _parse_parcel_number(parcel_display or pin_raw)

            results.append({
                "parcel_number": parcel_num,
                "parcel_pin_raw": pin_raw,
                "owner_name": owner,
                "situs_address": addr1,
                "property_city": city,
                "property_zip": zipcode,
                # Fields below require detail-page POST to populate
                "property_class": None,
                "land_use_code": None,
                "land_use_description": None,
                "legal_description": None,
                "neighborhood_code": None,
                "property_type": None,
                "search_result_count": total_count,
                # Detail enrichment fields (populated later)
                "tax_district": None,
                "school_district": None,
                "zoning_use": None,
                "total_buildings": None,
                "description": None,
                # Tax flags (populated by LegacyTaxes enrichment only)
                "flag_foreclosure": None,
                "flag_cert_pending": None,
                "flag_cert_sold": None,
                "flag_payment_plan": None,
                "tax_balance_due": None,
                "current_market_value": None,
                # Metadata
                "source_url": _BASE_URL,
                "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
            })
            i += 7  # skip the 3 data lis + 3 spacer lis
        else:
            i += 1

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Detail-page enrichment (httpx POST to /MainPage/PropertyData)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_property_data(html: str) -> dict[str, Any]:
    """
    Parse the General Information section from a /MainPage/PropertyData response.

    The server-rendered dataBody div contains label/value pairs in consecutive
    text nodes. We pair them up to produce a flat dict.
    """
    soup = BeautifulSoup(html, "lxml")
    data_body = soup.find("div", class_="dataBody")
    if not data_body:
        return {}

    text = data_body.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Known label → key mapping
    _label_map: dict[str, str] = {
        "Description": "legal_description",
        "Tax District Description": "tax_district",
        "School District": "school_district",
        "Property Class": "property_class",
        "Land Use": "land_use",
        "Ext Land Use": "ext_land_use",
        "Abt Land Use": "abt_land_use",
        "Tax Abatement": "tax_abatement",
        "Neighborhood Code": "neighborhood_code",
        "Total Associated Parcels": "total_associated_parcels",
        "Total Buildings": "total_buildings",
        "Gas": "util_gas",
        "Road Type": "road_type",
        "Electricity": "util_electricity",
        "Sewer": "util_sewer",
        "Water": "util_water",
        "Forest Land": "forest_land",
        "Mineral Rights": "mineral_rights",
        "Zoning Use": "zoning_use",
    }

    result: dict[str, Any] = {}
    known_labels = set(_label_map.keys())
    i = 0
    while i < len(lines):
        label = lines[i]
        if label in known_labels and i + 1 < len(lines):
            next_val = lines[i + 1]
            # Avoid consuming the next label as a value
            if next_val not in known_labels and next_val not in ("Top", "View Map"):
                result[_label_map[label]] = next_val
                i += 2
                continue
        i += 1

    # Parse land use code and description from "5100 - 1-FAMILY PLATTED LOT"
    if "land_use" in result:
        lu_m = re.match(r"^(\d+)\s*-\s*(.+)$", result["land_use"])
        if lu_m:
            result["land_use_code"] = lu_m.group(1)
            result["land_use_description"] = lu_m.group(2).strip()
        del result["land_use"]

    # Also extract the taxesForm hidden inputs for the LegacyTaxes POST chain
    taxes_form = soup.find("form", id="taxesForm")
    tax_fields: dict[str, str] = {}
    if taxes_form:
        for inp in taxes_form.find_all("input"):
            if inp.get("name"):
                tax_fields[inp["name"]] = inp.get("value", "")
        result["_taxes_form_fields"] = tax_fields

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Tax flag enrichment (Playwright POST to /MainPage/LegacyTaxes)
# ─────────────────────────────────────────────────────────────────name────────────

async def _enrich_tax_flags_playwright(
    parcel_pin_raw: str,
    search_choice: str = "Owner",
    search_text: str = "",
    search_city: str = _ENTIRE_COUNTY_CODE,
) -> dict[str, Any]:
    """
    Use Playwright to get Tax By Year data for a single parcel.

    The LegacyTaxes endpoint returns an HTML page, but the actual flag values
    (Foreclosure / Cert. Pending / Cert. Sold / Payment Plan / Balance Due)
    are populated via a secondary browser-side POST triggered after initial load.
    Playwright is required to capture this rendered data.

    Returns a dict with keys:
        flag_foreclosure, flag_cert_pending, flag_cert_sold, flag_payment_plan,
        tax_balance_due, current_market_value, tax_year
    Returns {} on failure (non-fatal — caller logs and continues).
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright not installed — skipping tax flag enrichment")
        return {}

    result: dict[str, Any] = {}
    search_url = _search_url(search_text or parcel_pin_raw, search_city, _MODE_OWNER)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(
                user_agent=random_ua(),
                viewport={"width": 1280, "height": 900},
            )

            legacy_tax_html: str | None = None

            async def on_response(resp: Any) -> None:
                nonlocal legacy_tax_html
                if "LegacyTaxes" in resp.url and resp.request.method == "POST":
                    try:
                        body = await resp.text()
                        if len(body) > 10000:  # real content, not an error page
                            legacy_tax_html = body
                    except Exception:
                        pass

            page = await ctx.new_page()
            page.on("response", on_response)

            # Load the search page to establish session
            await page.goto(search_url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(800)

            # Select the parcel via JS
            await page.evaluate(f"selectParcel('{parcel_pin_raw}')")
            await page.wait_for_timeout(2000)

            # Click Tax By Year tab
            await page.evaluate("""
                () => {
                    var links = document.querySelectorAll('a');
                    for (var link of links) {
                        if (link.textContent.trim() === 'Tax By Year') {
                            link.click();
                            return;
                        }
                    }
                }
            """)
            await page.wait_for_timeout(4000)

            await browser.close()

        if legacy_tax_html:
            result = _parse_legacy_taxes(legacy_tax_html)
        else:
            logger.debug("LegacyTaxes: no response captured for parcel %s", parcel_pin_raw)

    except Exception as exc:
        logger.warning("Tax flag enrichment failed for %s: %s", parcel_pin_raw, exc)

    return result


def _parse_legacy_taxes(html: str) -> dict[str, Any]:
    """
    Parse the LegacyTaxes HTML response for tax flags and balance.

    Fields extracted (all from Tax By Year tab):
        flag_foreclosure       (Y/N → bool)
        flag_cert_pending      (Y/N → bool)
        flag_cert_sold         (Y/N → bool)
        flag_payment_plan      (Y/N → bool)
        tax_balance_due        (float, dollars)
        current_market_value   (float, Total Value Taxable Market)
        tax_year               (str, e.g. "2025 Pay 2026")
    """
    soup = BeautifulSoup(html, "lxml")
    data_body = soup.find("div", class_="dataBody")
    if not data_body:
        return {}

    text = data_body.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    result: dict[str, Any] = {}

    _flag_labels = {
        "Foreclosure": "flag_foreclosure",
        "Cert. Pending": "flag_cert_pending",
        "Cert. Sold": "flag_cert_sold",
        "Payment Plan": "flag_payment_plan",
    }
    _dollar_labels = {
        "Balance Due": "tax_balance_due",
        "Total Value (Taxable Market)": "current_market_value",
    }
    _text_labels = {
        "Tax Year": "tax_year",
        "Taxset": "taxset",
    }

    all_labels = set(_flag_labels) | set(_dollar_labels) | set(_text_labels)

    i = 0
    while i < len(lines):
        label = lines[i]
        if label in all_labels and i + 1 < len(lines):
            val = lines[i + 1]
            if label in _flag_labels:
                result[_flag_labels[label]] = val.upper().startswith("Y")
            elif label in _dollar_labels:
                result[_dollar_labels[label]] = _parse_dollar(val)
            elif label in _text_labels:
                result[_text_labels[label]] = val
            i += 2
            continue
        i += 1

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Sale-data enrichment (Playwright capture of /MainPage/PropertyData Transfer History)
# Verified end-to-end 2026-05-28 against 8 parcels — deterministic when the page's
# #btnTransferInfo is clicked. The Transfer History section of PropertyData lists every
# recorded deed transfer with date + sales amount. Pairs with the LegacyTaxes path.
# ─────────────────────────────────────────────────────────────────────────────

def _parse_transfer_history(html: str) -> dict[str, Any]:
    """Parse the Transfer History section out of a /MainPage/PropertyData response.

    Returns:
        {
          "transfers_present": bool,
          "last_sale_date":   date | None,   # most recent transfer date
          "last_sale_price":  float | None,  # most recent Sales Amt (dollars)
          "transfer_count":   int,           # total transfers recorded
        }

    Conservative: returns transfers_present=False with NULL fields when the section
    contains "No Information Found" OR no parseable dates.
    """
    from datetime import date as _date

    out: dict[str, Any] = {
        "transfers_present": False,
        "last_sale_date": None,
        "last_sale_price": None,
        "transfer_count": 0,
    }
    if not html:
        return out

    idx = html.lower().find("<h3>transfer history</h3>")
    if idx < 0:
        return out
    block = html[idx:idx + 12000]  # generous window; typical block < 8KB
    if re.search(r"No Information Found|unable to find any Transfers", block[:3000], re.IGNORECASE):
        return out

    # Pull every "Transfer Date: M/D/YYYY" pattern (the structured datum); fall back
    # to bare M/D/YYYY scans if structure varies.
    dated = re.findall(r"Transfer Date:\s*(\d{1,2}/\d{1,2}/(?:19|20)\d{2})", block)
    if not dated:
        dated = re.findall(r"\b(\d{1,2}/\d{1,2}/(?:19|20)\d{2})\b", block)
    if not dated:
        return out

    def _to_date(d: str) -> _date:
        m, day, y = d.split("/")
        return _date(int(y), int(m), int(day))

    parsed = []
    for d in dated:
        try:
            parsed.append(_to_date(d))
        except Exception:
            continue
    if not parsed:
        return out

    out["transfers_present"] = True
    out["transfer_count"] = len(parsed)
    out["last_sale_date"] = max(parsed)

    # Sale price: look for "Sales Amt" followed by a dollar amount near the latest transfer.
    # The block is ordered with most-recent first, so the first Sales Amt > $0 is best.
    price_m = re.search(r"Sales Amt[^$]*\$([\d,]+(?:\.\d{2})?)", block, re.IGNORECASE)
    if price_m:
        try:
            out["last_sale_price"] = float(price_m.group(1).replace(",", ""))
        except Exception:
            pass

    return out


async def _enrich_sale_data_playwright(
    parcel_pin_raw: str,
    *,
    headless: bool = True,
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Fetch /MainPage/PropertyData for a parcel via Playwright and extract sale data.

    Mirrors _enrich_tax_flags_playwright's shape (async_playwright + chromium +
    response-capture). Returns dict from _parse_transfer_history; empty {} on failure
    (non-fatal so the caller can log and move on).

    Verified 2026-05-28: 100% extraction success on 8 test parcels when the
    #btnTransferInfo button is clicked after page load.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright not installed — skipping sale enrichment")
        return {}

    # Build deep-link URL exactly like a browser visit
    parcel_b64 = _b64(parcel_pin_raw)
    city_b64 = _b64(_ENTIRE_COUNTY_CODE)
    mode_b64 = _b64("Parcel")
    url = f"{_BASE_URL}/{parcel_b64}?city={city_b64}&searchBy={mode_b64}"

    property_data_html: str | None = None

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=headless)
            ctx = await browser.new_context(
                user_agent=random_ua(),
                viewport={"width": 1280, "height": 900},
            )
            page = await ctx.new_page()

            async def on_response(resp: Any) -> None:
                nonlocal property_data_html
                try:
                    if "myplace.cuyahogacounty.gov/MainPage/PropertyData" in resp.url:
                        body = await resp.text()
                        # Keep the largest capture (sometimes the page fires twice)
                        if property_data_html is None or len(body) > len(property_data_html):
                            property_data_html = body
                except Exception:
                    pass

            page.on("response", on_response)

            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            # Click the Transfers button to fire the PropertyData XHR (deterministic).
            try:
                await page.locator("#btnTransferInfo").click(timeout=10000)
            except Exception:
                # Fallback: any visible Transfers control on the page
                for sel in ("button:has-text('Transfers')", "[value='Transfers']"):
                    try:
                        await page.locator(sel).first.click(timeout=3000)
                        break
                    except Exception:
                        pass
            await asyncio.sleep(4)  # let the XHR complete

            await browser.close()
    except Exception as exc:
        logger.warning("Sale enrichment failed for parcel %s: %s", parcel_pin_raw, exc)
        return {}

    if not property_data_html:
        logger.debug("PropertyData: no response captured for parcel %s", parcel_pin_raw)
        return {}

    return _parse_transfer_history(property_data_html)


# ─────────────────────────────────────────────────────────────────────────────
# Core HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_search_page(
    client: httpx.AsyncClient,
    term: str,
    city_code: str = _ENTIRE_COUNTY_CODE,
    mode: str = _MODE_OWNER,
) -> str | None:
    """
    Fetch a MyPlace search result page via httpx GET.
    Returns HTML string on success, None on failure.
    """
    url = _search_url(term, city_code, mode)
    for attempt in range(3):
        try:
            resp = await client.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPStatusError as exc:
            logger.warning("Search HTTP %d for term=%r (attempt %d): %s", exc.response.status_code, term, attempt + 1, exc)
        except httpx.RequestError as exc:
            logger.warning("Search request error for term=%r (attempt %d): %s", term, attempt + 1, exc)
        if attempt < 2:
            await asyncio.sleep(2 ** attempt)
    return None


async def _fetch_property_data(
    client: httpx.AsyncClient,
    hit: dict[str, Any],
    search_text: str,
    search_city: str = _ENTIRE_COUNTY_CODE,
) -> dict[str, Any]:
    """
    POST to /MainPage/PropertyData to get General Information for one parcel.
    Returns a dict of parsed fields, empty dict on failure.
    """
    data = {
        "hdnParcelId": hit["parcel_pin_raw"],
        "hdnListId": "",
        "hdnButtonClicked": "General Information",
        "hdnSearchChoice": _MODE_OWNER,
        "hdnSearchText": search_text,
        "hdnSearchCity": search_city,
        "hdnSearchPropertyNumber": hit["parcel_number"],
        "hdnSearchDeededOwner": hit["owner_name"],
        "hdnSearchPhysicalAddress": hit["situs_address"],
        "hdnSearchParelUnit": "",
        "hdnSearchParcelCity": hit["property_city"] or "",
        "hdnSearchParcelZip": hit["property_zip"] or "",
        "hdnSearchPropertyType": hit.get("property_type") or "",
        "hdnSearchTaxLuc": hit.get("land_use_code") or "",
        "hdnSearchPropertyClass": hit.get("property_class") or "",
        "hdnSearchTaxLucDescription": hit.get("land_use_description") or "",
        "hdnSearchLegalDescription": hit.get("legal_description") or "",
        "hdnSearchNeighborhoodCode": hit.get("neighborhood_code") or "",
    }
    try:
        resp = await client.post(
            _PROPERTY_DATA_URL,
            data=data,
            headers={
                **_default_headers(),
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": _BASE_URL,
                "Referer": _BASE_URL + "/",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return _parse_property_data(resp.text)
    except Exception as exc:
        logger.warning("PropertyData POST failed for %s: %s", hit["parcel_number"], exc)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# FiscalOfficerScraper — bulk-pull mode (cron every 3h)
# ─────────────────────────────────────────────────────────────────────────────

class FiscalOfficerScraper(ListingScraper):
    """
    Bulk parcel pull for Cuyahoga County.

    Two entry points for the sweep:

    fetch_parcels() — returns raw hit dicts (list[dict]). Used by the registry
        path in run.py: feeds directly into upsert_parcels() to populate
        tranchi.parcels without touching tranchi.listings.

    fetch_and_parse() — convenience wrapper around fetch_parcels() that converts
        hits to RawListing objects. Kept for ad-hoc use; the orchestrator does NOT
        call this for fiscal_officer (it would flood tranchi.listings with ~20K
        registry rows).

    The bulk path does NOT enrich Tax By Year flags (too slow at scale). A
    separate enrichment pass should run on parcels where delinquent_flag is True
    or where any distress signals have been observed from other sources.

    Delinquent flag detection in bulk mode: relies on search result count anomalies
    and the downstream probate / sheriff signal join. Per-parcel tax enrichment
    is left to the on-demand path or a targeted enrichment cron.
    """

    site_name = "Cuyahoga Fiscal Officer"

    def __init__(
        self,
        sweep_letters: list[str] | None = None,
        enrich_detail: bool = True,
        city_code: str = _ENTIRE_COUNTY_CODE,
    ) -> None:
        """
        Args:
            sweep_letters: Letters to sweep (default: A–Z). Pass a subset for
                           incremental or partial runs.
            enrich_detail: If True, POST to /MainPage/PropertyData for each parcel
                           to get General Information fields. Slower but richer.
                           Pass False for the registry sweep (light: parcel# +
                           owner + address only, ~26 page fetches, fast).
            city_code: MyPlace city code (default: "99" = entire county).
        """
        self.sweep_letters = sweep_letters or list(_ALPHABET)
        self.enrich_detail = enrich_detail
        self.city_code = city_code

    async def fetch_parcels(self) -> list[dict[str, Any]]:
        """
        Sweep owner names A–Z across the entire county and return raw hit dicts.

        This is the primary entry point for the registry path in run.py. Returning
        raw dicts (not RawListings) lets the caller feed them directly into
        upsert_parcels() without a lossy round-trip through RawListing fields.

        With enrich_detail=False (registry mode): fetches ~26 search pages only,
        returning parcel_number + owner_name + situs_address. Fast (~minutes).
        With enrich_detail=True: adds a per-parcel detail POST (~hours at scale).
        """
        all_hits: list[dict[str, Any]] = []
        seen_parcels: set[str] = set()

        headers = _default_headers()
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=_TIMEOUT,
        ) as client:
            for letter in self.sweep_letters:
                logger.info("Fiscal Officer sweep: letter=%s", letter)
                html = await _fetch_search_page(client, letter, self.city_code)
                if html is None:
                    logger.warning("Skipping letter %s — fetch failed", letter)
                    await asyncio.sleep(_jitter(*_SEARCH_DELAY))
                    continue

                hits = _parse_hit_list(html)
                logger.info("Letter %s: %d hits", letter, len(hits))

                for hit in hits:
                    pn = hit["parcel_number"]
                    if pn in seen_parcels:
                        continue
                    seen_parcels.add(pn)

                    if self.enrich_detail:
                        detail = await _fetch_property_data(client, hit, letter, self.city_code)
                        hit.update({k: v for k, v in detail.items() if not k.startswith("_")})
                        await asyncio.sleep(_jitter(*_DETAIL_DELAY))

                    all_hits.append(hit)

                await asyncio.sleep(_jitter(*_SEARCH_DELAY))

        logger.info("Fiscal Officer sweep complete: %d unique parcels", len(all_hits))
        return all_hits

    async def fetch_and_parse(self) -> list[RawListing]:
        """
        Sweep owner names A–Z and return results as RawListing objects.

        Thin wrapper around fetch_parcels() for ad-hoc use. run.py does NOT call
        this for fiscal_officer — it calls fetch_parcels() + upsert_parcels()
        directly to avoid flooding tranchi.listings with registry rows.
        """
        hits = await self.fetch_parcels()
        return [_hit_to_raw_listing(h) for h in hits]


def _hit_to_raw_listing(hit: dict[str, Any]) -> RawListing:
    """Convert a parsed hit dict to a RawListing for tranchi.listings."""
    addr = hit.get("situs_address", "")
    city = hit.get("property_city")
    return RawListing(
        source_site="Cuyahoga Fiscal Officer",
        property_address=addr,
        property_city=city,
        property_county="Cuyahoga",
        property_state="OH",
        property_zip=hit.get("property_zip"),
        case_number=None,
        status="active",
        signal_type="parcel_registry",
        source_listing_id=hit.get("parcel_number"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Parcel upsert helpers (for run.py to call after fetch_and_parse)
# ─────────────────────────────────────────────────────────────────────────────

async def upsert_parcels(
    pool: Any,  # asyncpg.Pool
    hits: list[dict[str, Any]],
    dry_run: bool = False,
    *,
    market: str,
) -> dict[str, int]:
    """
    Upsert a list of parcel hit dicts into tranchi.parcels.

    Called by run.py immediately after fetch_and_parse(). The hits list is
    the raw dicts returned by _parse_hit_list (before conversion to RawListing).

    `market` is the market slug these parcels belong to (the caller always knows it
    statically — fiscal_officer/dln/probate => 'cuyahoga', shelby_parcels => 'shelby').
    It tags every row with market + property_state so cross-market parcel joins stay
    scoped (ITEM-1). Required keyword arg: a parcel must never land market=NULL.

    Returns counts: {"inserted": N, "updated": N, "errors": N}
    """
    if dry_run:
        logger.info("[DRY RUN] Would upsert %d parcels to tranchi.parcels", len(hits))
        return {"inserted": 0, "updated": 0, "errors": 0}

    property_state = state_for_market(market)
    counts = {"inserted": 0, "updated": 0, "errors": 0}
    now = datetime.now(tz=timezone.utc)

    async with pool.acquire() as conn:
        for hit in hits:
            try:
                # Market-scoped probe: under the composite PK (parcel_number, market)
                # a Lucas PARID can equal a Summit parcel_number (8,529 collisions).
                # Without `AND market = $2` this would find the OTHER market's row and
                # route into the UPDATE branch, overwriting it with this market's data.
                existing = await conn.fetchval(
                    "SELECT parcel_number FROM tranchi.parcels WHERE parcel_number = $1 AND market = $2",
                    hit["parcel_number"],
                    market,
                )
                # native_parcel_id: Shelby County spaced PARCELID for Trustee URL.
                # Present only on shelby_parcels hits; Cuyahoga hits won't have it → NULL.
                native_parcel_id = hit.get("native_parcel_id") or None

                if existing:
                    await conn.execute(
                        """
                        UPDATE tranchi.parcels SET
                            owner_name           = COALESCE($2, owner_name),
                            situs_address        = COALESCE($3, situs_address),
                            property_class       = COALESCE($4, property_class),
                            land_use_code        = COALESCE($5, land_use_code),
                            neighborhood         = COALESCE($6, neighborhood),
                            school_district      = COALESCE($7, school_district),
                            current_market_value = COALESCE($8, current_market_value),
                            current_tax_balance  = COALESCE($9, current_tax_balance),
                            delinquent_flag      = COALESCE($10, delinquent_flag),
                            last_seen_at         = $11,
                            source_url           = $12,
                            native_parcel_id     = COALESCE($13, native_parcel_id),
                            market               = $14,
                            property_state       = $15,
                            -- Sale data persisted by registry sweeps that carry it (Wayne folds
                            -- Detroit Property Sales into the spine). COALESCE-guarded: markets
                            -- whose registry hit passes NULL keep the value enrich_sales*.py set.
                            last_sale_date       = COALESCE($16, last_sale_date),
                            last_sale_price      = COALESCE($17, last_sale_price)
                        WHERE parcel_number = $1 AND market = $14
                        """,
                        hit["parcel_number"],
                        hit.get("owner_name") or None,
                        hit.get("situs_address") or None,
                        hit.get("property_class") or None,
                        hit.get("land_use_code") or None,
                        hit.get("neighborhood_code") or None,
                        hit.get("school_district") or None,
                        hit.get("current_market_value"),
                        hit.get("tax_balance_due"),
                        _is_delinquent(hit),
                        now,
                        hit.get("source_url"),
                        native_parcel_id,
                        market,
                        property_state,
                        hit.get("last_sale_date"),
                        hit.get("last_sale_price"),
                    )
                    counts["updated"] += 1
                else:
                    await conn.execute(
                        """
                        INSERT INTO tranchi.parcels (
                            parcel_number, owner_name, situs_address,
                            property_class, land_use_code, neighborhood,
                            school_district, current_market_value,
                            current_tax_balance, delinquent_flag,
                            first_seen_at, last_seen_at, source_url,
                            native_parcel_id, market, property_state,
                            last_sale_date, last_sale_price
                        ) VALUES (
                            $1, $2, $3,
                            $4, $5, $6,
                            $7, $8,
                            $9, $10,
                            $11, $11, $12,
                            $13, $14, $15,
                            $16, $17
                        )
                        ON CONFLICT (parcel_number, market) DO NOTHING
                        """,
                        hit["parcel_number"],
                        hit.get("owner_name") or None,
                        hit.get("situs_address") or None,
                        hit.get("property_class") or None,
                        hit.get("land_use_code") or None,
                        hit.get("neighborhood_code") or None,
                        hit.get("school_district") or None,
                        hit.get("current_market_value"),
                        hit.get("tax_balance_due"),
                        _is_delinquent(hit),
                        now,
                        hit.get("source_url"),
                        native_parcel_id,
                        market,
                        property_state,
                        hit.get("last_sale_date"),
                        hit.get("last_sale_price"),
                    )
                    counts["inserted"] += 1
            except Exception as exc:
                logger.error("Parcel upsert error for %s: %s", hit.get("parcel_number"), exc)
                counts["errors"] += 1

    return counts


def _is_delinquent(hit: dict[str, Any]) -> bool | None:
    """
    Determine if a parcel is delinquent based on available fields.
    Returns None when not enough data to determine.
    """
    if hit.get("flag_foreclosure") or hit.get("flag_cert_pending") or hit.get("flag_cert_sold"):
        return True
    balance = hit.get("tax_balance_due")
    if balance is not None:
        return balance > 0
    return None


# ─────────────────────────────────────────────────────────────────────────────
# On-demand lookup — used by probate.py
# ─────────────────────────────────────────────────────────────────────────────

async def search_by_owner(
    name: str,
    *,
    fuzzy: bool = True,
    enrich_detail: bool = True,
    enrich_tax: bool = False,
    city_code: str = _ENTIRE_COUNTY_CODE,
    min_confidence: float = _FUZZY_THRESHOLD,
) -> list[ParcelMatch]:
    """
    On-demand owner-name search. Called by probate.py to resolve a decedent
    name to one or more parcels they may own.

    High-recall mode: returns ALL candidate parcels where the owner name
    matches the query at or above min_confidence. Multiple matches get
    ambiguous=True. Caller (probate.py) filters on confidence and ambiguous.

    Args:
        name: Owner name to search (e.g. "SMITH JOHN" or "ANNETTE SMITH").
        fuzzy: If True, compute Levenshtein confidence and include all hits
               above min_confidence. If False, exact-only (after normalization).
        enrich_detail: If True, POST to PropertyData for each candidate parcel
                       to get land use, school district, neighborhood, etc.
        enrich_tax: If True, use Playwright to get Tax By Year flags. Slow —
                    only enable when the caller needs distress signal data.
        city_code: Restrict to a specific municipality. Default: entire county.
        min_confidence: Minimum Levenshtein confidence to include a result.

    Returns:
        List of ParcelMatch objects sorted by confidence descending.
        Returns empty list when name is blank or no results above threshold.
    """
    name = name.strip()
    if not name:
        return []

    # Use the last name token as the search term (MyPlace searches by owner name substring)
    # For probate: decedent names often come as "FIRSTNAME LASTNAME" → search LASTNAME
    tokens = name.upper().split()
    # Heuristic: last token is most likely surname, which is what MyPlace indexes
    search_term = tokens[-1] if tokens else name.upper()

    logger.info("search_by_owner: name=%r, search_term=%r, city=%s", name, search_term, city_code)

    headers = _default_headers()
    all_hits: list[dict[str, Any]] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=_TIMEOUT) as client:
        # Seed the session with a GET to the search page
        html = await _fetch_search_page(client, search_term, city_code)
        if html is None:
            logger.error("search_by_owner: search page fetch failed for %r", search_term)
            return []

        raw_hits = _parse_hit_list(html)
        logger.info("search_by_owner: %d raw hits for term=%r", len(raw_hits), search_term)

        for hit in raw_hits:
            pn = hit["parcel_number"]
            if pn in seen:
                continue
            seen.add(pn)

            # Compute name confidence
            confidence = _name_confidence(name, hit["owner_name"])
            if confidence < min_confidence:
                continue

            if enrich_detail and len(all_hits) < _MAX_DETAIL_ENRICH:
                detail = await _fetch_property_data(client, hit, search_term, city_code)
                hit.update({k: v for k, v in detail.items() if not k.startswith("_")})
                await asyncio.sleep(_jitter(*_DETAIL_DELAY))

            hit["_confidence"] = confidence
            all_hits.append(hit)

    # Tax flag enrichment (Playwright, opt-in)
    if enrich_tax and all_hits:
        logger.info("search_by_owner: enriching %d parcels with tax flags (Playwright)", len(all_hits))
        for hit in all_hits[:_MAX_DETAIL_ENRICH]:
            tax_data = await _enrich_tax_flags_playwright(
                hit["parcel_pin_raw"],
                search_text=search_term,
                search_city=city_code,
            )
            hit.update(tax_data)
            await asyncio.sleep(_jitter(*_DETAIL_DELAY))

    if not all_hits:
        return []

    # Build ParcelMatch objects
    is_ambiguous = len(all_hits) > 1

    matches: list[ParcelMatch] = []
    for hit in all_hits:
        conf = hit.get("_confidence", 0.0)
        match = ParcelMatch(
            parcel_number=hit["parcel_number"],
            owner_name=hit["owner_name"],
            situs_address=hit.get("situs_address", ""),
            property_city=hit.get("property_city"),
            property_zip=hit.get("property_zip"),
            market_value=hit.get("current_market_value"),
            tax_balance=hit.get("tax_balance_due"),
            confidence=conf,
            ambiguous=is_ambiguous or conf < 1.0,
            full_record={k: v for k, v in hit.items() if not k.startswith("_")},
        )
        matches.append(match)

    matches.sort(key=lambda m: m.confidence, reverse=True)
    logger.info(
        "search_by_owner: returning %d matches (top confidence=%.2f, ambiguous=%s)",
        len(matches),
        matches[0].confidence if matches else 0.0,
        any(m.ambiguous for m in matches),
    )
    return matches


# ─────────────────────────────────────────────────────────────────────────────
# On-demand lookup by address — used by probate.py (dual-path join)
# ─────────────────────────────────────────────────────────────────────────────

async def search_by_address(
    address: str,
    *,
    enrich_detail: bool = True,
    city_code: str = _ENTIRE_COUNTY_CODE,
) -> list[ParcelMatch]:
    """
    On-demand address search on MyPlace. Called by probate.py as the
    high-confidence path when a decedent_address is known.

    Mirrors search_by_owner but uses Address mode instead of Owner mode.
    An exact address match gets confidence=0.95 and ambiguous=False.
    When multiple parcels are returned (e.g. a condo complex sharing an
    address root), all are returned with ambiguous=True and confidence=0.90.

    Args:
        address: Situs address to search (e.g. "6859 Hidden Lake Trail").
        enrich_detail: If True, POST to PropertyData for each hit to get
                       land use, school district, neighborhood, etc.
        city_code: Restrict to a specific municipality. Default: entire county.

    Returns:
        List of ParcelMatch objects sorted by confidence descending.
        Returns empty list when address is blank or no results found.
    """
    address = address.strip()
    if not address:
        return []

    # Use the street-and-number portion as the search term (strip city/state/zip
    # suffix if the caller passed a full address string from CaseParties).
    # MyPlace Address search matches on the street portion, not the full string.
    search_term = address.split(",")[0].strip()

    logger.info(
        "search_by_address: address=%r, search_term=%r, city=%s",
        address, search_term, city_code,
    )

    headers = _default_headers()
    all_hits: list[dict[str, Any]] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=_TIMEOUT) as client:
        html = await _fetch_search_page(client, search_term, city_code, mode=_MODE_ADDRESS)
        if html is None:
            logger.error("search_by_address: search page fetch failed for %r", search_term)
            return []

        raw_hits = _parse_hit_list(html)
        logger.info("search_by_address: %d raw hits for address=%r", len(raw_hits), search_term)

        for hit in raw_hits:
            pn = hit["parcel_number"]
            if pn in seen:
                continue
            seen.add(pn)

            if enrich_detail and len(all_hits) < _MAX_DETAIL_ENRICH:
                detail = await _fetch_property_data(client, hit, search_term, city_code)
                hit.update({k: v for k, v in detail.items() if not k.startswith("_")})
                await asyncio.sleep(_jitter(*_DETAIL_DELAY))

            all_hits.append(hit)

    if not all_hits:
        return []

    # Confidence assignment: single result = exact address match (0.95, not ambiguous).
    # Multiple results = address matched multiple parcels (0.90, ambiguous).
    is_ambiguous = len(all_hits) > 1
    confidence = 0.90 if is_ambiguous else 0.95

    matches: list[ParcelMatch] = []
    for hit in all_hits:
        match = ParcelMatch(
            parcel_number=hit["parcel_number"],
            owner_name=hit["owner_name"],
            situs_address=hit.get("situs_address", ""),
            property_city=hit.get("property_city"),
            property_zip=hit.get("property_zip"),
            market_value=hit.get("current_market_value"),
            tax_balance=hit.get("tax_balance_due"),
            confidence=confidence,
            ambiguous=is_ambiguous,
            full_record={k: v for k, v in hit.items() if not k.startswith("_")},
        )
        matches.append(match)

    matches.sort(key=lambda m: m.confidence, reverse=True)
    logger.info(
        "search_by_address: returning %d matches (confidence=%.2f, ambiguous=%s)",
        len(matches),
        matches[0].confidence if matches else 0.0,
        is_ambiguous,
    )
    return matches


# ─────────────────────────────────────────────────────────────────────────────
# Signal upsert helper (for tax flags → tranchi.signals)
# ─────────────────────────────────────────────────────────────────────────────

async def upsert_signals_from_parcels(
    pool: Any,  # asyncpg.Pool
    hits: list[dict[str, Any]],
    dry_run: bool = False,
) -> int:
    """
    Write distress signals to tranchi.signals for parcels that have tax flags set.

    Signal types written:
        'tax_foreclosure'  — Foreclosure flag = Y
        'cert_pending'     — Cert. Pending flag = Y
        'cert_sold'        — Cert. Sold flag = Y
        'tax_payment_plan' — Payment Plan flag = Y
        'tax_delinquent'   — Balance Due > 0 (and no more specific flag)

    INVARIANT: parcel must exist in tranchi.parcels before signal insert
               (FK enforced by schema). upsert_parcels() must run first.

    Returns count of signals written.
    """
    if dry_run:
        flagged = [h for h in hits if _is_delinquent(h)]
        logger.info("[DRY RUN] Would upsert signals for %d distressed parcels", len(flagged))
        return 0

    count = 0
    now = datetime.now(tz=timezone.utc)

    flag_map = {
        "flag_foreclosure": "tax_foreclosure",
        "flag_cert_pending": "cert_pending",
        "flag_cert_sold": "cert_sold",
        "flag_payment_plan": "tax_payment_plan",
    }

    async with pool.acquire() as conn:
        for hit in hits:
            pn = hit.get("parcel_number")
            if not pn:
                continue

            signals_to_write: list[tuple[str, float]] = []

            # Specific flag signals
            for flag_key, signal_type in flag_map.items():
                if hit.get(flag_key) is True:
                    signals_to_write.append((signal_type, 0.95))

            # Generic delinquent signal if no specific flags but balance > 0
            if not signals_to_write:
                balance = hit.get("tax_balance_due")
                if balance and balance > 0:
                    signals_to_write.append(("tax_delinquent", 0.8))

            for signal_type, confidence in signals_to_write:
                try:
                    await conn.execute(
                        """
                        INSERT INTO tranchi.signals
                            (parcel_number, signal_type, source, observed_at,
                             confidence, payload, first_seen_at, last_seen_at, market)
                        VALUES ($1, $2, 'fiscal_officer', $3, $4, $5::jsonb, $3, $3, 'cuyahoga')
                        ON CONFLICT DO NOTHING
                        """,
                        pn,
                        signal_type,
                        now,
                        confidence,
                        f'{{"tax_balance": {hit.get("tax_balance_due") or 0}}}',
                    )
                    count += 1
                except Exception as exc:
                    logger.warning("Signal upsert failed for %s / %s: %s", pn, signal_type, exc)

    return count


# ─────────────────────────────────────────────────────────────────────────────
# CLI dry-run entry (for `python -m app.scrapers.run --site fiscal_officer --dry-run`)
# ─────────────────────────────────────────────────────────────────────────────

async def _dry_run_demo() -> None:
    """
    Quick smoke test:
    1. Sweep letter 'S' for bulk mode, print first 5 parcels.
    2. Call search_by_owner("SMITH") and print first 3 results.
    """
    import json

    print("\n=== Fiscal Officer — DRY RUN DEMO ===\n")

    # Bulk mode: one letter
    scraper = FiscalOfficerScraper(sweep_letters=["S"], enrich_detail=False)
    listings = await scraper.fetch_and_parse()
    print(f"Bulk sweep 'S': {len(listings)} listings found")
    for l in listings[:5]:
        print(f"  {l.source_listing_id} | {l.property_address} | {l.property_city}")

    print()

    # On-demand search
    matches = await search_by_owner("SMITH", enrich_detail=False, enrich_tax=False)
    print(f"search_by_owner('SMITH'): {len(matches)} matches (confidence >= {_FUZZY_THRESHOLD})")
    for m in matches[:3]:
        print(
            f"  {m.parcel_number} | {m.owner_name!r} | {m.situs_address} | "
            f"conf={m.confidence:.2f} | ambiguous={m.ambiguous}"
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_dry_run_demo())
