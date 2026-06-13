"""
Akron Legal News (ALN) scraper — upcoming Summit County sheriff-sale abstracts.

Site: https://www.akronlegalnews.com (Official Law Journal of Summit County, OH)
Role: cross-check / enrichment source for the RealAuction foreclosure feed.
      ALN notices cover the same auctions; they add plaintiff, defendant/owner,
      attorney line, vacant-lot flag, and an independent record for validation.

INVARIANTS (read before editing):
  1. Case numbers on ALN are SPACE-FORMATTED (e.g. "CV2024 08 3496"). Spaces
     are STRIPPED before storing (→ "CV2024083496") so joins against RealAuction
     case numbers work. Never compare raw case text to RealAuction.
  2. Parcel ID is NOT in the abstract paragraph — it is ONLY on the detail page.
     Every case requires exactly 1 extra HTTP fetch. Throttle to ~1 req/sec.
  3. Detail page parcel is in DISPLAY FORM with a dash after digit 2
     ("70-00697"). normalize_parcel_number strips the dash → 7-digit
     zero-padded canonical ("7000697"). Always call normalize_parcel_number.
  4. "V/L" inside the street address means vacant lot. Strip it from the stored
     address; it does not represent a city.
  5. Cross-source dedup vs RealAuction happens at the engine dedup layer (keyed
     on parcel / case_number). This scraper just emits rows — no DB pool needed.

Cron: full-rescan (single page, ~80 rows) every 3 h alongside DLN.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime

import httpx
from bs4 import BeautifulSoup

from app.scrapers._time import today_et
from app.scrapers.base import ListingScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawListing
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

SITE_NAME = "Akron Legal News"

_BASE_URL = "https://www.akronlegalnews.com"
_ABSTRACTS_URL = f"{_BASE_URL}/notices/sheriff_sale_abstracts"
_DETAIL_URL = f"{_BASE_URL}/notices/detail/{{notice_id}}"

_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0
_INTER_DETAIL_DELAY = 1.0   # ~1 req/sec — polite; ~80 fetches/full-rescan

# "Properties for Sale on June 12, 2026"
_SALE_DATE_HEADER_RE = re.compile(
    r"Properties for Sale on ([A-Za-z]+ \d{1,2}, \d{4})"
)
# Abstract text format:
# "CV2024 08 3496-US Bank Trust NA v Jonathan J Widrig, et al
#  Property located at 976 Hampton Ridge Drive, Akron.
#  Appraised at $144,000. Term of sale, cash. R R HOOSE, Akron atty."
_ABSTRACT_CASE_RE = re.compile(r"^([A-Z]{2}\d{4} \d{2} \d{4})-")
_ABSTRACT_PLAINTIFF_RE = re.compile(r"^[A-Z]{2}\d{4} \d{2} \d{4}-(.+?) v ")
_ABSTRACT_DEFENDANT_RE = re.compile(r" v (.+?)(?:, et al)?(?= Property located at)")
_ABSTRACT_ADDR_RE = re.compile(
    r"Property located at ([^,]+),\s*([A-Za-z .'-]+?)\."
)
_ABSTRACT_APPRAISE_RE = re.compile(r"Appraised at \$([\d,]+)")

# Detail page re-offer date: "on the 26th day of June, 2026"
# or "on\nthe\n10th day of July, 2026" (split across lines in some variants)
_REOFFER_RE = re.compile(
    r"offered again.*?(?:on\s+the|on the)\s+(\d{1,2}(?:st|nd|rd|th) day of [A-Z][a-z]+ \d{4})",
    re.IGNORECASE | re.DOTALL,
)
# "26th day of June, 2026" → date
_DAY_MONTH_YEAR_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th) day of ([A-Za-z]+),? (\d{4})"
)


# ─────────────────────────────────────────────────────────────────────────────
# Parse helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_longdate(raw: str | None) -> date | None:
    """'June 12, 2026' → date."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_day_month_year(raw: str | None) -> date | None:
    """'26th day of June, 2026' → date."""
    if not raw:
        return None
    m = _DAY_MONTH_YEAR_RE.search(raw)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(2)} {m.group(1)}, {m.group(3)}", "%B %d, %Y").date()
    except ValueError:
        return None


