"""
Cuyahoga County Sheriff Sales scraper — Tax Delinquent foreclosures only.

Site: https://cpdocket.cp.cuyahogacounty.gov/sheriffsearch/search.aspx
Platform: ProWare ASP.NET WebForms (Build 2.6.0416). No T&C gate.

Key invariants (from Playwright probe 2026-05-23):
  - Dropdown values: "M/D/YYYY 12:00:00 AM" + single-char suffix.
    Suffix 'T' = Tax Delinquent, 'N' = Non-Tax. Filter to 'T' only.
  - Dropdown shows ONLY past/completed dates — no upcoming sales.
    Future sales require a separate Realauction path (Phase 2, out of scope).
  - Date-mode POST redirects through check.aspx → results.aspx (httpx
    follows both redirects automatically; no special handling needed).
  - results.aspx contains a GridView (id contains 'gvSaleSummary') with 15
    direct-child <tr> rows for a typical Tax Delinquent date. No pagination.
  - Each <tr> holds a single <td> containing 3 inner <table> elements. All
    field values are in <span> elements with IDs matching the pattern:
      SheetContentPlaceHolder_C_searchresults_gvSaleSummary_<fieldname>_<row_index>
  - detail.aspx carries NO query string — session-bound POST state.
    Detail pages are SKIPPED in Phase 1; result table has sufficient fields.

Date strategy:
  - Normal run: last 30 days of past Tax Delinquent dates (~3-4 dates).
  - Backfill mode (SHERIFF_BACKFILL=1 env var or backfill=True constructor):
    all historical Tax Delinquent dates in the dropdown (~18 as of 2026-05-23).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime
from typing import NamedTuple

from bs4 import BeautifulSoup

from app.scrapers.base import ListingScraper
from app.scrapers.models import RawListing
from app.scrapers.proware_client import ProwareSession
from app.scrapers._time import n_days_ago_et

logger = logging.getLogger(__name__)

_BASE_URL = "https://cpdocket.cp.cuyahogacounty.gov"
_SEARCH_PATH = "/sheriffsearch/search.aspx"

# Field name for the sale-date dropdown (from field-map section B, verbatim)
_DROPDOWN_FIELD = "ctl00$SheetContentPlaceHolder$c_search1$ddlSaleDate"
# Submit button name (from field-map section B, verbatim)
_SEARCH_BUTTON = "ctl00$SheetContentPlaceHolder$c_search1$btnSearch"

# Jitter between per-date POSTs (per plan §scraper-identity)
_JITTER_MIN = 0.5
_JITTER_MAX = 1.5

# Default lookback window for non-backfill runs
_LOOKBACK_DAYS = 30

# Span ID suffix → field name mapping (discovered via DOM inspection 2026-05-23)
# IDs follow the pattern: ...gvSaleSummary_<SPAN_SUFFIX>_<row_index>
# We extract by matching on the suffix before the trailing underscore+digits.
_SPAN_FIELD_MAP: dict[str, str] = {
    "lnkCaseNum": "case_number",        # <a> inside the case# cell (extracted as anchor text)
    "lblPlaintiffName": "plaintiff",
    "lblPlaintiffAtty": "attorney",
    "lblDefendant": "defendant",
    "lblAppraised": "appraised",
    "lblOpeningBid": "minimum_bid",
    "lblSaleDate2": "sale_date",
    "lblStatus": "status",
    "lblLandTypeData": "land_type",
    "lblAddress": "address",
    "lblDescription": "description",
}


class _SaleDate(NamedTuple):
    """A parsed entry from the sale-date dropdown."""
    raw_value: str   # e.g. "5/20/2026 12:00:00 AMT"
    sale_date: date
    suffix: str      # 'T' = Tax Delinquent, 'N' = Non-Tax


def _parse_dropdown_options(html: str) -> list[_SaleDate]:
    """Extract and parse all sale-date options from the search form dropdown.

    Dropdown value format: "M/D/YYYY 12:00:00 AM" + single-char suffix.
    e.g. "5/20/2026 12:00:00 AMT"
    """
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", {"name": _DROPDOWN_FIELD})
    if not select:
        logger.warning("sheriff: date dropdown not found in landing page")
        return []

    results: list[_SaleDate] = []
    for option in select.find_all("option"):
        raw = (option.get("value") or "").strip()
        if not raw:
            continue

        suffix = raw[-1]
        date_time_str = raw[:-1].strip()

        try:
            parsed_dt = datetime.strptime(date_time_str, "%m/%d/%Y %I:%M:%S %p")
            results.append(_SaleDate(raw_value=raw, sale_date=parsed_dt.date(), suffix=suffix))
        except ValueError:
            logger.debug("sheriff: could not parse dropdown value %r — skipping", raw)

    logger.info("sheriff: %d total date options in dropdown", len(results))
    return results


def _parse_money(raw: str | None) -> float | None:
    """Parse '$59,968.92' → 59968.92."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.]", "", raw.strip())
    if not cleaned:
        return None
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


