"""
Shelby County (TN) Land Bank scraper — ePropertyPlus JSON REST API.

INVARIANT: ePropertyPlus returns the FULL inventory including sold, pending, and
redeemed records. Only rows where currentStatus == "FOR SALE" AND available == "Y"
are live deals. Everything else (SALE COMPLETE, SALE PENDING, Bid Off Pending,
IN EVALUATION, NOT FOR SALE, REDEEMED / RESCINDED, or available == "N") is
explicitly KILLED at parse time. Do NOT relax this filter — the bulk of the 12,899
rows are historical and will pollute the active deal set if admitted.

parcelNumber arrives as a 14-char alphanumeric string (e.g. '07204700000160')
already in the canonical Shelby form. normalize_parcel_number() is called on
every row as a safety net against format drift; for 14-char clean inputs it is
a no-op (returns the same string uppercased).

Staleness policy: FULL_RESCAN — the API re-publishes the full inventory on every
call, so a listing absent from a given run is genuinely gone (sold/pulled/redeemed).
The orchestrator's _mark_stale_listings() handles retirement by absence.

Source: https://public-sctn.epropertyplus.com/landmgmtpub/remote/public/property/
        getPublishedProperties?page=1&limit=200&json={"criterias":[]}
Pace:   1 req/sec, page size 200 (~65 pages for 12,899 total inventory rows).
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

SITE_NAME = "Shelby County Land Bank"

_API_URL = (
    "https://public-sctn.epropertyplus.com"
    "/landmgmtpub/remote/public/property/getPublishedProperties"
)
_PAGE_SIZE = 200          # rows per request
_MAX_PAGES = 150          # safety ceiling — full inventory is ~65 pages (12,899 rows / 200)
_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0
_INTER_PAGE_DELAY = 1.0  # 1 req/sec — polite for a county site

# The API encodes filter criteria as a JSON string in the `json` query param.
# Empty criterias returns the full published inventory.
_JSON_PARAM = '{"criterias":[]}'

# Status values that represent a live, purchasable listing.
# INVARIANT: see module docstring — only these two flags combined admit a row.
_ACTIVE_STATUS = "FOR SALE"
_ACTIVE_AVAILABLE = "Y"

# City values from the source that should be stored as None (not real cities).
_NULL_CITY_VALUES: frozenset[str] = frozenset({"TBD", "tbd", ""})


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _get_page(
    client: httpx.AsyncClient,
    page: int,
) -> dict[str, Any] | None:
    """Fetch one page from the ePropertyPlus API with retry on transient errors.

    Returns the parsed JSON dict, or None on permanent failure.
    """
    params = {"page": page, "limit": _PAGE_SIZE, "json": _JSON_PARAM}
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(_API_URL, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error(
                    "ShelbyLandBank: page %d failed after %d attempts: %s",
                    page, attempt, exc,
                )
                return None
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "ShelbyLandBank: page %d attempt %d failed, retrying in %.1fs: %s",
                page, attempt, delay, exc,
            )
            await asyncio.sleep(delay)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Row → RawListing conversion
# ─────────────────────────────────────────────────────────────────────────────

def _parse_money(value: Any) -> float | None:
    """Convert a numeric or None API value to float. Returns None on falsy input."""
    if value is None:
        return None
    try:
        result = float(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def _to_raw_listing(row: dict[str, Any]) -> RawListing | None:
    """Convert one ePropertyPlus row dict into a RawListing.

    Returns None when the KILL filter applies (see module docstring INVARIANT)
    or when no usable address is present.
    """
    # ── KILL filter — INVARIANT (see module docstring) ────────────────────────
    if row.get("currentStatus") != _ACTIVE_STATUS or row.get("available") != _ACTIVE_AVAILABLE:
        return None

    raw_parcel = row.get("parcelNumber") or ""
    if not raw_parcel:
        logger.debug("ShelbyLandBank: skipping row with missing parcelNumber: %r", row.get("id"))
        return None

    # parcelNumber is already 14-char canonical; normalize_parcel_number is a
    # safety net for any future format drift (no-op for clean 14-char inputs).
    norm_parcel = normalize_parcel_number(raw_parcel)

    # Address: propertyAddress1 is always present; propertyAddress2 is not in
    # this source's schema (confirmed via live probe — field absent on all rows).
    raw_addr = (row.get("propertyAddress1") or "").strip()
    if not raw_addr:
        logger.debug(
            "ShelbyLandBank: skipping parcel %s — no street address", norm_parcel
        )
        return None
    canon_addr = canonical_address(raw_addr)

    # City: filter sentinel values ("TBD", empty) to None.
    raw_city = (row.get("city") or "").strip()
    city: str | None = None
    if raw_city and raw_city.upper() not in _NULL_CITY_VALUES:
        city = canonical_city(raw_city)

    return RawListing(
        source_site=SITE_NAME,
        source_listing_id=norm_parcel,
        signal_type="land_bank_inventory",
        property_address=canon_addr,
        property_city=city,
        property_county="Shelby",
        property_state="TN",
        property_zip=(row.get("postalCode") or "").strip() or None,
        opening_bid_usd=_parse_money(row.get("askingPrice")),
        appraised_value_usd=_parse_money(row.get("currentAssessment")),
        status="active",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class ShelbyCountyLandBankScraper(ListingScraper):
    """Shelby County (TN) Land Bank inventory via ePropertyPlus JSON API.

    Paginates the full published inventory (~12,900 rows, ~65 pages at 200/page)
    and emits only FOR SALE + available=Y rows (~2,100 as of June 2026).
    All other statuses (SALE COMPLETE, SALE PENDING, etc.) are KILLed at parse.

    Staleness: FULL_RESCAN — orchestrator retires absent listings by time-not-seen.
    Pace: 1 req/sec between pages; realistic Chrome UA; no auth required.
    """

    site_name = SITE_NAME

    async def fetch_and_parse(self) -> list[RawListing]:
        headers = {
            "User-Agent": random_ua(),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://public-sctn.epropertyplus.com/",
        }

        listings: list[RawListing] = []
        total_rows_seen = 0
        total_size: int | None = None

        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            for page in range(1, _MAX_PAGES + 1):
                data = await _get_page(client, page)
                if data is None:
                    logger.error(
                        "ShelbyLandBank: page %d fetch failed — stopping pagination "
                        "(partial results, %d listings collected so far)",
                        page, len(listings),
                    )
                    break

                if total_size is None:
                    total_size = int(data.get("size") or 0)
                    logger.info(
                        "ShelbyLandBank: API reports %d total inventory rows", total_size
                    )

                rows: list[dict[str, Any]] = data.get("rows") or []
                total_rows_seen += len(rows)

                for row in rows:
                    listing = _to_raw_listing(row)
                    if listing is not None:
                        listings.append(listing)

                logger.debug(
                    "ShelbyLandBank: page %d — %d rows, %d for-sale so far, "
                    "%d/%s total rows consumed",
                    page, len(rows), len(listings), total_rows_seen,
                    total_size if total_size else "?",
                )

                # Stop when we've consumed the reported total or got a short page.
                if len(rows) < _PAGE_SIZE:
                    logger.info(
                        "ShelbyLandBank: short page at %d (%d rows) — end of inventory",
                        page, len(rows),
                    )
                    break
                if total_size and total_rows_seen >= total_size:
                    logger.info(
                        "ShelbyLandBank: consumed all %d reported rows after page %d",
                        total_size, page,
                    )
                    break

                await asyncio.sleep(_INTER_PAGE_DELAY)

            else:
                # Hit _MAX_PAGES ceiling without ending normally.
                logger.warning(
                    "ShelbyLandBank: hit _MAX_PAGES ceiling (%d) — feed may be "
                    "undercounted (%d total rows seen, %d for-sale collected)",
                    _MAX_PAGES, total_rows_seen, len(listings),
                )

        logger.info(
            "ShelbyLandBank: complete — %d total inventory rows, %d FOR SALE + available=Y emitted",
            total_rows_seen, len(listings),
        )
        return listings
