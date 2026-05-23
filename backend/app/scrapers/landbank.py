"""
Cuyahoga Land Bank scraper.

Site: https://cuyahogalandbank.org/all-available-properties/
Structure: Vanilla WordPress + jQuery DataTables. All rows are preloaded in the
           initial HTML response — no JavaScript execution required. Single GET,
           no pagination, no anti-bot protection.

Table headers (verbatim): Parcel number | Number | Street | City | Ward | Date Posted | Status
Status values (3 only):
  - "New Construction Underway - Available Soon"
  - "Renovation Underway - Available Soon"
  - "Vacant Land - Available"

No price field exists anywhere — Land Bank sells by direct inquiry only.
Detail pages (/property-detail/?parcel_id=<PID>) are fetched for enriched
metadata (lot size, building style, neighborhood, etc.) when enrichment is
enabled. Parcel # (format DDD-NN-NNN) is the dedup key and cross-site join key
to Sheriff Sales and MyPlace.

Cron: every 3h  →  0 */3 * * * (no entry created yet — Phase C)
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.scrapers.base import ListingScraper
from app.scrapers.models import RawListing
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

_LIST_URL = "https://cuyahogalandbank.org/all-available-properties/"
_DETAIL_BASE = "https://cuyahogalandbank.org/property-detail/"

_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0
_INTER_REQUEST_DELAY = 0.75   # seconds between detail-page GETs (jitter applied)
_ENRICH_DETAIL = False        # detail pass adds ~99 GETs (~3min) for price/photos/restrictions
                              # which we don't surface yet — run as a separate weekly pass if needed.
                              # Set True for a one-off enrichment run.

# Land Bank status → canonical status used downstream
_STATUS_MAP: dict[str, str] = {
    "new construction underway - available soon": "active",
    "renovation underway - available soon": "active",
    "vacant land - available": "active",
}

# Months spelled out by the Land Bank's "Date Posted" column
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _get(client: httpx.AsyncClient, url: str, params: dict[str, str] | None = None) -> str | None:
    """GET url with retry on transient errors. Returns response text or None."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error("LandBank: GET %s failed after %d attempts: %s", url, attempt, exc)
                return None
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning("LandBank: GET %s attempt %d failed, retrying in %.1fs: %s", url, attempt, delay, exc)
            await asyncio.sleep(delay)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Parsers — main listing table
# ─────────────────────────────────────────────────────────────────────────────

def _parse_date_posted(raw: str) -> date | None:
    """Parse "May 4, 2026" or "April 22, 2025" into a date object."""
    raw = raw.strip()
    # Expected format: "Month D, YYYY"
    m = re.match(r"(\w+)\s+(\d{1,2}),?\s+(\d{4})", raw, re.IGNORECASE)
    if not m:
        return None
    month_name, day_str, year_str = m.group(1).lower(), m.group(2), m.group(3)
    month = _MONTHS.get(month_name)
    if not month:
        return None
    try:
        return date(int(year_str), month, int(day_str))
    except ValueError:
        return None


def _cell_text(cell: Any) -> str:
    """Strip whitespace from a BeautifulSoup tag."""
    return cell.get_text(separator=" ").strip()


