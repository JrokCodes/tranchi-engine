"""
Shelby County (TN) Tax Sale scraper — pre-sale distress catalog.

Source:
  CSV:      https://scgpublic.s3.amazonaws.com/TaxSaleExtract.csv
  Schedule: https://www.shelbycountytrustee.com/191/Tax-Sale-Schedule

INVARIANT (read before editing):
  - The CSV is a DYNAMIC pre-sale catalog. Rows DROP as owners pay delinquent
    taxes before the sale — absence means payment/resolution, not stale data.
    Staleness policy = FULL_RESCAN; retire-by-absence is CORRECT for this source.
  - Alt_Parcel (14-char alphanumeric, always present, always length 14) is the
    canonical parcel identifier. normalize_parcel_number(Alt_Parcel) is already
    the 14-char form but must still be called so cross-source joins work.
    Do NOT use the spaced ParcelID column (e.g. '072047  00016') as the id key.
  - sale_date comes from the SCHEDULE page (sale code → date map) NOT the CSV.
    The CSV only has the sale code (e.g. 'TS2302'). A code absent from the
    schedule means the date is genuinely unknown — leave sale_date null, never guess.
  - Phase-2 note: TN is a redeemable-tax-deed state. POST-sale redemption and
    'sold' outcomes live in Zeus auction results + Trustee 'Properties Under
    Redemption'. Cross-checking those is out of scope for Phase 1; this scraper
    covers the PRE-sale catalog only.

CSV columns (verified live 2026-06-01):
  ParcelID, Alt_Parcel, Street Number, Street Name, Tax Sale, Register GIS
  No amount/tax/owner/city/zip columns exist in the current CSV.

Schedule table (verified live 2026-06-01):
  SALE #, SALE DATE, DATE OF PUBLICATION, PLACE OF PUBLICATION
  Schedule sale numbers are 4-digit (e.g. '2301'); CSV tax-sale codes are 'TS' + 4-digit.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from datetime import date, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.scrapers.base import ListingScraper
from app.scrapers.db import canonical_address, canonical_city, normalize_parcel_number
from app.scrapers.models import RawListing
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

SITE_NAME = "Shelby County Tax Sale"

_CSV_URL = "https://scgpublic.s3.amazonaws.com/TaxSaleExtract.csv"
_SCHEDULE_URL = "https://www.shelbycountytrustee.com/191/Tax-Sale-Schedule"

_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0

# "TS2301" → "2301" (strip 'TS' prefix to match schedule table's raw sale#)
_CODE_RE = re.compile(r"^TS(\d{4})$", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _get(
    client: httpx.AsyncClient,
    url: str,
    *,
    accept: str = "text/html,*/*",
) -> str | None:
    """GET with retry on transient errors. Returns response text or None."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url, timeout=_TIMEOUT, headers={"Accept": accept})
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error(
                    "ShelbyTaxSale: GET %s failed after %d attempts: %s",
                    url, attempt, exc,
                )
                return None
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "ShelbyTaxSale: GET %s attempt %d failed, retrying in %.1fs: %s",
                url, attempt, delay, exc,
            )
            await asyncio.sleep(delay)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Schedule parser — builds {sale_code: date} map
# ─────────────────────────────────────────────────────────────────────────────

