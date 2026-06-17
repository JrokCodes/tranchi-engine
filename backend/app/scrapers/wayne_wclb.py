"""
Wayne County Land Bank (WCLB) scraper — ePropertyPlus JSON REST API.

INVARIANT — SPA HTML TRAP: The WCLB ePropertyPlus endpoint returns a full
React SPA HTML page (HTTP 200) when the query params are missing or malformed.
A 200 status is NOT confirmation of a JSON response. Every response is validated
as JSON with the expected top-level keys before any row is consumed. If the body
fails this check the scraper raises immediately with a clear error rather than
silently misprocessing HTML as listings.

INVARIANT: Only rows where available == 'Y' are live buy-now listings.
Rows where available == 'N' (holds, reviewed, under-contract) are excluded.
The `comments` field often carries the hold reason for excluded rows.

WCLB is the out-county Wayne County entity (waynecountylandbank.com) — distinct
from DLBA (buildingdetroit.org), which covers the City of Detroit.
Municipalities served: Allen Park, Highland Park, Inkster, Ecorse, etc.

Parcel format: 14-digit packed Wayne County out-county form (e.g. '30004020981000').
normalize_parcel_number() returns these verbatim (14-char alphanumeric, no spaces).

Staleness policy: FULL_RESCAN — the API re-publishes the full inventory on every
call. Absent rows in a given run are genuinely gone.
Source: https://public-wclb.epropertyplus.com/landmgmtpub/remote/public/property/
        getPublishedProperties?page=1&limit=50&json={"criterias":[]}
Volume: ~812 published rows (≈17 pages at limit=50).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.scrapers.base import ListingScraper
from app.scrapers.db import canonical_address, canonical_city, normalize_parcel_number
from app.scrapers.models import RawListing
from app.scrapers.user_agents import random_ua

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SITE_NAME = "Wayne County Land Bank"

_API_URL = (
    "https://public-wclb.epropertyplus.com"
    "/landmgmtpub/remote/public/property/getPublishedProperties"
)
_PAGE_SIZE = 50           # rows per request (field-map uses 50)
_MAX_PAGES = 40           # safety ceiling — ~812 rows / 50 = ~17 pages
_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0
_INTER_PAGE_DELAY = 1.0   # 1 req/sec — polite for a county site

# The API encodes filter criteria as a JSON string in the `json` query param.
# Empty criterias returns the full published inventory.
_JSON_PARAM = '{"criterias":[]}'

# Lifecycle filter — only ingest rows explicitly marked available.
# INVARIANT: see module docstring.
_ACTIVE_AVAILABLE = "Y"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def _assert_json_response(data: Any, page: int) -> None:
    """Guard against the SPA HTML trap — see module-level INVARIANT.

    The WCLB ePropertyPlus endpoint returns HTTP 200 with full React SPA HTML
    when params are missing or malformed. Verify the decoded response is a dict
    carrying 'rows' before trusting it.

    Raises ValueError with a clear message if the response is not the expected
    JSON shape, so the caller logs it and stops pagination rather than
    processing HTML as row data.
    """
    if not isinstance(data, dict):
        raise ValueError(
            f"WCLB page {page}: expected JSON object, got {type(data).__name__}. "
            "Server may have returned SPA HTML — check query params."
        )
    if "rows" not in data:
        raise ValueError(
            f"WCLB page {page}: JSON response missing 'rows' key "
            f"(keys={list(data.keys())[:10]}). "
            "Server may have returned SPA HTML — check query params."
        )


async def _get_page(
    client: httpx.AsyncClient,
    page: int,
) -> dict[str, Any] | None:
    """Fetch one page from the WCLB ePropertyPlus API with retry on transient errors.

    Returns the parsed JSON dict, or None on permanent failure.
    """
    params = {"page": page, "limit": _PAGE_SIZE, "json": _JSON_PARAM}
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(_API_URL, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            _assert_json_response(data, page)
            return data
        except ValueError as exc:
            # SPA HTML trap or malformed JSON — not a transient error; stop immediately.
            logger.error("WCLB: %s", exc)
            return None
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error(
                    "WCLB: page %d failed after %d attempts: %s",
                    page, attempt, exc,
                )
                return None
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "WCLB: page %d attempt %d failed, retrying in %.1fs: %s",
                page, attempt, delay, exc,
            )
            await asyncio.sleep(delay)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Row → RawListing conversion
# ─────────────────────────────────────────────────────────────────────────────

def _parse_money(value: Any) -> float | None:
    """Convert a numeric or None API value to float. Returns None on falsy/zero input."""
    if value is None:
        return None
    try:
        result = float(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def _to_raw_listing(row: dict[str, Any]) -> RawListing | None:
    """Convert one WCLB ePropertyPlus row dict into a RawListing.

    Returns None when the available filter excludes the row or when
    no usable address is present.
    """
    # INVARIANT: only available=Y rows are live buy-now deals.
    if row.get("available") != _ACTIVE_AVAILABLE:
        return None

    raw_parcel = (row.get("parcelNumber") or "").strip()
    if not raw_parcel:
        logger.debug("WCLB: skipping row with missing parcelNumber: id=%r", row.get("id"))
        return None

    # WCLB parcels are 14-digit out-county packed form; normalize_parcel_number
    # returns them verbatim (the 14-char alphanumeric branch is a no-op).
    norm_parcel = normalize_parcel_number(raw_parcel)

    raw_addr = (row.get("propertyAddress1") or "").strip()
    if not raw_addr:
        logger.debug("WCLB: skipping parcel %s — no street address", norm_parcel)
        return None
    canon_addr = canonical_address(raw_addr)

    # city/state/postalCode arrive UPPERCASE from WCLB — canonical_city title-cases them.
    raw_city = (row.get("city") or "").strip()
    city: str | None = canonical_city(raw_city) if raw_city else None

    raw_zip = (row.get("postalCode") or "").strip() or None

    # payload: preserve source metadata fields for downstream querying
    payload_fields = {
        k: row.get(k)
        for k in ("propertyClass", "structureType", "bedrooms", "squareFootage", "legalDescription")
        if row.get(k) is not None
    }

    return RawListing(
        source_site=SITE_NAME,
        source_listing_id=norm_parcel,
        signal_type="land_bank_inventory",
        property_address=canon_addr,
        property_city=city,
        property_county="Wayne",
        property_state="MI",
        property_zip=raw_zip,
        opening_bid_usd=_parse_money(row.get("askingPrice")),
        status="active",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class WayneCountyLandBankScraper(ListingScraper):
    """Wayne County Land Bank (out-county) buy-now inventory via ePropertyPlus JSON API.

    Paginates the full published inventory (~812 rows, ~17 pages at 50/page)
    and emits only available=Y rows.

    WCLB covers Wayne County municipalities outside Detroit (Allen Park, Highland
    Park, Inkster, Ecorse, etc.) — distinct from DLBA (buildingdetroit.org).

    Staleness: FULL_RESCAN — orchestrator retires absent listings by time-not-seen.
    Pace: 1 req/sec between pages; realistic Chrome UA; no auth required.
    """

    site_name = SITE_NAME

    async def fetch_and_parse(self) -> list[RawListing]:
        headers = {
            "User-Agent": random_ua(),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://public-wclb.epropertyplus.com/",
        }

        listings: list[RawListing] = []
        total_rows_seen = 0
        total_size: int | None = None

        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            for page in range(1, _MAX_PAGES + 1):
                data = await _get_page(client, page)
                if data is None:
                    logger.error(
                        "WCLB: page %d fetch failed — stopping pagination "
                        "(partial results, %d listings collected so far)",
                        page, len(listings),
                    )
                    break

                if total_size is None:
                    total_size = int(data.get("size") or 0)
                    logger.info("WCLB: API reports %d total published rows", total_size)

                rows: list[dict[str, Any]] = data.get("rows") or []
                total_rows_seen += len(rows)

                for row in rows:
                    listing = _to_raw_listing(row)
                    if listing is not None:
                        listings.append(listing)

                logger.debug(
                    "WCLB: page %d — %d rows, %d available=Y so far, "
                    "%d/%s total rows consumed",
                    page, len(rows), len(listings), total_rows_seen,
                    total_size if total_size else "?",
                )

                # Stop when we've consumed the reported total or got a short page.
                if len(rows) < _PAGE_SIZE:
                    logger.info(
                        "WCLB: short page at %d (%d rows) — end of inventory",
                        page, len(rows),
                    )
                    break
                if total_size and total_rows_seen >= total_size:
                    logger.info(
                        "WCLB: consumed all %d reported rows after page %d",
                        total_size, page,
                    )
                    break

                await asyncio.sleep(_INTER_PAGE_DELAY)

            else:
                # Hit _MAX_PAGES ceiling without ending normally.
                logger.warning(
                    "WCLB: hit _MAX_PAGES ceiling (%d) — feed may be "
                    "undercounted (%d total rows seen, %d available=Y collected)",
                    _MAX_PAGES, total_rows_seen, len(listings),
                )

        logger.info(
            "WCLB: complete — %d total published rows, %d available=Y emitted",
            total_rows_seen, len(listings),
        )
        return listings


# ─────────────────────────────────────────────────────────────────────────────
# Dry-run entry point (no DB writes)
# ─────────────────────────────────────────────────────────────────────────────

async def _dry_run_sample(n: int = 12) -> None:
    """Fetch the live WCLB feed and print a sample. No DB writes."""
    logging.basicConfig(level=logging.INFO)

    print(f"\n=== Wayne County Land Bank — dry-run sample (first {n} available) ===\n")
    scraper = WayneCountyLandBankScraper()
    listings = await scraper.fetch_and_parse()

    total = len(listings)
    print(f"Total available=Y listings: {total}\n")

    for listing in listings[:n]:
        parcel = listing.source_listing_id or "?"
        addr = listing.property_address or "?"
        city = listing.property_city or "?"
        price = listing.opening_bid_usd
        print(
            f"  parcel={parcel:16s}  addr={addr:30s}  city={city:18s}  "
            f"price=${price:>8.0f}" if price else
            f"  parcel={parcel:16s}  addr={addr:30s}  city={city:18s}  price=None"
        )

    if listings:
        first = listings[0]
        parcel = first.source_listing_id or ""
        print(f"\nParcel digit-check: len({parcel!r}) = {len(parcel)}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_dry_run_sample(12))