def _parse_listing_rows(html: str) -> list[dict[str, str]]:
    """Parse the DataTables listing table and return raw row dicts."""
    soup = BeautifulSoup(html, "html.parser")

    # The table has id="properties-list-table" or similar.
    # Fall back to any table containing the expected headers.
    table = soup.find("table", id=re.compile(r"properties", re.IGNORECASE))
    if table is None:
        # Generic fallback: find a table whose headers match
        for tbl in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in tbl.find_all("th")]
            if "parcel number" in headers or "parcel" in " ".join(headers):
                table = tbl
                break

    if table is None:
        logger.error("LandBank: could not locate listing table in HTML")
        return []

    # Map header names → column index (case-insensitive)
    header_row = table.find("thead")
    if header_row is None:
        logger.error("LandBank: table has no <thead>")
        return []

    col_index: dict[str, int] = {}
    for i, th in enumerate(header_row.find_all("th")):
        label = th.get_text(strip=True).lower()
        col_index[label] = i

    # Expected columns from the field map (verbatim headers):
    # "parcel number", "number", "street", "city", "ward", "date posted", "status"
    required = {"parcel number", "number", "street", "city", "status"}
    missing = required - col_index.keys()
    if missing:
        logger.error("LandBank: table missing expected columns: %s (found: %s)", missing, list(col_index.keys()))
        return []

    tbody = table.find("tbody")
    if tbody is None:
        logger.warning("LandBank: table has no <tbody>, attempting rows in <table>")
        rows = [r for r in table.find_all("tr") if r.find("td")]
    else:
        rows = tbody.find_all("tr")

    records: list[dict[str, str]] = []
    for row in rows:
        cells = row.find_all("td")
        if not cells:
            continue

        def col(name: str) -> str:
            idx = col_index.get(name)
            if idx is None or idx >= len(cells):
                return ""
            return _cell_text(cells[idx])

        parcel = col("parcel number")
        if not parcel:
            continue  # skip header-repeat or empty rows

        records.append({
            "parcel_number": parcel,
            "number": col("number"),
            "street": col("street"),
            "city": col("city"),
            "ward": col("ward"),
            "date_posted": col("date posted"),
            "status": col("status"),
        })

    return records


# ─────────────────────────────────────────────────────────────────────────────
# Parsers — detail page enrichment (optional second pass)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_detail(html: str) -> dict[str, Any]:
    """Extract structured fields from a /property-detail/?parcel_id=<PID> page.

    Returns a dict of enrichment fields; empty dict on parse failure.
    These land in the 'metadata' field of RawListing if supported by schema,
    or are silently dropped if not. Either way they don't affect upsert correctness.
    """
    soup = BeautifulSoup(html, "html.parser")
    meta: dict[str, Any] = {}

    # Field labels verbatim from field map — look for <th> or <label> + value
    # The detail page uses a structured dl or table layout per the Playwright probe.
    # Strategy: find any element whose text contains the label, grab the sibling/next value.
    label_map = {
        "Lot Size": "lot_size_sqft",
        "Lot Shape": "lot_shape",
        "Lot Dimensions": "lot_dimensions",
        "Year Built": "year_built",
        "Building Style": "building_style",
        "Building Size": "building_size_sqft",
        "Num Rooms": "num_rooms",
        "Num Bedrooms": "num_bedrooms",
        "Num Baths": "num_baths",
        "Neighborhood": "neighborhood",
        "Cleveland Ward": "ward",
        "Cuyahoga District": "cuyahoga_district",
        "Additional Information": "additional_info",
    }

    # Photo URLs: look for <img> tags whose src matches the GCS pattern
    photo_urls: list[str] = []
    gcs_pattern = re.compile(r"storage\.googleapis\.com/cclrc-pps2\.appspot\.com", re.IGNORECASE)
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if gcs_pattern.search(src):
            photo_urls.append(src)
    if photo_urls:
        meta["photo_urls"] = photo_urls

    # Attempt structured field extraction
    # Most WordPress detail pages render fields as <tr><th>Label</th><td>Value</td></tr>
    # or <div class="field-label">Label</div><div class="field-value">Value</div>
    for label, key in label_map.items():
        # Try th/td table pattern first
        th = soup.find(lambda tag: tag.name in ("th", "dt", "label", "strong")
                       and label.lower() in tag.get_text(strip=True).lower())
        if th:
            # Sibling td or dd
            sibling = th.find_next_sibling(["td", "dd"])
            if sibling:
                meta[key] = sibling.get_text(strip=True)
                continue
            # Next element in a flat div structure
            parent = th.parent
            if parent:
                value_div = parent.find_next_sibling()
                if value_div:
                    meta[key] = value_div.get_text(strip=True)

    return meta


# ─────────────────────────────────────────────────────────────────────────────
# Row → RawListing conversion
# ─────────────────────────────────────────────────────────────────────────────