def _parse_money(raw: str | None) -> float | None:
    """'5,000.00' / '$144,000' → float; None/'' → None."""
    if not raw:
        return None
    cleaned = re.sub(r"[^0-9.]", "", str(raw))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _strip_vl(address: str) -> tuple[str, bool]:
    """Strip 'V/L' or ', V/L' vacant-lot marker from address string.

    Returns (cleaned_address, is_vacant_lot).
    """
    vl = False
    # Match ", V/L" or " V/L" at end or in middle (before comma)
    cleaned = re.sub(r",?\s*V/L\s*", "", address, flags=re.IGNORECASE).strip().rstrip(",").strip()
    if cleaned != address.strip():
        vl = True
    return cleaned, vl


def _normalize_case(raw: str | None) -> str | None:
    """Strip spaces from ALN case number so it joins RealAuction.

    'CV2024 08 3496' → 'CV2024083496'
    """
    if not raw:
        return None
    return re.sub(r"\s+", "", raw.strip()) or None


async def _get_html(
    client: httpx.AsyncClient,
    url: str,
    label: str,
) -> str | None:
    """GET url with retry. Returns HTML text or None on failure."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error("ALN: GET %s (%s) failed after %d attempts: %s", url, label, attempt, exc)
                return None
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Abstract list page parsing
# ─────────────────────────────────────────────────────────────────────────────

def _parse_abstracts_page(html: str) -> list[dict]:
    """Parse the ALN sheriff-sale abstracts page.

    Returns a list of dicts, each with:
        notice_id     : str  (e.g. '16381')
        case_number   : str  (space-stripped, e.g. 'CV2024083496')
        case_raw      : str  (original spaced form, e.g. 'CV2024 08 3496')
        plaintiff     : str | None
        defendant     : str | None
        property_address : str | None  (street only, V/L stripped)
        property_city : str | None
        appraised_value_usd : float | None
        sale_date     : date | None
        is_vacant_lot : bool
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    current_sale_date: date | None = None

    # Walk all elements in order. Date-section headers are <strong> tags with
    # text "Properties for Sale on <date>". Each header is followed immediately
    # by one or more <table class="format-notice-table"> tags containing one row.
    # Strategy: iterate top-level children of the main page body element and
    # track date state using the <strong> header text.

    # Collect all nodes in document order that are either:
    #   a) <strong> with a sale-date header
    #   b) <table class="format-notice-table">
    all_nodes = soup.find_all(
        lambda tag: (
            (tag.name == "strong" and "Properties for Sale on" in (tag.get_text() or ""))
            or (tag.name == "table" and "format-notice-table" in (tag.get("class") or []))
        )
    )

    for node in all_nodes:
        if node.name == "strong":
            text = node.get_text().strip()
            m = _SALE_DATE_HEADER_RE.search(text)
            if m:
                current_sale_date = _parse_longdate(m.group(1))
        elif node.name == "table":
            row = _parse_abstract_table(node, current_sale_date)
            if row:
                rows.append(row)

    return rows


def _parse_abstract_table(table, sale_date: date | None) -> dict | None:
    """Parse one format-notice-table into a partial row dict."""
    # Grab the detail link and notice_id
    link = table.find("a", href=re.compile(r"/notices/detail/\d+"))
    if not link:
        return None
    href = link.get("href", "")
    notice_id_m = re.search(r"/notices/detail/(\d+)", href)
    if not notice_id_m:
        return None
    notice_id = notice_id_m.group(1)

    # The abstract text lives in <td class="format-notice">
    notice_td = table.find("td", class_="format-notice")
    if not notice_td:
        return None
    abstract_text = notice_td.get_text(" ", strip=True)

    return _parse_abstract_text(abstract_text, notice_id, sale_date)