def _parse_schedule(html: str) -> dict[str, date]:
    """Parse the Trustee sale-schedule table into a {sale_code_str: date} map.

    Schedule table headers: SALE #, SALE DATE, DATE OF PUBLICATION, PLACE OF PUBLICATION
    Sale # column contains raw 4-digit numbers (e.g. '2301'). The CSV uses 'TS2301'.
    We map both forms: 'TS2301' and '2301' → same date, so callers can key by either.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        logger.warning("ShelbyTaxSale: schedule page has no <table> — sale_date will be null for all rows")
        return {}

    col_idx: dict[str, int] = {}
    header_row = table.find("thead") or table
    for i, th in enumerate(header_row.find_all("th")):
        label = th.get_text(strip=True).lower()
        col_idx[label] = i

    sale_num_col = col_idx.get("sale #")
    sale_date_col = col_idx.get("sale date")
    if sale_num_col is None or sale_date_col is None:
        logger.warning(
            "ShelbyTaxSale: schedule table missing expected columns (found: %s)",
            list(col_idx.keys()),
        )
        return {}

    schedule: dict[str, date] = {}
    tbody = table.find("tbody") or table
    for row in tbody.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        if sale_num_col >= len(cells) or sale_date_col >= len(cells):
            continue
        raw_num = cells[sale_num_col].get_text(strip=True)   # e.g. "2301"
        raw_date = cells[sale_date_col].get_text(strip=True)  # e.g. "10/27/2026"
        if not raw_num or not raw_date:
            continue
        parsed = _parse_mdy(raw_date)
        if parsed is None:
            logger.warning("ShelbyTaxSale: could not parse schedule date %r for sale #%s", raw_date, raw_num)
            continue
        # Store under both key forms so callers don't have to worry about prefix
        schedule[raw_num] = parsed          # "2301"
        schedule[f"TS{raw_num}"] = parsed   # "TS2301"

    logger.info("ShelbyTaxSale: loaded %d sale-date entries from schedule", len(schedule) // 2)
    return schedule


def _parse_mdy(raw: str | None) -> date | None:
    """'10/27/2026' or '04/21/2026' → date; None on failure."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CSV parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_csv_rows(text: str) -> list[dict[str, str]]:
    """Parse raw CSV text into a list of row dicts. Returns [] on failure."""
    try:
        reader = csv.DictReader(io.StringIO(text))
        # Validate expected headers are present
        expected = {"ParcelID", "Alt_Parcel", "Street Number", "Street Name", "Tax Sale"}
        if reader.fieldnames is None:
            logger.error("ShelbyTaxSale: CSV has no headers")
            return []
        actual = set(reader.fieldnames)
        missing = expected - actual
        if missing:
            logger.error(
                "ShelbyTaxSale: CSV missing expected columns: %s (found: %s)",
                missing, list(actual),
            )
            return []
        rows = list(reader)
        logger.info("ShelbyTaxSale: parsed %d rows from CSV (columns: %s)", len(rows), list(actual))
        return rows
    except Exception as exc:
        logger.error("ShelbyTaxSale: CSV parse failed: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Row → RawListing conversion
# ─────────────────────────────────────────────────────────────────────────────

def _row_to_listing(
    row: dict[str, str],
    schedule: dict[str, date],
) -> RawListing | None:
    """Convert one CSV row to a RawListing. Returns None on unrecoverable bad data."""
    alt_parcel = row.get("Alt_Parcel", "").strip()
    if not alt_parcel:
        logger.debug("ShelbyTaxSale: skipping row — empty Alt_Parcel")
        return None

    # Normalize parcel: Alt_Parcel is already 14-char canonical form, but
    # normalize_parcel_number must still be called for cross-source join correctness.
    norm_parcel = normalize_parcel_number(alt_parcel)
    if not norm_parcel:
        logger.debug("ShelbyTaxSale: skipping row — parcel normalization failed for %r", alt_parcel)
        return None

    # Build address from Street Number + Street Name
    # Street Number '0' means the county has no numeric address (vacant lot) — treat as no number.
    street_num = row.get("Street Number", "").strip()
    street_name = row.get("Street Name", "").strip()
    if street_num and street_num != "0" and street_name:
        raw_addr = f"{street_num} {street_name}"
    elif street_name:
        raw_addr = street_name
    else:
        # No usable address — anchor on parcel so the listing is still importable
        raw_addr = f"Parcel {norm_parcel}"

    property_address = canonical_address(raw_addr)
    if not property_address:
        property_address = raw_addr  # canonical_address only returns None for empty strings

    # Sale code → date via schedule map
    tax_sale_code = row.get("Tax Sale", "").strip()  # e.g. "TS2302"
    sale_date = schedule.get(tax_sale_code)          # None if code not on the schedule

    return RawListing(
        source_site=SITE_NAME,
        source_listing_id=norm_parcel,
        case_number=tax_sale_code or None,
        signal_type="tax_deed",
        property_address=property_address,
        property_city=None,        # No city column in current CSV (Phase 2: derive from parcel registry)
        property_county="Shelby",
        property_state="TN",       # IMPORTANT: TN not OH — default in RawListing is OH
        property_zip=None,         # No zip column in current CSV
        sale_date=sale_date,
        opening_bid_usd=None,      # No amount column in current CSV
        status="active",
        auction_status="scheduled" if sale_date else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class ShelbyTaxSaleScraper(ListingScraper):
    """Scraper for Shelby County (TN) Tax Sale pre-sale catalog.

    Fetches a CSV of all properties with unpaid taxes scheduled for upcoming
    tax-deed auctions, maps each row to a RawListing with:
      - source_listing_id = normalize_parcel_number(Alt_Parcel) [14-char canonical]
      - sale_date = looked up from the Trustee sale-schedule page
      - property_state = "TN" (override — RawListing defaults to "OH")
      - signal_type = "tax_deed"

    Staleness: FULL_RESCAN. Rows vanish from the CSV when owners pay before
    the sale — retire-by-absence is the correct behavior.

    Source: https://scgpublic.s3.amazonaws.com/TaxSaleExtract.csv
    Schedule: https://www.shelbycountytrustee.com/191/Tax-Sale-Schedule
    """

    site_name = SITE_NAME

    async def fetch_and_parse(self) -> list[RawListing]:
        headers = default_headers()
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            # ── Step 1: fetch sale schedule → {sale_code: date} map ──────────
            logger.info("ShelbyTaxSale: fetching sale schedule from %s", _SCHEDULE_URL)
            schedule_html = await _get(client, _SCHEDULE_URL)
            if schedule_html is None:
                logger.error(
                    "ShelbyTaxSale: could not fetch sale schedule — sale_date will be null for all listings"
                )
                schedule: dict[str, date] = {}
            else:
                schedule = _parse_schedule(schedule_html)

            # ── Step 2: fetch CSV ─────────────────────────────────────────────
            logger.info("ShelbyTaxSale: fetching CSV from %s", _CSV_URL)
            csv_text = await _get(client, _CSV_URL, accept="text/csv,*/*")
            if csv_text is None:
                logger.error("ShelbyTaxSale: could not fetch CSV — returning empty")
                return []

        # ── Step 3: parse CSV rows ────────────────────────────────────────────
        rows = _parse_csv_rows(csv_text)
        if not rows:
            logger.warning("ShelbyTaxSale: 0 rows parsed — check CSV format")
            return []

        # ── Step 4: convert rows to RawListings ──────────────────────────────
        listings: list[RawListing] = []
        skipped = 0
        for row in rows:
            listing = _row_to_listing(row, schedule)
            if listing is not None:
                listings.append(listing)
            else:
                skipped += 1

        if skipped:
            logger.warning("ShelbyTaxSale: skipped %d rows (empty parcel or bad data)", skipped)

        logger.info(
            "ShelbyTaxSale: returning %d RawListings from %d CSV rows (schedule codes: %s)",
            len(listings),
            len(rows),
            sorted({r.case_number for r in listings if r.case_number}),
        )
        return listings
