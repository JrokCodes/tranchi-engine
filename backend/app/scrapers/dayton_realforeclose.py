"""
Montgomery County (OH / Dayton) sheriff foreclosure auction scraper.

Source: https://montgomery.sheriffsaleauction.ohio.gov  (RealForeclose / RealAuction platform)
One sale type: "FC" (Foreclosure) — mortgage foreclosure (ORC 2329) and tax foreclosure
(ORC 5721.19) appear in the SAME weekly roster, unlike Summit's Tue/Fri channel split.
Signal type is inferred per item: $0.00 appraised → tax_delinquent_foreclosure; else →
mortgage_foreclosure.

Role: ENRICHMENT — joins to DCR (dayton_legalnews) on case_number to supply the two fields
DCR lacks: opening_bid_usd and appraised_value_usd. The case_number ("YYYY CV NNNNN") is the
JOIN KEY; source_listing_id (normalized PRINT_KEY) is the spine FK.

INVARIANTS — read before editing:

  1. BROWSER UA OR 403: RealForeclose returns HTTP 403 on any non-browser UA string,
     including a plain "Mozilla/5.0". Always use a full Chrome UA (random_ua()).

  2. PREVIEW-BEFORE-LOAD (cookie state): The PREVIEW call sets a session-cookie that
     scopes the LOAD calls to the correct auction date. One httpx.AsyncClient (shared
     cookie jar) MUST make the PREVIEW GET *before* any LOAD GETs for the same date.
     Interleaving dates without a fresh PREVIEW will read the wrong roster silently.

  3. ONE SALE TYPE (FC): Montgomery mixes mortgage + tax foreclosure in a single weekly
     roster with NO weekday split (unlike Summit Tue=tax / Fri=mortgage). signal_type is
     inferred at item level: appraised_value_usd == 0.0 or None → tax_delinquent_foreclosure;
     all other cases → mortgage_foreclosure. Tax FC cases carry $0.00 appraised + a flat
     deposit; mortgage cases carry the sheriff's official appraised value.

  4. MULTIPLE PARCEL / COMMA-SPLIT: Parcel ID may be the literal string "MULTIPLE" or a
     comma-separated list (multi-parcel case). For "MULTIPLE": store source_listing_id=None.
     For comma-separated: normalize and use the FIRST parcel as source_listing_id (case_number
     is the primary join key so enrichment still lands correctly).

  5. 9-DIGIT ZIP: The city row sometimes carries a 9-digit unhyphenated ZIP
     (e.g. "DAYTON , 454060000"). Truncate to the first 5 digits before storing.

  6. W-AREA LAST-PAGE REPEAT STOP: Area W paginates at 10 items/page. PageDir=1 advances
     forward. The last page re-returns the SAME rlist value — detect this and stop.
     Do NOT use an item-count heuristic; use rlist equality.
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

SITE_NAME = "Montgomery Sheriff Sale (RealForeclose)"

_BASE_URL = "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm"
_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0
_INTER_REQ_DELAY = 1.0          # ≤1 req/sec (robots posture; OH public record)
_CALENDAR_LOOKAHEAD_MONTHS = 3  # scan this many months of calendar pages


# ─────────────────────────────────────────────────────────────────────────────
# Parse helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_money(raw: str | None) -> float | None:
    """'$81,000.00' / '54000.00' → float; None/'' → None."""
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
    """'07/10/2026' → date(2026, 7, 10); None/'' → None."""
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
    """'454060000' → '45406'; '45406-1234' → '45406-1234'; None → None.

    RealForeclose sometimes serves a 9-digit unhyphenated ZIP (INVARIANT #5).
    Only truncate 9+ digit strings that lack a hyphen.
    """
    if not raw:
        return None
    z = str(raw).strip()
    if re.match(r"^\d{9,}$", z):
        return z[:5]
    return z


def _strip_seq(case_raw: str) -> str:
    """'2024 CV 01948 (0)' → '2024 CV 01948'; preserves any letter suffix."""
    return re.sub(r"\s*\(\d+\)\s*$", "", case_raw.strip())


def _build_headers() -> dict[str, str]:
    return {
        "User-Agent": random_ua(),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }


def _build_json_headers() -> dict[str, str]:
    hdrs = _build_headers()
    hdrs["Accept"] = "application/json, text/javascript, */*; q=0.01"
    hdrs["X-Requested-With"] = "XMLHttpRequest"
    hdrs["Referer"] = "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm"
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
                logger.error(
                    "RealForeclose GET %s params=%s failed after %d attempts: %s",
                    url, params, attempt, exc,
                )
                return None
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Calendar parsing
# ─────────────────────────────────────────────────────────────────────────────

# Matches calendar day cells that carry a dayid AND have auction content.
# Montgomery markup (confirmed live 2026-06-27):
#   <div ... dayid='07/10/2026' ...<span class='CALTEXT'><b>Foreclosure</b>...FC...
# Single-quotes on dayid; CALTEXT lookahead filters empty calendar days.
_CALENDAR_DAY_RE = re.compile(
    r"""dayid=['"]([\d/]+)['"][^>]*>(?=(?:(?!dayid=).){0,800}CALTEXT)""",
    re.DOTALL,
)


def _parse_calendar(html_text: str, today: date) -> list[str]:
    """Extract upcoming FC sale date strings from the calendar HTML.

    Returns list of 'MM/DD/YYYY' strings for dates >= today.
    Montgomery has ONE sale type (FC) — no weekday channel split; all CALTEXT
    dates are valid FC sale dates.
    """
    results: list[str] = []
    seen: set[str] = set()

    for m in _CALENDAR_DAY_RE.finditer(html_text):
        raw_dayid = m.group(1).strip()
        if not raw_dayid or raw_dayid in seen:
            continue
        sale_date = _parse_date(raw_dayid)
        if sale_date is None or sale_date < today:
            continue
        seen.add(raw_dayid)
        results.append(raw_dayid)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# retHTML token-template parser  (identical grammar to Summit RealAuction)
# ─────────────────────────────────────────────────────────────────────────────
#
# Items are separated by @A tokens. Fields within an item are separated by @G.
# Each field segment:
#   <th @CAD_LBL"...>LABEL:</th><td @CAD_DTA">VALUE
# An empty LABEL (unlabeled city/zip continuation row after Property Address):
#   <th @CAD_LBL" scope="row"></th><td @CAD_DTA">CITY , ZIP

_FIELD_RE = re.compile(
    r"<th\s[^>]*>([^<]*)</th>\s*<td\s[^>]*>(.*?)(?=<tr|</tbody|$)",
    re.DOTALL | re.IGNORECASE,
)

# City + ZIP: "DAYTON , 45406" or "MIAMISBURG , 454060000"
_CITY_ZIP_RE = re.compile(
    r"^([A-Z][A-Z\s\-]+?)\s*,\s*(\d{5,9})\s*$",
    re.IGNORECASE,
)
_CITY_ONLY_RE = re.compile(r"^([A-Z][A-Z\s\-]+?)\s*,\s*(?:OH)?\s*$", re.IGNORECASE)


def _parse_ret_html(ret_html: str) -> list[dict[str, str]]:
    """Parse the RealForeclose retHTML token-template into a list of field dicts.

    Items are separated by @A; fields within each item by @G. Returns a list of
    dicts with keys matching label text ("Case Status", "Case #", "Parcel ID",
    "Property Address", "Appraised Value", "Opening Bid", "Deposit Requirement")
    plus a special "__city_zip__" key for the unlabeled city/zip continuation row.
    """
    if not ret_html or not ret_html.strip():
        return []

    items: list[dict[str, str]] = []

    for item_block in ret_html.split("@A"):
        if "Case Status" not in item_block:
            continue

        fields: dict[str, str] = {}
        for seg in item_block.split("@G"):
            m = _FIELD_RE.search(seg)
            if not m:
                continue
            label_raw = m.group(1).strip().rstrip(":").strip()
            value_raw = m.group(2).strip()
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

    Raw text: 'DAYTON , 45406' or 'MIAMISBURG , 454060000' (INVARIANT #5).
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


def _infer_signal_type(appraised_usd: float | None) -> str:
    """Infer FC signal_type from the appraised value (INVARIANT #3).

    Tax foreclosure (ORC 5721.19) cases always carry $0.00 appraised + a flat
    deposit. Mortgage foreclosure (ORC 2329) cases carry the official appraised
    value. Cannot determine by weekday (unlike Summit) — appraised value is the
    reliable discriminator here.
    """
    if appraised_usd is None or appraised_usd == 0.0:
        return "tax_delinquent_foreclosure"
    return "mortgage_foreclosure"


def _parse_item(fields: dict[str, str], sale_date: date) -> RawListing | None:
    """Map a single retHTML field dict to a RawListing. Returns None to skip."""
    # ── Validity filter ──────────────────────────────────────────────────────
    case_status_raw = fields.get("Case Status", "").strip().upper()
    if case_status_raw != "ACTIVE":
        logger.debug("RealForeclose: skipping non-ACTIVE row (status=%r)", case_status_raw)
        return None

    # ── Case number (JOIN KEY to DCR) ────────────────────────────────────────
    case_raw = fields.get("Case #", fields.get("Case#", "")).strip()
    case_number = _strip_seq(case_raw) if case_raw else None

    # ── Parcel / source_listing_id (INVARIANT #4) ────────────────────────────
    parcel_raw = fields.get("Parcel ID", fields.get("Parcel", "")).strip()
    source_listing_id: str | None
    if parcel_raw.upper() == "MULTIPLE" or not parcel_raw:
        source_listing_id = None
    elif "," in parcel_raw:
        # Comma-separated multi-parcel: normalize and use the first one.
        # case_number is the primary join key so enrichment still lands correctly.
        first = parcel_raw.split(",")[0].strip()
        source_listing_id = normalize_parcel_number(first) if first else None
    else:
        source_listing_id = normalize_parcel_number(parcel_raw)

    # ── Address ──────────────────────────────────────────────────────────────
    addr_raw = fields.get("Property Address", "").strip()
    property_address = addr_raw if addr_raw else "MULTIPLE"

    city, zip_code = _extract_city_zip(fields)

    # ── Money fields (INVARIANT: all rendered as '$NNN,NNN.NN' strings) ──────
    appraised_value_usd = _parse_money(fields.get("Appraised Value", ""))
    opening_bid_usd = _parse_money(fields.get("Opening Bid", ""))
    deposit_usd = _parse_money(fields.get("Deposit Requirement", ""))

    # ── Signal type inferred from appraised value (INVARIANT #3) ─────────────
    signal_type = _infer_signal_type(appraised_value_usd)

    # ── Emit ─────────────────────────────────────────────────────────────────
    return RawListing(
        source_site=SITE_NAME,
        case_number=case_number,
        source_listing_id=source_listing_id,
        signal_type=signal_type,
        property_address=property_address,
        property_city=city,
        property_county="Montgomery",
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
    today: date,
) -> list[RawListing]:
    """Fetch all listings for one FC auction date.

    Step 2: PREVIEW (sets session-scoped auction date cookie — INVARIANT #2).
    Step 3: LOAD union of AREA C and AREA W with pagination.
    """
    sale_date = _parse_date(auction_date)
    if sale_date is None or sale_date < today:
        return []

    # ── Step 2: PREVIEW ───────────────────────────────────────────────────────
    preview_params = {
        "zaction": "AUCTION",
        "Zmethod": "PREVIEW",
        "AUCTIONDATE": auction_date,
    }
    preview_html = await _get_with_retry(
        client, _BASE_URL, params=preview_params, headers=_build_headers()
    )
    if preview_html is None:
        logger.error("RealForeclose: PREVIEW failed for %s — skipping date", auction_date)
        return []
    await asyncio.sleep(_INTER_REQ_DELAY)

    listings: list[RawListing] = []
    ts = str(int(datetime.now().timestamp() * 1000))

    # ── Step 3: LOAD AREA C (Closed/Canceled) — NOT ingested ─────────────────
    # INVARIANT: AREA C is RealForeclose's "Auctions Closed or Canceled" bucket.
    # For a future-dated sale these are auctions CANCELED before the sale (e.g.
    # "Canceled per Bankruptcy") — they are NOT live buy-now deals. The retHTML
    # carries no "Auction Status" field — only "Case Status", which stays ACTIVE
    # (the court case is still open even when the auction is canceled) — so the
    # AREA is the ONLY canceled-vs-live discriminator. We load it only to log how
    # many we exclude; we do NOT ingest it. (Bug fixed 2026-06-29: AREA C rows
    # were surfacing as active sheriff sales — a live verify caught 2 canceled
    # auctions in the feed. Existing AREA-C rows age out via FULL_RESCAN.)
    c_listings = await _load_area(
        client, area="C", sale_date=sale_date, today=today, ts=ts
    )

    # ── Step 3: LOAD AREA W (Waiting = scheduled) — the live buy-now auctions ─
    w_listings = await _load_area(
        client, area="W", sale_date=sale_date, today=today, ts=ts
    )
    listings.extend(w_listings)

    logger.info(
        "RealForeclose: %s → %d listings (W=%d ingested, C=%d canceled/closed excluded, "
        "mortgage=%d tax=%d)",
        auction_date,
        len(listings),
        len(w_listings),
        len(c_listings),
        sum(1 for l in listings if l.signal_type == "mortgage_foreclosure"),
        sum(1 for l in listings if l.signal_type == "tax_delinquent_foreclosure"),
    )
    return listings


async def _load_area(
    client: httpx.AsyncClient,
    *,
    area: str,
    sale_date: date,
    today: date,
    ts: str,
) -> list[RawListing]:
    """Paginate one AREA (C or W) for the cookie-scoped auction date.

    Returns all pages. Stops when rlist repeats (INVARIANT #6).
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
            logger.debug(
                "RealForeclose: AREA %s page_dir=%d returned empty/invalid JSON",
                area, page_dir,
            )
            break

        rlist: str = (data.get("rlist") or "").strip()
        ret_html: str = data.get("retHTML") or ""

        if not rlist and not ret_html.strip():
            break

        # INVARIANT #6: last page re-returns the same rlist — stop
        if rlist in seen_rlist:
            logger.debug("RealForeclose: AREA %s rlist repeated — last page reached", area)
            break
        seen_rlist.add(rlist)

        for item_fields in _parse_ret_html(ret_html):
            listing = _parse_item(item_fields, sale_date)
            if listing is not None:
                listings.append(listing)

        page_dir = 1

    return listings


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class DaytonRealForecloseScraper(ListingScraper):
    """Montgomery County (OH) sheriff foreclosure listings from RealForeclose.

    Enrichment source: joins to DCR (dayton_legalnews) on case_number to fill
    opening_bid_usd and appraised_value_usd — the two fields DCR lacks.
    One sale type (FC): mortgage + tax foreclosure mixed in a single weekly roster.
    No DB pool required — the orchestrator handles stale/expiry passes via full_rescan.
    """

    site_name = SITE_NAME

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    async def fetch_and_parse(self) -> list[RawListing]:
        today = today_et()
        all_listings: list[RawListing] = []

        # One httpx client with a shared cookie jar across all requests per run.
        # REQUIRED — the PREVIEW call sets a cookie that scopes subsequent LOAD
        # calls to the correct auction date (INVARIANT #2).
        async with httpx.AsyncClient(follow_redirects=True) as client:
            sale_dates = await self._collect_sale_dates(client, today)
            logger.info(
                "RealForeclose: found %d upcoming FC sale dates across %d month(s)",
                len(sale_dates), _CALENDAR_LOOKAHEAD_MONTHS,
            )

            for auction_date in sale_dates:
                listings = await _fetch_roster_for_date(client, auction_date, today)
                all_listings.extend(listings)

        logger.info(
            "RealForeclose: total %d listings (mortgage=%d tax=%d)",
            len(all_listings),
            sum(1 for l in all_listings if l.signal_type == "mortgage_foreclosure"),
            sum(1 for l in all_listings if l.signal_type == "tax_delinquent_foreclosure"),
        )
        return all_listings

    async def _collect_sale_dates(
        self, client: httpx.AsyncClient, today: date
    ) -> list[str]:
        """Scan the calendar for _CALENDAR_LOOKAHEAD_MONTHS months and return all
        upcoming FC sale date strings ('MM/DD/YYYY').

        The calendar endpoint takes a selCalDate in the form
        `{ts 'YYYY-MM-01 00:00:00'}` — URL-encoded by httpx automatically.
        """
        sale_dates: list[str] = []
        seen: set[str] = set()

        for month_offset in range(_CALENDAR_LOOKAHEAD_MONTHS):
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
                logger.warning("RealForeclose: calendar fetch failed for %s", sel_date)
                continue

            for day_str in _parse_calendar(html_text, today):
                if day_str not in seen:
                    seen.add(day_str)
                    sale_dates.append(day_str)

        return sorted(sale_dates, key=lambda s: _parse_date(s) or date.max)


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
        print(f"\n=== Montgomery RealForeclose dry-run (today={today}) ===\n")

        async with httpx.AsyncClient(follow_redirects=True) as client:
            scraper = DaytonRealForecloseScraper(dry_run=True)

            sale_dates = await scraper._collect_sale_dates(client, today)
            print(f"Upcoming FC sale dates ({len(sale_dates)} total):")
            for d in sale_dates:
                print(f"  {d}")
            print()

            if not sale_dates:
                print("No upcoming FC sale dates — site may be between auction cycles.")
                return

            # Sample the next 2 dates
            sample_dates = sale_dates[:2]
            all_rows: list[RawListing] = []
            for auction_date in sample_dates:
                rows = await _fetch_roster_for_date(client, auction_date, today)
                all_rows.extend(rows)
                mort = [r for r in rows if r.signal_type == "mortgage_foreclosure"]
                tax = [r for r in rows if r.signal_type == "tax_delinquent_foreclosure"]
                print(f"--- {auction_date} → {len(rows)} rows "
                      f"(mortgage={len(mort)} tax={len(tax)}) ---")

                for i, r in enumerate(rows[:3]):
                    d = r.model_dump(exclude={"source_site"})
                    d = {k: v for k, v in d.items() if v is not None}
                    print(f"  [{i}] {json.dumps(d, default=str, indent=4)}")
                print()

            print("=== TOTALS ===")
            mort_rows = [r for r in all_rows if r.signal_type == "mortgage_foreclosure"]
            tax_rows = [r for r in all_rows if r.signal_type == "tax_delinquent_foreclosure"]
            print(f"  mortgage_foreclosure:       {len(mort_rows)}")
            print(f"  tax_delinquent_foreclosure: {len(tax_rows)}")
            print(f"  grand total:                {len(all_rows)}")

            # ZIP invariant check
            bad_zips = [
                r for r in all_rows
                if r.property_zip and len(r.property_zip) > 5 and "-" not in r.property_zip
            ]
            if bad_zips:
                print(f"\n  WARNING: {len(bad_zips)} rows with bad ZIP (>5 digits, no hyphen)")
            else:
                print("  ZIP invariant: OK (no 9-digit unhyphenated ZIPs)")

            # Case # format spot-check: should match 'YYYY CV NNNNN' pattern
            bad_case = [
                r for r in all_rows
                if r.case_number and re.search(r"\(\d+\)", r.case_number)
            ]
            if bad_case:
                print(f"\n  WARNING: {len(bad_case)} rows with un-stripped (N) suffix in case#")
            else:
                print("  Case# strip: OK (no (N) suffixes)")

    asyncio.run(_dry_run())
