"""
Summit County (Akron, OH) sheriff/tax foreclosure auction scraper.

Source: https://summit.sheriffsaleauction.ohio.gov  (RealAuction / RealForeclose)
Both mortgage foreclosures (Friday sales) and tax foreclosures (Tuesday sales) live
on the same platform and are fetched here in one scraper.

INVARIANTS — read before editing:

  1. BROWSER UA OR 403: RealAuction returns HTTP 403 on any non-browser UA string,
     including a plain "Mozilla/5.0". Always use a full Chrome UA (random_ua()).

  2. PREVIEW-BEFORE-LOAD (cookie state): The PREVIEW call sets a session-cookie that
     scopes the LOAD calls to the correct auction date. One httpx.AsyncClient (shared
     cookie jar) MUST make the PREVIEW GET *before* any LOAD GETs for the same date.
     Interleaving dates without a fresh PREVIEW will read the wrong roster silently.

  3. WEEKDAY + SIGNATURE = CHANNEL SPLIT: Friday sale dates → mortgage_foreclosure;
     Tuesday sale dates → tax_delinquent_foreclosure. Do NOT use the plaintiff name
     to discriminate — use the weekday of the AuctionDate. Confirm with the tax
     signature (Appraised $0.00 + flat $1,000 deposit) but the weekday is ground truth.

  4. MULTIPLE PARCEL/ADDRESS: Both `Parcel ID` and `Property Address` can literally be
     the string "MULTIPLE" (multi-parcel case). Store `source_listing_id=None` and
     `property_address="MULTIPLE"` — do NOT skip or error on these rows.

  5. 9-DIGIT ZIP: The city row sometimes carries a 9-digit unhyphenated ZIP
     (e.g. "LAKEMORE , 442500000"). Truncate to the first 5 digits before storing.

  6. W-AREA LAST-PAGE REPEAT STOP: Area W paginates at 10 items/page. PageDir=1
     advances forward. The last page re-returns the SAME `rlist` value — detect this
     and stop. Do NOT use an item-count heuristic; use rlist equality.

  7. LETTER-SUFFIX CASES STAY SEPARATE: Case numbers like CV2025126090A/B/C are one
     Common Pleas case split into per-parcel sale items. Strip only the trailing (seq)
     number but preserve the letter suffix (A/B/C). Do NOT dedupe letter-suffix rows
     together — each is a distinct sale with its own parcel and bid.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from typing import Any

import httpx

from app.scrapers._time import today_et
from app.scrapers.base import ListingScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawListing
from app.scrapers.user_agents import random_ua

logger = logging.getLogger(__name__)

SITE_NAME = "Summit Sheriff Sale (RealAuction)"

_BASE_URL = "https://summit.sheriffsaleauction.ohio.gov/index.cfm"
_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0
_INTER_REQ_DELAY = 1.0          # ≤1 req/sec (field-map robots posture)
_CALENDAR_LOOKAHEAD_MONTHS = 3  # scan this many months of calendar pages

# The two auction channels and their weekday indices (Monday=0 … Sunday=6)
_MORTGAGE_WEEKDAY = 4   # Friday
_TAX_WEEKDAY = 1        # Tuesday

# retHTML rows are delimited by @G tokens
_ROW_SEP = "@G"


# ─────────────────────────────────────────────────────────────────────────────
# Parse helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_money(raw: str | None) -> float | None:
    """'$80,000.00' / '15926.39' → float; None/'' → None."""
    if not raw:
        return None
    cleaned = re.sub(r"[^0-9.]", "", str(raw).strip())
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_date(raw: str | None) -> date | None:
    """'06/12/2026' → date(2026, 6, 12); None/'' → None."""
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ("%m/%d/%Y", "%-m/%-d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _truncate_zip(raw: str | None) -> str | None:
    """'442500000' → '44250'; '44250-1234' → '44250-1234'; None → None.

    RealAuction sometimes serves a 9-digit unhyphenated ZIP (INVARIANT #5).
    We only truncate 9+ digit strings that lack a hyphen.
    """
    if not raw:
        return None
    z = str(raw).strip()
    if re.match(r"^\d{9,}$", z):
        return z[:5]
    return z


def _strip_seq(case_raw: str) -> str:
    """'CV2024125266 (10616)' → 'CV2024125266'; preserves letter suffix."""
    return re.sub(r"\s*\(\d+\)\s*$", "", case_raw.strip())


def _build_headers() -> dict[str, str]:
    return {
        "User-Agent": random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }


def _build_json_headers() -> dict[str, str]:
    hdrs = _build_headers()
    hdrs["Accept"] = "application/json, text/javascript, */*; q=0.01"
    hdrs["X-Requested-With"] = "XMLHttpRequest"
    hdrs["Referer"] = "https://summit.sheriffsaleauction.ohio.gov/index.cfm"
    return hdrs


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    as_json: bool = False,
) -> Any | None:
    """GET url with retry. Returns parsed JSON dict or raw text (or None on failure)."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url, params=params, headers=headers, timeout=_TIMEOUT)
            resp.raise_for_status()
            if as_json:
                return resp.json()
            return resp.text
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error("RealAuction GET %s params=%s failed after %d attempts: %s",
                             url, params, attempt, exc)
                return None
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Calendar parsing
# ─────────────────────────────────────────────────────────────────────────────

# Matches calendar day cells that carry a dayid AND have auction content.
# Summit markup: <div ... dayid='MM/DD/YYYY' ...>...<span class='CALTEXT'>...
# Single-quotes on dayid confirmed live 2026-06-11; the look-ahead for CALTEXT
# is a ~500-char lookahead (largest observed day-cell) to filter empty days.
_CALENDAR_DAY_RE = re.compile(
    r"""dayid=['"]([\d/]+)['"][^>]*>(?=(?:(?!dayid=).){0,800}CALTEXT)""",
    re.DOTALL,
)


def _parse_calendar(html_text: str, today: date) -> list[tuple[str, str]]:
    """Extract sale dates from the calendar HTML.

    Returns list of (dayid_str, signal_type) where dayid_str is 'MM/DD/YYYY'.
    Only returns dates >= today. Channel assigned by weekday (INVARIANT #3).

    Summit calendar: dayid attributes live on <div class='CALBOX ...'> elements.
    We use a regex lookahead to filter to cells that actually contain auction
    entries (CALTEXT class present within ~800 chars of the dayid) — empty
    calendar days have no CALTEXT and are skipped. No BeautifulSoup needed.
    """
    results: list[tuple[str, str]] = []

    for m in _CALENDAR_DAY_RE.finditer(html_text):
        raw_dayid = m.group(1).strip()
        if not raw_dayid:
            continue
        sale_date = _parse_date(raw_dayid)
        if sale_date is None or sale_date < today:
            continue
        wd = sale_date.weekday()
        if wd == _MORTGAGE_WEEKDAY:
            signal_type = "mortgage_foreclosure"
        elif wd == _TAX_WEEKDAY:
            signal_type = "tax_delinquent_foreclosure"
        else:
            # Not a known auction day — skip (could be a special date)
            logger.debug("RealAuction: skipping %s (weekday=%d, not Fri/Tue)", raw_dayid, wd)
            continue
        results.append((raw_dayid, signal_type))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# retHTML token-template parser
# ─────────────────────────────────────────────────────────────────────────────
#
# The RealAuction retHTML token template grammar (confirmed from live probing):
#
#   Items are separated by @A tokens. Each item is a flat string containing:
#     @E_DETAILS">...     (start of the data table section)
#     Fields are separated by @G within an item.
#     Each field segment has the form:
#       <tr><th @CAD_LBL"...>LABEL:</th><td @CAD_DTA">VALUE
#     Where @C = ' class="' and @E = ' id="' (abbreviation tokens).
#     An empty LABEL (the unlabeled city/zip row after Property Address) has:
#       <tr><th @CAD_LBL" scope="row"></th><td @CAD_DTA">CITY , ZIP
#
# We parse using regex on the raw token string — BeautifulSoup cannot handle
# the malformed HTML with shorthand tokens (@C, @E, @I, @B).

# Extracts: label (group 1, may be empty) and value (group 2) from one @G segment
_FIELD_RE = re.compile(
    r"<th\s[^>]*>([^<]*)</th>\s*<td\s[^>]*>(.*?)(?=<tr|</tbody|$)",
    re.DOTALL | re.IGNORECASE,
)

# City + ZIP in the unlabeled address continuation row: "AKRON , 44314"
_CITY_ZIP_RE = re.compile(
    r"^([A-Z][A-Z\s\-]+?)\s*,\s*(\d{5,9})\s*$",
    re.IGNORECASE,
)
# City only (no zip)
_CITY_ONLY_RE = re.compile(r"^([A-Z][A-Z\s\-]+?)\s*,\s*(?:OH)?\s*$", re.IGNORECASE)


def _parse_ret_html(ret_html: str) -> list[dict[str, str]]:
    """Parse the RealAuction retHTML token-template into a list of field dicts.

    Items are separated by @A tokens. Within each item, field rows are separated
    by @G tokens. Field structure is:
        <th @CAD_LBL"...>Label:</th><td @CAD_DTA">Value

    Returns a list of dicts with string keys matching the label text (e.g.
    "Case Status", "Case #", "Parcel ID", "Property Address") plus a special
    "__city_zip__" key for the unlabeled city/zip continuation row.
    """
    if not ret_html or not ret_html.strip():
        return []

    items: list[dict[str, str]] = []

    # Split on @A — each segment is one auction item block
    for item_block in ret_html.split("@A"):
        if "Case Status" not in item_block:
            # Skip non-data segments (wrapper divs, spacers, stats blocks)
            continue

        fields: dict[str, str] = {}
        # Split item block on @G to get individual field rows
        for seg in item_block.split("@G"):
            m = _FIELD_RE.search(seg)
            if not m:
                continue
            label_raw = m.group(1).strip().rstrip(":").strip()
            value_raw = m.group(2).strip()
            # Clean out any residual HTML tags from the value
            value_clean = re.sub(r"<[^>]+>", "", value_raw).strip()

            if label_raw:
                fields[label_raw] = value_clean
            else:
                # Unlabeled row = city/zip continuation after Property Address
                fields["__city_zip__"] = value_clean

        if fields:
            items.append(fields)

    return items


def _extract_city_zip(fields: dict[str, str]) -> tuple[str | None, str | None]:
    """Extract city and ZIP from the unlabeled city/zip continuation row.

    The raw text looks like: 'CUYAHOGA FALLS , 44221' or 'LAKEMORE , 442500000'
    """
    raw = fields.get("__city_zip__", "").strip()
    if not raw:
        return None, None

    m = _CITY_ZIP_RE.match(raw)
    if m:
        city = m.group(1).strip().title()
        zip_raw = m.group(2).strip()
        return city, _truncate_zip(zip_raw)

    m2 = _CITY_ONLY_RE.match(raw)
    if m2:
        return m2.group(1).strip().title(), None

    return None, None


def _parse_item(fields: dict[str, str], signal_type: str, sale_date: date) -> RawListing | None:
    """Map a single retHTML field dict to a RawListing. Returns None to skip."""
    # ── Validity filter (INVARIANT: ACTIVE + sale_date >= today handled by caller) ──
    case_status_raw = fields.get("Case Status", "").strip().upper()
    if case_status_raw != "ACTIVE":
        logger.debug("RealAuction: skipping non-ACTIVE row (status=%r)", case_status_raw)
        return None

    # ── Case number ──────────────────────────────────────────────────────────
    case_raw = fields.get("Case #", fields.get("Case#", "")).strip()
    case_number = _strip_seq(case_raw) if case_raw else None

    # ── Parcel / source_listing_id ───────────────────────────────────────────
    parcel_raw = fields.get("Parcel ID", fields.get("Parcel", "")).strip()
    if parcel_raw.upper() == "MULTIPLE":
        source_listing_id = None          # INVARIANT #4
    elif parcel_raw:
        source_listing_id = normalize_parcel_number(parcel_raw)
        # Non-real-property cases (manufactured-home lots, e.g. parcel 'MHLOT12')
        # can't join the 7-digit Summit spine. Treat like MULTIPLE: keep the listing
        # but null the parcel key so it never carries a bogus FK / dedup key.
        if source_listing_id and not re.match(r"^\d{7}$", source_listing_id):
            source_listing_id = None
    else:
        source_listing_id = None

    # ── Address ──────────────────────────────────────────────────────────────
    addr_raw = fields.get("Property Address", "").strip()
    property_address = addr_raw if addr_raw else "MULTIPLE"

    city, zip_code = _extract_city_zip(fields)

    # ── Money fields ─────────────────────────────────────────────────────────
    appr_raw = fields.get("Appraised Value", "")
    appraised_value_usd: float | None
    if signal_type == "tax_delinquent_foreclosure":
        # INVARIANT #3 + field-map: Appraised is always $0.00 for tax rows — do NOT map it
        appraised_value_usd = None
    else:
        appraised_value_usd = _parse_money(appr_raw)

    opening_bid_usd = _parse_money(fields.get("Opening Bid", ""))
    deposit_usd = _parse_money(fields.get("Deposit Requirement", ""))

    # ── Emit ─────────────────────────────────────────────────────────────────
    return RawListing(
        source_site=SITE_NAME,
        case_number=case_number,
        source_listing_id=source_listing_id,
        signal_type=signal_type,
        property_address=property_address,
        property_city=city,
        property_county="Summit",
        property_state="OH",
        property_zip=zip_code,
        sale_date=sale_date,
        appraised_value_usd=appraised_value_usd,
        opening_bid_usd=opening_bid_usd,
        deposit_usd=deposit_usd,
        auction_status=case_status_raw,  # verbatim from source ("ACTIVE")
        status="active",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Roster fetcher (steps 2 + 3 for one auction date)
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_roster_for_date(
    client: httpx.AsyncClient,
    auction_date: str,
    signal_type: str,
    today: date,
) -> list[RawListing]:
    """Fetch all listings for one auction date (mortgage or tax).

    Step 2: PREVIEW (sets session cookie).
    Step 3: LOAD union of AREA C and AREA W with pagination.
    """
    sale_date = _parse_date(auction_date)
    if sale_date is None or sale_date < today:
        return []

    # ── Step 2: PREVIEW — sets the session-scoped auction date cookie ─────────
    preview_params = {
        "zaction": "AUCTION",
        "zmethod": "PREVIEW",
        "AuctionDate": auction_date,
    }
    preview_html = await _get_with_retry(
        client, _BASE_URL, params=preview_params, headers=_build_headers()
    )
    if preview_html is None:
        logger.error("RealAuction: PREVIEW failed for %s — skipping date", auction_date)
        return []
    await asyncio.sleep(_INTER_REQ_DELAY)

    listings: list[RawListing] = []
    ts = str(int(datetime.now().timestamp() * 1000))

    # ── Step 3: LOAD AREA C ──────────────────────────────────────────────────
    c_listings = await _load_area(client, area="C", auction_date=auction_date,
                                   signal_type=signal_type, sale_date=sale_date,
                                   today=today, ts=ts)
    listings.extend(c_listings)

    # ── Step 3: LOAD AREA W (paginated) ──────────────────────────────────────
    w_listings = await _load_area(client, area="W", auction_date=auction_date,
                                   signal_type=signal_type, sale_date=sale_date,
                                   today=today, ts=ts)
    listings.extend(w_listings)

    logger.info(
        "RealAuction: %s (%s) → %d listings (C=%d W=%d)",
        auction_date, signal_type, len(listings), len(c_listings), len(w_listings),
    )
    return listings


async def _load_area(
    client: httpx.AsyncClient,
    *,
    area: str,
    auction_date: str,
    signal_type: str,
    sale_date: date,
    today: date,
    ts: str,
) -> list[RawListing]:
    """Paginate one AREA (C or W) for the current cookie-scoped date.

    Returns the union of all pages. Stops when rlist repeats (INVARIANT #6).
    """
    listings: list[RawListing] = []
    seen_rlist: set[str] = set()
    page_dir = 0   # 0 = first page; 1 = advance

    while True:
        params: dict[str, Any] = {
            "zaction": "AUCTION",
            "Zmethod": "UPDATE",
            "FNC": "LOAD",
            "AREA": area,
            "PageDir": page_dir,
            "doR": "1",
            "tx": ts,
            "bypassPage": "0",
            "test": "1",
            "_": ts,
        }
        data = await _get_with_retry(
            client, _BASE_URL, params=params,
            headers=_build_json_headers(), as_json=True,
        )
        await asyncio.sleep(_INTER_REQ_DELAY)

        if not data or not isinstance(data, dict):
            logger.debug("RealAuction: AREA %s page_dir=%d returned empty/invalid JSON", area, page_dir)
            break

        rlist: str = (data.get("rlist") or "").strip()
        ret_html: str = data.get("retHTML") or ""

        # Empty rlist / empty retHTML → no items in this area
        if not rlist and not ret_html.strip():
            break

        # INVARIANT #6: last page re-returns the same rlist — stop
        if rlist in seen_rlist:
            logger.debug("RealAuction: AREA %s rlist repeated — last page reached", area)
            break
        seen_rlist.add(rlist)

        # Parse the retHTML token template
        item_fields_list = _parse_ret_html(ret_html)
        for item_fields in item_fields_list:
            listing = _parse_item(item_fields, signal_type, sale_date)
            if listing is not None:
                listings.append(listing)

        # Advance to next page
        page_dir = 1

    return listings


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class SummitRealAuctionScraper(ListingScraper):
    """Summit County (OH) sheriff + tax foreclosure listings from RealAuction.

    One scraper, two signal_types:
      - mortgage_foreclosure  → Friday sale dates
      - tax_delinquent_foreclosure → Tuesday sale dates

    No DB pool required — the orchestrator handles catalog cross-check and
    expiry passes via full_rescan + post-passes.
    """

    site_name = SITE_NAME

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    async def fetch_and_parse(self) -> list[RawListing]:
        today = today_et()
        all_listings: list[RawListing] = []

        # One httpx client with a cookie jar shared across all requests per run.
        # This is REQUIRED — the PREVIEW call sets a cookie that scopes subsequent
        # LOAD calls to the correct auction date (INVARIANT #2).
        async with httpx.AsyncClient(follow_redirects=True) as client:
            # ── Step 1: collect all upcoming auction dates from calendar ───────
            sale_dates = await self._collect_sale_dates(client, today)
            logger.info(
                "RealAuction: found %d upcoming sale dates across %d month(s)",
                len(sale_dates), _CALENDAR_LOOKAHEAD_MONTHS,
            )

            # ── Steps 2+3: fetch roster for each date ─────────────────────────
            for auction_date, signal_type in sale_dates:
                listings = await _fetch_roster_for_date(
                    client, auction_date, signal_type, today
                )
                all_listings.extend(listings)

        logger.info(
            "RealAuction: total %d listings (%d mortgage, %d tax)",
            len(all_listings),
            sum(1 for l in all_listings if l.signal_type == "mortgage_foreclosure"),
            sum(1 for l in all_listings if l.signal_type == "tax_delinquent_foreclosure"),
        )
        return all_listings

    async def _collect_sale_dates(
        self, client: httpx.AsyncClient, today: date
    ) -> list[tuple[str, str]]:
        """Scan the calendar for _CALENDAR_LOOKAHEAD_MONTHS months and return all
        upcoming sale date strings with their channel (signal_type).

        The calendar endpoint takes a `selCalDate` in the form
        `{ts 'YYYY-MM-01 00:00:00'}` — URL-encoded by httpx automatically.
        """
        sale_dates: list[tuple[str, str]] = []
        seen: set[str] = set()

        for month_offset in range(_CALENDAR_LOOKAHEAD_MONTHS):
            # Compute target month (handle year rollover)
            m = today.month + month_offset
            y = today.year + (m - 1) // 12
            m = ((m - 1) % 12) + 1
            sel_date = f"{{ts '{y:04d}-{m:02d}-01 00:00:00'}}"

            params = {
                "zaction": "USER",
                "zmethod": "CALENDAR",
                "selCalDate": sel_date,
            }
            html_text = await _get_with_retry(
                client, _BASE_URL, params=params, headers=_build_headers()
            )
            await asyncio.sleep(_INTER_REQ_DELAY)

            if html_text is None:
                logger.warning("RealAuction: calendar fetch failed for %s", sel_date)
                continue

            for day_str, sig_type in _parse_calendar(html_text, today):
                if day_str not in seen:
                    seen.add(day_str)
                    sale_dates.append((day_str, sig_type))

        return sorted(sale_dates, key=lambda t: _parse_date(t[0]) or date.max)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run — validates live without touching any DB
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    async def _dry_run() -> None:
        today = today_et()
        print(f"\n=== Summit RealAuction dry-run (today={today}) ===\n")

        async with httpx.AsyncClient(follow_redirects=True) as client:
            scraper = SummitRealAuctionScraper(dry_run=True)

            # --- collect sale dates ---
            sale_dates = await scraper._collect_sale_dates(client, today)
            print(f"Upcoming sale dates ({len(sale_dates)} total):")
            for d, st in sale_dates:
                print(f"  {d}  →  {st}")
            print()

            # --- fetch next 1-2 dates per channel ---
            mortgage_dates = [(d, st) for d, st in sale_dates if st == "mortgage_foreclosure"]
            tax_dates = [(d, st) for d, st in sale_dates if st == "tax_delinquent_foreclosure"]

            sample_dates = mortgage_dates[:2] + tax_dates[:2]
            if not sample_dates:
                print("No upcoming sale dates found — site may be between auction cycles.")
                return

            all_rows: list[RawListing] = []
            for auction_date, signal_type in sample_dates:
                rows = await _fetch_roster_for_date(client, auction_date, signal_type, today)
                all_rows.extend(rows)
                mort = [r for r in rows if r.signal_type == "mortgage_foreclosure"]
                tax = [r for r in rows if r.signal_type == "tax_delinquent_foreclosure"]
                print(f"--- {auction_date} ({signal_type}) → {len(rows)} rows "
                      f"(mort={len(mort)} tax={len(tax)}) ---")

                # Print first 2 rows per date as sample
                for i, r in enumerate(rows[:2]):
                    d = r.model_dump(exclude={"source_site"})
                    # Remove None values for readability
                    d = {k: v for k, v in d.items() if v is not None}
                    print(f"  [{i}] {json.dumps(d, default=str, indent=4)}")
                print()

            print(f"\n=== TOTALS ===")
            mort_rows = [r for r in all_rows if r.signal_type == "mortgage_foreclosure"]
            tax_rows = [r for r in all_rows if r.signal_type == "tax_delinquent_foreclosure"]
            print(f"  mortgage_foreclosure:       {len(mort_rows)}")
            print(f"  tax_delinquent_foreclosure: {len(tax_rows)}")
            print(f"  grand total:                {len(all_rows)}")

            # Spot-check format-lock parcel
            anchor = "7000697"
            anchor_hits = [r for r in all_rows if r.source_listing_id == anchor]
            if anchor_hits:
                print(f"\n  FORMAT-LOCK: parcel {anchor} (CV2024083496) FOUND — {anchor_hits[0].case_number}")
            else:
                print(f"\n  FORMAT-LOCK: parcel {anchor} (CV2024083496) NOT in sampled window "
                      "(may be past/future — check full roster separately)")

            # Verify no 9-digit zips slipped through
            bad_zips = [r for r in all_rows if r.property_zip and len(r.property_zip) > 5
                        and "-" not in r.property_zip]
            if bad_zips:
                print(f"\n  WARNING: {len(bad_zips)} rows with bad ZIP (>5 digits, no hyphen)")
            else:
                print("  ZIP invariant: OK (no 9-digit unhyphenated ZIPs)")

    asyncio.run(_dry_run())