def _parse_abstract_text(text: str, notice_id: str, sale_date: date | None) -> dict | None:
    """Extract fields from one abstract paragraph."""
    # Case number (raw spaced form at start before '-')
    case_m = _ABSTRACT_CASE_RE.match(text)
    if not case_m:
        return None
    case_raw = case_m.group(1)
    case_number = _normalize_case(case_raw)

    # Plaintiff (between end of case# and " v ")
    plaintiff: str | None = None
    pl_m = _ABSTRACT_PLAINTIFF_RE.match(text)
    if pl_m:
        plaintiff = pl_m.group(1).strip() or None

    # Defendant (between " v " and ", et al" or " Property located at")
    defendant: str | None = None
    def_m = _ABSTRACT_DEFENDANT_RE.search(text)
    if def_m:
        defendant = def_m.group(1).strip() or None

    # Address and city from "Property located at <addr>, <city>."
    property_address: str | None = None
    property_city: str | None = None
    is_vacant_lot = False
    addr_m = _ABSTRACT_ADDR_RE.search(text)
    if addr_m:
        raw_addr = addr_m.group(1).strip()
        raw_city = addr_m.group(2).strip()
        cleaned_addr, is_vacant_lot = _strip_vl(raw_addr)
        property_address = cleaned_addr or None
        property_city = raw_city or None

    # Appraised value
    appraise_m = _ABSTRACT_APPRAISE_RE.search(text)
    appraised_value_usd = _parse_money(appraise_m.group(1)) if appraise_m else None

    return {
        "notice_id": notice_id,
        "case_raw": case_raw,
        "case_number": case_number,
        "plaintiff": plaintiff,
        "defendant": defendant,
        "property_address": property_address,
        "property_city": property_city,
        "appraised_value_usd": appraised_value_usd,
        "sale_date": sale_date,
        "is_vacant_lot": is_vacant_lot,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Detail page parsing
# ─────────────────────────────────────────────────────────────────────────────

def _parse_detail_page(html: str) -> dict:
    """Parse a /notices/detail/<id> page.

    Returns a dict with:
        parcel_raw        : str | None  (display form e.g. '70-00697')
        parcel_normalized : str | None  (7-digit e.g. '7000697')
        property_address  : str | None  (V/L stripped)
        property_city     : str | None
        property_state    : str         ('OH')
        property_zip      : str | None
        deposit_usd       : float | None
        appraised_value_usd : float | None
        opening_bid_usd   : float | None  (2/3 of appraised if stated on page)
        sec_sale_date     : date | None
        is_vacant_lot     : bool
    """
    soup = BeautifulSoup(html, "html.parser")

    # Parcel — semantic ID
    parcel_tag = soup.find(id="notice_parcel_number")
    parcel_raw = parcel_tag.get_text(strip=True) if parcel_tag else None
    parcel_normalized = normalize_parcel_number(parcel_raw)

    # Full address components — each has its own ID
    addr_tag = soup.find(id="notice_address")
    city_tag = soup.find(id="notice_city")
    state_tag = soup.find(id="notice_state")
    zip_tag = soup.find(id="notice_zip")

    raw_addr = addr_tag.get_text(strip=True) if addr_tag else None
    raw_city = city_tag.get_text(strip=True) if city_tag else None
    property_state = (state_tag.get_text(strip=True) if state_tag else None) or "OH"
    property_zip = zip_tag.get_text(strip=True) if zip_tag else None

    is_vacant_lot = False
    if raw_addr:
        raw_addr, is_vacant_lot = _strip_vl(raw_addr)

    property_address = raw_addr or None
    property_city = raw_city or None

    # Deposit amount
    deposit_tag = soup.find(id="notice_deposit_amount")
    deposit_usd = _parse_money(deposit_tag.get_text(strip=True) if deposit_tag else None)

    # Appraised value (prefer detail page — same number but more precise decimal)
    appraise_tag = soup.find(class_="notice_appraised_amount")
    appraised_value_usd = _parse_money(appraise_tag.get_text(strip=True) if appraise_tag else None)

    # Opening bid: only set when page explicitly states "cannot be sold for less than 2/3"
    opening_bid_usd: float | None = None
    page_text = soup.get_text(" ", strip=True)
    if "cannot be sold for less than 2/3" in page_text and appraised_value_usd:
        opening_bid_usd = round(appraised_value_usd * (2 / 3), 2)

    # Re-offer (second sale) date — lives in <span class="notice_date2">
    sec_sale_date: date | None = None
    date2_tag = soup.find(class_="notice_date2")
    if date2_tag:
        sec_sale_date = _parse_day_month_year(date2_tag.get_text(strip=True))
    if sec_sale_date is None:
        # Fallback: regex over full page text (handles "on\nthe" line-split variants)
        reoffer_m = _REOFFER_RE.search(page_text)
        if reoffer_m:
            sec_sale_date = _parse_day_month_year(reoffer_m.group(1))

    return {
        "parcel_raw": parcel_raw,
        "parcel_normalized": parcel_normalized,
        "property_address": property_address,
        "property_city": property_city,
        "property_state": property_state,
        "property_zip": property_zip,
        "deposit_usd": deposit_usd,
        "appraised_value_usd": appraised_value_usd,
        "opening_bid_usd": opening_bid_usd,
        "sec_sale_date": sec_sale_date,
        "is_vacant_lot": is_vacant_lot,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Scraper
# ─────────────────────────────────────────────────────────────────────────────

class SummitLegalNewsScraper(ListingScraper):
    """Summit County sheriff-sale abstracts from Akron Legal News.

    No DB pool required — cross-source dedup vs RealAuction is handled at the
    engine dedup layer (parcel/case keyed). This scraper emits RawListing rows
    with normalized parcels in source_listing_id and space-stripped case numbers.

    Constructor:
        dry_run (bool): If True, skip no-op path (still fetches + parses live).
    """

    site_name = SITE_NAME

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    async def fetch_and_parse(self) -> list[RawListing]:
        today = today_et()
        headers = default_headers()
        # Referer header helps with the legacy MooTools site's access patterns
        headers["Referer"] = _BASE_URL + "/"

        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            # ── Step 1: fetch and parse the abstracts list page ───────────────
            abstracts_html = await _get_html(client, _ABSTRACTS_URL, "abstracts")
            if not abstracts_html:
                logger.error("ALN: failed to fetch abstracts page — returning empty")
                return []

            abstracts = _parse_abstracts_page(abstracts_html)
            logger.info("ALN: parsed %d abstract rows from list page", len(abstracts))

            # Filter to sale_date >= today before fetching detail pages
            upcoming = [a for a in abstracts if a.get("sale_date") and a["sale_date"] >= today]
            logger.info(
                "ALN: %d rows with sale_date >= %s (of %d total)", len(upcoming), today, len(abstracts)
            )

            # ── Step 2: fetch detail page per case for parcel + enrichment ────
            listings: list[RawListing] = []
            for i, abstract in enumerate(upcoming):
                notice_id = abstract["notice_id"]
                detail_url = _DETAIL_URL.format(notice_id=notice_id)
                detail_html = await _get_html(client, detail_url, f"detail/{notice_id}")

                if detail_html:
                    detail = _parse_detail_page(detail_html)
                else:
                    logger.warning(
                        "ALN: detail fetch failed for notice %s (case %s) — emitting without parcel",
                        notice_id, abstract.get("case_number"),
                    )
                    detail = {
                        "parcel_raw": None,
                        "parcel_normalized": None,
                        "property_address": None,
                        "property_city": None,
                        "property_state": "OH",
                        "property_zip": None,
                        "deposit_usd": None,
                        "appraised_value_usd": None,
                        "opening_bid_usd": None,
                        "sec_sale_date": None,
                        "is_vacant_lot": False,
                    }

                listing = _merge_to_listing(abstract, detail)
                if listing:
                    listings.append(listing)

                # Throttle: ~1 req/sec. Skip sleep after the last item.
                if i < len(upcoming) - 1:
                    await asyncio.sleep(_INTER_DETAIL_DELAY)

        logger.info("ALN: returning %d listings (sale_date >= %s)", len(listings), today)
        return listings


def _merge_to_listing(abstract: dict, detail: dict) -> RawListing | None:
    """Merge abstract + detail dicts into a RawListing.

    Detail page fields win over abstract fields (more precise / more complete).
    property_address is required; rows without one are dropped.
    """
    # Address: detail wins (has zip, authoritative V/L-stripped form + city from ID tag)
    property_address = detail.get("property_address") or abstract.get("property_address")
    if not property_address:
        logger.debug(
            "ALN: dropping row — no property_address (case=%s, notice=%s)",
            abstract.get("case_number"), abstract.get("notice_id"),
        )
        return None

    property_city = detail.get("property_city") or abstract.get("property_city")

    # Parcel from detail only (not in abstract)
    parcel = detail.get("parcel_normalized")

    # Appraised: detail has the decimal form; abstract has the rounded integer
    appraised = detail.get("appraised_value_usd") or abstract.get("appraised_value_usd")

    # Determine signal_type by SALE WEEKDAY — the same ground-truth discriminator
    # RealAuction uses (Summit runs tax-foreclosure sheriff sales on Tuesdays, mortgage
    # on Fridays). ALN abstracts cover BOTH, so hardcoding mortgage_foreclosure would
    # misclassify Tuesday tax sales and let cross-source dedup promote a wrong-typed
    # canonical row (the Memphis-class silent-misclassification risk). Tuesday = tax.
    _sale_date = abstract.get("sale_date")
    signal_type = (
        "tax_delinquent_foreclosure"
        if (_sale_date is not None and _sale_date.weekday() == 1)
        else "mortgage_foreclosure"
    )

    return RawListing(
        source_site=SITE_NAME,
        source_listing_id=parcel,
        case_number=abstract.get("case_number"),
        signal_type=signal_type,
        property_address=property_address,
        property_city=property_city,
        property_county="Summit",
        property_state=detail.get("property_state") or "OH",
        property_zip=detail.get("property_zip"),
        sale_date=abstract.get("sale_date"),
        sec_sale_date=detail.get("sec_sale_date"),
        deposit_usd=detail.get("deposit_usd"),
        opening_bid_usd=detail.get("opening_bid_usd"),
        appraised_value_usd=appraised,
        trustee_name=abstract.get("defendant"),
        status="active",
        auction_status="scheduled",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    async def _dry_run() -> None:
        print(f"\n=== ALN Dry Run — {SITE_NAME} ===\n")

        scraper = SummitLegalNewsScraper(dry_run=True)

        # ── Phase 1: fetch just the abstracts page to get totals + sample ────
        headers = default_headers()
        headers["Referer"] = _BASE_URL + "/"
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            abstracts_html = await _get_html(client, _ABSTRACTS_URL, "abstracts")

        if not abstracts_html:
            print("ERROR: could not fetch abstracts page")
            return

        abstracts = _parse_abstracts_page(abstracts_html)
        today = today_et()
        upcoming = [a for a in abstracts if a.get("sale_date") and a["sale_date"] >= today]
        past = [a for a in abstracts if not a.get("sale_date") or a["sale_date"] < today]

        print(f"Abstract rows total : {len(abstracts)}")
        print(f"Upcoming (>= today) : {len(upcoming)}")
        print(f"Past / no date      : {len(past)}")
        print()

        # Print first 3 abstracts (pre-detail)
        print("--- Sample abstracts (pre-detail) ---")
        for a in abstracts[:3]:
            print(a)
        print()

        # ── Phase 2: fetch a handful of detail pages ─────────────────────────
        # Always include the anchor case (notice 16381, CV2024 08 3496 → parcel 70-00697)
        ANCHOR_NOTICE_ID = "16381"
        ANCHOR_CASE = "CV2024083496"
        ANCHOR_PARCEL_DISPLAY = "70-00697"
        ANCHOR_PARCEL_NORM = "7000697"

        # Build a small set: anchor + first 3 upcoming
        sample_ids = [ANCHOR_NOTICE_ID]
        for a in upcoming[:3]:
            if a["notice_id"] != ANCHOR_NOTICE_ID:
                sample_ids.append(a["notice_id"])
        sample_ids = list(dict.fromkeys(sample_ids))[:4]  # dedupe, cap at 4

        print(f"--- Fetching detail pages for notice IDs: {sample_ids} ---")
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            for notice_id in sample_ids:
                detail_url = _DETAIL_URL.format(notice_id=notice_id)
                detail_html = await _get_html(client, detail_url, f"detail/{notice_id}")
                if detail_html:
                    d = _parse_detail_page(detail_html)
                    print(f"\nNotice {notice_id}:")
                    print(f"  parcel_raw        = {d['parcel_raw']!r}")
                    print(f"  parcel_normalized = {d['parcel_normalized']!r}")
                    print(f"  property_address  = {d['property_address']!r}")
                    print(f"  property_city     = {d['property_city']!r}")
                    print(f"  property_zip      = {d['property_zip']!r}")
                    print(f"  deposit_usd       = {d['deposit_usd']!r}")
                    print(f"  appraised_usd     = {d['appraised_value_usd']!r}")
                    print(f"  opening_bid_usd   = {d['opening_bid_usd']!r}")
                    print(f"  sec_sale_date     = {d['sec_sale_date']!r}")
                    print(f"  is_vacant_lot     = {d['is_vacant_lot']!r}")
                else:
                    print(f"\nNotice {notice_id}: FETCH FAILED")
                if notice_id != sample_ids[-1]:
                    await asyncio.sleep(_INTER_DETAIL_DELAY)

        # ── Phase 3: verify anchor ────────────────────────────────────────────
        print()
        print("--- Anchor case verification ---")
        anchor_abstract = next(
            (a for a in abstracts if a.get("notice_id") == ANCHOR_NOTICE_ID), None
        )
        if anchor_abstract:
            print(f"  case_raw      = {anchor_abstract.get('case_raw')!r}")
            print(f"  case_number   = {anchor_abstract.get('case_number')!r} (expected '{ANCHOR_CASE}')")
            print(f"  case match    : {'PASS' if anchor_abstract.get('case_number') == ANCHOR_CASE else 'FAIL'}")
        else:
            print(f"  WARNING: anchor notice {ANCHOR_NOTICE_ID} not found in abstracts (may be past sale_date)")

        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            anchor_html = await _get_html(client, _DETAIL_URL.format(notice_id=ANCHOR_NOTICE_ID), "anchor")
        if anchor_html:
            ad = _parse_detail_page(anchor_html)
            print(f"  parcel_raw    = {ad['parcel_raw']!r} (expected '{ANCHOR_PARCEL_DISPLAY}')")
            print(f"  parcel_norm   = {ad['parcel_normalized']!r} (expected '{ANCHOR_PARCEL_NORM}')")
            print(f"  parcel match  : {'PASS' if ad['parcel_normalized'] == ANCHOR_PARCEL_NORM else 'FAIL'}")
        else:
            print("  ERROR: could not fetch anchor detail page")

        # ── Phase 4: full fetch (all upcoming) ────────────────────────────────
        print()
        print("--- Full fetch (all upcoming rows) ---")
        listings = await scraper.fetch_and_parse()
        print(f"Total RawListings returned : {len(listings)}")
        if listings:
            sample = listings[0]
            print(f"Sample listing:")
            print(f"  source_site       = {sample.source_site!r}")
            print(f"  case_number       = {sample.case_number!r}")
            print(f"  source_listing_id = {sample.source_listing_id!r}")
            print(f"  signal_type       = {sample.signal_type!r}")
            print(f"  property_address  = {sample.property_address!r}")
            print(f"  property_city     = {sample.property_city!r}")
            print(f"  property_county   = {sample.property_county!r}")
            print(f"  property_state    = {sample.property_state!r}")
            print(f"  property_zip      = {sample.property_zip!r}")
            print(f"  sale_date         = {sample.sale_date!r}")
            print(f"  sec_sale_date     = {sample.sec_sale_date!r}")
            print(f"  appraised_value   = {sample.appraised_value_usd!r}")
            print(f"  opening_bid_usd   = {sample.opening_bid_usd!r}")
            print(f"  deposit_usd       = {sample.deposit_usd!r}")
            print(f"  trustee_name      = {sample.trustee_name!r}")
            print(f"  status            = {sample.status!r}")
            print(f"  auction_status    = {sample.auction_status!r}")

    asyncio.run(_dry_run())