def _parse_date_str(raw: str | None) -> date | None:
    if not raw:
        return None
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _get_span_by_id_suffix(container: BeautifulSoup, suffix: str, row_idx: int) -> str | None:
    """Find the span/anchor with ID ending in '_<suffix>_<row_idx>' and return its text.

    ProWare GridView IDs follow: ..._gvSaleSummary_<suffix>_<row_idx>
    """
    target_id_end = f"_{suffix}_{row_idx}"
    # Search spans first
    tag = container.find(
        lambda t: t.name in ("span", "a")
        and (t.get("id") or "").endswith(target_id_end)
    )
    if tag:
        return tag.get_text(strip=True) or None
    return None


def _parse_result_page(html: str, fallback_date: date) -> list[RawListing]:
    """Parse the results.aspx GridView into a list of RawListing objects.

    The outer table has ID containing 'gvSaleSummary'. Each direct-child <tr>
    is one case. Fields are extracted via span IDs (see _SPAN_FIELD_MAP).
    """
    soup = BeautifulSoup(html, "html.parser")

    # Locate the GridView table
    gridview = soup.find(
        "table",
        id=lambda x: x and "gvSaleSummary" in x,
    )
    if not gridview:
        body_text = soup.get_text()
        if any(p in body_text.lower() for p in ("no results", "no records", "0 records")):
            logger.info("sheriff: results page reports no records for %s", fallback_date)
        else:
            logger.warning(
                "sheriff: gvSaleSummary table not found for %s — page structure may have changed",
                fallback_date,
            )
        return []

    # Direct-child <tr> rows = one per case (no header row in this GridView)
    case_rows = gridview.find_all("tr", recursive=False)
    if not case_rows:
        logger.info("sheriff: no case rows found for %s", fallback_date)
        return []

    listings: list[RawListing] = []

    for row_idx, row in enumerate(case_rows):
        # Extract each field by span ID suffix + row index
        case_number = _get_span_by_id_suffix(row, "lnkCaseNum", row_idx)
        plaintiff = _get_span_by_id_suffix(row, "lblPlaintiffName", row_idx)
        defendant = _get_span_by_id_suffix(row, "lblDefendant", row_idx)
        minimum_bid_raw = _get_span_by_id_suffix(row, "lblOpeningBid", row_idx)
        appraised_raw = _get_span_by_id_suffix(row, "lblAppraised", row_idx)
        sale_date_raw = _get_span_by_id_suffix(row, "lblSaleDate2", row_idx)
        status_raw = _get_span_by_id_suffix(row, "lblStatus", row_idx)
        address_raw = _get_span_by_id_suffix(row, "lblAddress", row_idx)

        # Parcel # is in an <a> tag inside the row (no distinctive span ID —
        # it's a postback anchor with no ID, sitting in the inner parcel table).
        # Reliable pattern: look for an <a> whose text matches the parcel format DDD-NN-NNN.
        parcel_number: str | None = None
        for anchor in row.find_all("a"):
            text = anchor.get_text(strip=True)
            if re.match(r"^\d{3}-\d{2}-\d{3}$", text):
                parcel_number = text
                break

        if not case_number:
            logger.debug(
                "sheriff: row %d missing case_number — skipping (likely a wrapper row)",
                row_idx,
            )
            continue

        # Resolve sale date
        resolved_date = _parse_date_str(sale_date_raw) or fallback_date

        # Address: title-case and strip double-spaces
        street = " ".join(address_raw.split()).title() if address_raw else ""
        if not street:
            street = f"Unknown — Case {case_number}"

        # Determine listing status
        status_lower = (status_raw or "").lower()
        if any(k in status_lower for k in ("sold", "forfeit", "withdrawn", "cancelled", "canceled")):
            listing_status = "expired"
        else:
            listing_status = "active"

        # Defendant is the foreclosed property owner. RawListing.trustee_name is
        # the closest available field (used for attorney/firm name in Gotham scrapers
        # but semantically appropriate as the responsible party name here).
        defendant_clean = (
            defendant.strip().replace("/", " ").strip() if defendant else None
        )

        listings.append(
            RawListing(
                source_site="sheriff_sales",
                signal_type="tax_delinquent_foreclosure",
                case_number=case_number,
                source_listing_id=parcel_number,   # Parcel # is the cross-source join key
                property_address=street,
                property_city=None,                # Not available in result row (detail page only)
                property_county="Cuyahoga",
                property_state="OH",
                property_zip=None,
                sale_date=resolved_date,
                sale_time=None,
                deposit_usd=_parse_money(minimum_bid_raw),
                trustee_name=defendant_clean,
                sale_location=None,
                status=listing_status,
            )
        )

    return listings