def _to_raw_listing(row: dict[str, str], detail_meta: dict[str, Any] | None = None) -> RawListing | None:
    """Convert a parsed row dict into a RawListing. Returns None on bad data."""
    parcel = row["parcel_number"].strip()
    number = row["number"].strip()
    street = row["street"].strip()
    city = row["city"].strip() or None
    status_raw = row["status"].strip()
    date_posted_raw = row["date_posted"].strip()

    # Address: "NUMBER STREET"
    if number and street:
        property_address = f"{number} {street}"
    elif street:
        property_address = street
    else:
        logger.debug("LandBank: skipping parcel %s — no street address", parcel)
        return None

    # Normalize status
    canonical_status = _STATUS_MAP.get(status_raw.lower(), "active")

    # Date posted → use as sale_date? For Land Bank, there's no sale date.
    # We parse date_posted purely for metadata; sale_date stays None.
    posted_date = _parse_date_posted(date_posted_raw) if date_posted_raw else None

    listing = RawListing(
        source_site="Cuyahoga Land Bank",
        source_listing_id=parcel,
        case_number=parcel,            # parcel# is the dedup key per field map
        signal_type="land_bank_inventory",
        property_address=property_address,
        property_city=city,
        property_county="Cuyahoga",
        property_state="OH",
        sale_date=None,                # Land Bank = inquiry only, no auction date
        deposit_usd=None,              # no price field
        status=canonical_status,
    )

    # Attach enrichment metadata if available; also store date_posted + ward
    # in a flat metadata dict. The DB upsert ignores unknown fields gracefully.
    extra: dict[str, Any] = {}
    if posted_date:
        extra["date_posted"] = posted_date.isoformat()
    if row.get("ward"):
        extra["ward"] = row["ward"]
    if detail_meta:
        extra.update(detail_meta)
    if extra:
        # RawListing doesn't have a metadata field in current schema — store as
        # a private attribute for logging/debug only. Phase C can add JSONB if needed.
        object.__setattr__(listing, "_metadata", extra)

    return listing


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class LandBankScraper(ListingScraper):
    """Scraper for Cuyahoga Land Bank available-properties inventory.

    Single GET to the listing page extracts all rows from the DataTables HTML.
    Optional second pass fetches each parcel's detail page for enriched fields.
    No price or auction date is available — Land Bank sells by inquiry only.

    Run: every 3h (Phase C cron)
    Source: https://cuyahogalandbank.org/all-available-properties/
    """

    site_name = "Cuyahoga Land Bank"

    async def fetch_and_parse(self) -> list[RawListing]:
        headers = default_headers()
        listings: list[RawListing] = []

        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            # ── Step 1: fetch the main listing page ──────────────────────────
            logger.info("LandBank: fetching listing page %s", _LIST_URL)
            html = await _get(client, _LIST_URL)
            if html is None:
                logger.error("LandBank: failed to fetch listing page — returning empty")
                return []

            # ── Step 2: parse the DataTable rows ─────────────────────────────
            rows = _parse_listing_rows(html)
            logger.info("LandBank: found %d rows in DataTable", len(rows))

            if not rows:
                logger.warning("LandBank: 0 rows parsed — check HTML structure changes")

            # Log count anomaly flagged in the field map (expected ~144, probe saw 99)
            if len(rows) < 80:
                logger.warning(
                    "LandBank: row count %d is below expected minimum (~99). "
                    "Possible pagination change or inventory update.",
                    len(rows),
                )

            # ── Step 3: optional detail-page enrichment ───────────────────────
            for i, row in enumerate(rows):
                parcel = row["parcel_number"]
                detail_meta: dict[str, Any] | None = None

                if _ENRICH_DETAIL:
                    detail_url = _DETAIL_BASE
                    detail_html = await _get(client, detail_url, params={"parcel_id": parcel})
                    if detail_html:
                        detail_meta = _parse_detail(detail_html)
                        logger.debug(
                            "LandBank: enriched %s — %d meta fields, %d photos",
                            parcel,
                            len(detail_meta),
                            len(detail_meta.get("photo_urls", [])),
                        )
                    else:
                        logger.debug("LandBank: detail fetch failed for %s, skipping enrichment", parcel)

                    # Throttle between detail-page GETs
                    if i < len(rows) - 1:
                        jitter = _INTER_REQUEST_DELAY + (0.5 if i % 3 == 0 else 0.0)
                        await asyncio.sleep(jitter)

                listing = _to_raw_listing(row, detail_meta)
                if listing is not None:
                    listings.append(listing)

        logger.info("LandBank: returning %d RawListings", len(listings))
        return listings