class SheriffSalesScraper(ListingScraper):
    """Scrape Tax Delinquent foreclosure cases from Cuyahoga Sheriff Sales.

    Normal run: queries the last 30 days of past Tax Delinquent sale dates (~3-4 dates).
    Backfill run: queries all historical Tax Delinquent dates in the dropdown.
    Enable backfill via SHERIFF_BACKFILL=1 env var or backfill=True constructor arg.
    """

    site_name = "sheriff_sales"

    def __init__(self, *, backfill: bool = False) -> None:
        env_backfill = os.environ.get("SHERIFF_BACKFILL", "0").strip() == "1"
        self._backfill = backfill or env_backfill

    async def fetch_and_parse(self) -> list[RawListing]:
        all_listings: list[RawListing] = []

        async with ProwareSession(_BASE_URL) as session:
            # Step 1: GET landing page — parse dropdown options
            logger.info("sheriff: fetching landing page %s%s", _BASE_URL, _SEARCH_PATH)
            landing_html = await self._get_html(session, _SEARCH_PATH)
            all_dates = _parse_dropdown_options(landing_html)

            # Step 2: Filter to Tax Delinquent ('T') only
            tax_dates = [d for d in all_dates if d.suffix == "T"]
            logger.info("sheriff: %d Tax Delinquent dates in dropdown", len(tax_dates))

            # Step 3: Apply lookback window (skip for backfill mode)
            if self._backfill:
                target_dates = tax_dates
                logger.info(
                    "sheriff: BACKFILL mode — querying all %d Tax Delinquent dates",
                    len(target_dates),
                )
            else:
                cutoff = n_days_ago_et(_LOOKBACK_DAYS)
                target_dates = [d for d in tax_dates if d.sale_date >= cutoff]
                logger.info(
                    "sheriff: normal mode — %d Tax Delinquent dates within last %d days (cutoff: %s)",
                    len(target_dates), _LOOKBACK_DAYS, cutoff,
                )

            if not target_dates:
                logger.info("sheriff: no Tax Delinquent dates to query")
                return []

            # Step 4: POST per-date search, parse results
            for i, sale_date_entry in enumerate(target_dates):
                if i > 0:
                    # Small jitter between requests (low-detection posture)
                    jitter = _JITTER_MIN + (
                        (_JITTER_MAX - _JITTER_MIN) * ((i * 37) % 100) / 100
                    )
                    await asyncio.sleep(jitter)

                logger.info(
                    "sheriff: querying %s (%d/%d)",
                    sale_date_entry.sale_date, i + 1, len(target_dates),
                )

                # Fresh ViewState per POST (server state resets on each GET)
                fresh_state = await session.fetch_form_state(_SEARCH_PATH)

                result_html, _ = await session.post_back(
                    _SEARCH_PATH,
                    target="",
                    argument="",
                    extra_fields={
                        _DROPDOWN_FIELD: sale_date_entry.raw_value,
                        _SEARCH_BUTTON: "Start Search",
                    },
                    viewstate=fresh_state,
                )

                date_listings = _parse_result_page(result_html, sale_date_entry.sale_date)
                logger.info(
                    "sheriff: %s → %d cases",
                    sale_date_entry.sale_date, len(date_listings),
                )
                all_listings.extend(date_listings)

        logger.info(
            "sheriff: total Tax Delinquent listings: %d", len(all_listings)
        )
        return all_listings

    async def _get_html(self, session: ProwareSession, path: str) -> str:
        """GET a page and return the raw HTML body.

        ProwareSession.fetch_form_state() parses and discards the HTML body.
        This method preserves it for cases where we need to inspect the page
        content (e.g. parsing the sale-date dropdown).
        """
        client = session._assert_client()
        await session._rate_limiter()
        resp = await client.get(path)
        resp.raise_for_status()
        return resp.text
