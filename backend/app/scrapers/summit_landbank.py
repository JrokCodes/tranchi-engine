"""
Summit County Land Bank scraper — Tolemi publiCity GraphQL backend.

INVARIANTS (read before editing):
  1. QUERY TOKEN: _QUERY_TOKEN is an opaque server-side-encrypted filter handle that
     represents the default "all publicly-visible assets" filter for the
     summit-county-oh publiCity tenant. It is NOT constructed by us — it is captured
     from the SPA's initial network call and hard-coded here. Re-validated on every run
     via fetchAssetCount: if the token has rotated the count call will return an error
     or 0, at which point the log message tells you to re-capture from the SPA's
     network inspector. Do NOT derive or mutate this token.

  2. PID IS THE JOIN KEY: `pid` from the API is a 7-digit zero-padded numeric string
     (e.g. '6700526'). It is the Summit County parcel spine join key. Leading zeros
     are load-bearing — never cast to int. normalize_parcel_number() is called on
     every pid; for clean 7-digit inputs it is a no-op (returns same string). The
     canonical form MUST match tranchi.parcels.parcel_number for the summit-akron
     market.

  3. FULL_RESCAN semantics: this API re-publishes the full live inventory on every
     call. A parcel absent from a given run has genuinely left the inventory (sold /
     transferred). The orchestrator's _mark_stale_listings() handles retirement by
     absence — this scraper does NOT touch the DB.
"""
from __future__ import annotations

import asyncio
import json
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

SITE_NAME = "Summit County Land Bank"

_GQL_URL = "https://cg.tolemi.com/q"

# Opaque encrypted filter token — see INVARIANT 1 in module docstring.
# Captured from the SPA's first fetchAssetCount/fetchAssetRecords call on 2026-06-11.
# Re-validated each run via fetchAssetCount; if this rotates, the count call will
# return an error or zero — see the WARNING log below for recovery instructions.
_QUERY_TOKEN = "Q0yH7mSQGr/EYTufMhfWJQ=="

# heatAttribute scopes the filter set — "filter1953" is Summit County Land Bank's
# default public filter. Pair with the query token; both must match.
_HEAT_ATTRIBUTE = "filter1953"

# Attributes we request per record. Omit `polygon` (WKB geometry) and `pl`
# (base64 PNG thumbnail) — both are heavy and irrelevant to the deal join.
_ATTRIBUTES = ["address", "street_address", "city", "state", "zip", "pid", "identity_owner"]

# Expected minimum asset count. If fetchAssetCount returns below this, log a
# warning — could indicate a token rotation or inventory anomaly.
_EXPECTED_MIN_COUNT = 200

# fetchAssetRecords page size (observed: 100 per page from live API, 2026-06-13).
# We stop when a page returns fewer rows than this.
_PAGE_SIZE = 100

# Safety ceiling to prevent infinite loops if the API changes pagination behavior.
_MAX_PAGES = 100

_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0
_INTER_PAGE_DELAY = 1.0  # 1 req/sec — polite for a county GraphQL endpoint


# ─────────────────────────────────────────────────────────────────────────────
# GraphQL operation bodies
# ─────────────────────────────────────────────────────────────────────────────

def _count_body() -> dict[str, Any]:
    return {
        "operationName": "fetchAssetCount",
        "variables": {
            "filters": {
                "heatAttribute": _HEAT_ATTRIBUTE,
                "query": _QUERY_TOKEN,
            }
        },
        "query": (
            "query fetchAssetCount($filters: JSON, $includeBounds: Boolean)"
            "{ assetCount(filters:$filters, includeBounds:$includeBounds) }"
        ),
    }


def _records_body(page: int) -> dict[str, Any]:
    return {
        "operationName": "fetchAssetRecords",
        "variables": {
            "params": {
                "heatAttribute": _HEAT_ATTRIBUTE,
                "query": _QUERY_TOKEN,
                "attributes": _ATTRIBUTES,
                "sort": None,
                "order": "asc",
                "page": page,
                "list": True,
            }
        },
        "query": (
            "query fetchAssetRecords($params: JSON, $savedViewId: ID)"
            "{ assetRecords(params:$params, savedViewId:$savedViewId) }"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tolemi_headers() -> dict[str, str]:
    """Headers required for Tolemi publiCity GraphQL — see field-map for auth story."""
    return {
        "content-type": "application/json",
        "city-alias": "summit-county-oh",
        "product": "publiCity",
        "apollo-require-preflight": "true",
        "User-Agent": random_ua(),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }


async def _post(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    label: str,
) -> dict[str, Any] | None:
    """POST a GraphQL operation with retry. Returns the parsed JSON or None."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.post(
                _GQL_URL,
                content=json.dumps(body),
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error(
                    "SummitLandBank: %s failed after %d attempts: %s",
                    label, attempt, exc,
                )
                return None
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "SummitLandBank: %s attempt %d failed, retrying in %.1fs: %s",
                label, attempt, delay, exc,
            )
            await asyncio.sleep(delay)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Address extraction helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_address(record: dict[str, Any]) -> str | None:
    """Extract the best available street address string from a Tolemi asset record.

    Priority:
      1. `street_address` — pre-parsed situs street (e.g. 'PRINCETON ST')
      2. `address.commonName` — JSON blob field with a `commonName` key

    Returns None if neither yields a non-empty string.
    """
    # street_address is a top-level string field
    street = (record.get("street_address") or "").strip()
    if street:
        return street

    # address is a JSON blob: '{"commonName":"PRINCETON ST","address":"PRINCETON ST, Akron, OH"}'
    raw_addr = record.get("address") or ""
    if isinstance(raw_addr, str) and raw_addr.strip():
        try:
            addr_obj = json.loads(raw_addr)
            common = (addr_obj.get("commonName") or "").strip()
            if common:
                return common
            # Fall back to the full address string inside the blob
            full = (addr_obj.get("address") or "").strip()
            if full:
                return full
        except (json.JSONDecodeError, AttributeError):
            # If the field isn't valid JSON, treat it as a plain string
            plain = raw_addr.strip()
            if plain:
                return plain

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Row → RawListing conversion
# ─────────────────────────────────────────────────────────────────────────────

def _to_raw_listing(record: dict[str, Any]) -> RawListing | None:
    """Convert one Tolemi asset record into a RawListing.

    Returns None if the record has no usable pid or no street address.
    """
    raw_pid = (record.get("pid") or "").strip()
    if not raw_pid:
        logger.debug(
            "SummitLandBank: skipping record id=%s — missing pid", record.get("id")
        )
        return None

    # INVARIANT 2: pid is the Summit 7-digit join key; normalize is a safety net.
    norm_pid = normalize_parcel_number(raw_pid)

    raw_street = _extract_address(record)
    if not raw_street:
        logger.debug(
            "SummitLandBank: skipping pid %s — no usable street address", norm_pid
        )
        return None

    canon_addr = canonical_address(raw_street)

    raw_city = (record.get("city") or "").strip()
    city = canonical_city(raw_city) if raw_city else None

    raw_zip = (record.get("zip") or "").strip()
    zip_code: str | None = raw_zip if raw_zip else None

    return RawListing(
        source_site=SITE_NAME,
        source_listing_id=norm_pid,
        signal_type="land_bank_inventory",
        property_address=canon_addr,
        property_city=city,
        property_county="Summit",
        property_state="OH",
        property_zip=zip_code,
        status="active",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class SummitLandBankScraper(ListingScraper):
    """Summit County Land Bank inventory via Tolemi publiCity GraphQL.

    Validates the opaque query token via fetchAssetCount on each run, then
    paginates fetchAssetRecords until a short/empty page signals end-of-inventory.
    ~489 assets as of 2026-06-11; turns slowly (re-scan weekly).

    Staleness: FULL_RESCAN — the orchestrator's _mark_stale_listings() retires
    parcels absent from this run. No DB writes in this class.

    Pool: NOT needed — httpx only, no DB access.
    """

    site_name = SITE_NAME

    async def fetch_and_parse(self) -> list[RawListing]:
        headers = _tolemi_headers()
        listings: list[RawListing] = []

        async with httpx.AsyncClient(headers=headers) as client:
            # ── Step 1: validate query token via fetchAssetCount ─────────────
            count_data = await _post(client, _count_body(), "fetchAssetCount")
            if count_data is None:
                logger.error(
                    "SummitLandBank: fetchAssetCount failed — aborting. "
                    "If the query token rotated, re-capture _QUERY_TOKEN from "
                    "the SPA network inspector at summit-county-oh-publicity.tolemi.com"
                )
                return []

            # Response shape: {"data": {"assetCount": {"assetCount": 489}}}
            asset_count: int = 0
            try:
                asset_count = int(
                    count_data["data"]["assetCount"]["assetCount"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.error(
                    "SummitLandBank: unexpected fetchAssetCount response shape: %s — "
                    "raw response: %r. Token may have rotated — re-capture from SPA.",
                    exc, count_data,
                )
                return []

            logger.info("SummitLandBank: fetchAssetCount returned %d assets", asset_count)

            if asset_count < _EXPECTED_MIN_COUNT:
                logger.warning(
                    "SummitLandBank: asset count %d is below expected minimum %d. "
                    "Possible token rotation or inventory anomaly — verify at "
                    "summit-county-oh-publicity.tolemi.com before trusting results.",
                    asset_count, _EXPECTED_MIN_COUNT,
                )

            # ── Step 2: paginate fetchAssetRecords ───────────────────────────
            total_records_seen = 0

            for page in range(_MAX_PAGES):
                records_data = await _post(
                    client, _records_body(page), f"fetchAssetRecords page={page}"
                )
                if records_data is None:
                    logger.error(
                        "SummitLandBank: fetchAssetRecords page %d failed — stopping "
                        "pagination (%d listings collected so far)",
                        page, len(listings),
                    )
                    break

                # Response shape: {"data": {"assetRecords": [...]}}
                try:
                    records: list[dict[str, Any]] = records_data["data"]["assetRecords"]
                except (KeyError, TypeError) as exc:
                    logger.error(
                        "SummitLandBank: unexpected fetchAssetRecords response shape "
                        "on page %d: %s — raw: %r",
                        page, exc, records_data,
                    )
                    break

                if not isinstance(records, list):
                    logger.error(
                        "SummitLandBank: assetRecords is not a list on page %d: %r",
                        page, type(records),
                    )
                    break

                page_count = len(records)
                total_records_seen += page_count

                for rec in records:
                    listing = _to_raw_listing(rec)
                    if listing is not None:
                        listings.append(listing)

                logger.debug(
                    "SummitLandBank: page %d — %d records, %d valid listings so far, "
                    "%d total records consumed",
                    page, page_count, len(listings), total_records_seen,
                )

                # End of inventory: short or empty page
                if page_count < _PAGE_SIZE:
                    logger.info(
                        "SummitLandBank: short page at %d (%d records) — end of inventory",
                        page, page_count,
                    )
                    break

                await asyncio.sleep(_INTER_PAGE_DELAY)

            else:
                logger.warning(
                    "SummitLandBank: hit _MAX_PAGES ceiling (%d) without a short page — "
                    "feed may be undercounted (%d listings collected)",
                    _MAX_PAGES, len(listings),
                )

        logger.info(
            "SummitLandBank: complete — %d total records seen, %d RawListings emitted",
            total_records_seen, len(listings),
        )
        return listings


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio as _asyncio
    import json as _json
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    _FORMAT_LOCK_PID = "6700526"  # PRINCETON ST, Akron — confirm if present

    async def _dry_run() -> None:
        headers = _tolemi_headers()

        async with httpx.AsyncClient(headers=headers) as client:
            # ── 1. Count ─────────────────────────────────────────────────────
            print("\n=== fetchAssetCount ===")
            count_resp = await _post(client, _count_body(), "fetchAssetCount")
            if count_resp is None:
                print("ERROR: fetchAssetCount failed — check token or network")
                return

            print(_json.dumps(count_resp, indent=2))

            try:
                asset_count = int(count_resp["data"]["assetCount"]["assetCount"])
                print(f"\nassetCount: {asset_count}")
            except (KeyError, TypeError, ValueError) as exc:
                print(f"ERROR: unexpected shape: {exc}")
                return

            # ── 2. First two pages of records ────────────────────────────────
            all_records: list[dict[str, Any]] = []
            for page in range(2):
                print(f"\n=== fetchAssetRecords page={page} ===")
                rec_resp = await _post(
                    client, _records_body(page), f"fetchAssetRecords page={page}"
                )
                if rec_resp is None:
                    print(f"ERROR: page {page} failed")
                    break

                try:
                    records: list[dict[str, Any]] = rec_resp["data"]["assetRecords"]
                except (KeyError, TypeError) as exc:
                    print(f"ERROR: bad shape on page {page}: {exc}")
                    break

                print(f"  records on this page: {len(records)}")
                all_records.extend(records)

                if len(records) < _PAGE_SIZE:
                    print("  (short page — end of inventory reached early)")
                    break

                if page < 1:
                    await asyncio.sleep(1.0)

            # ── 3. Convert to RawListing + print samples ──────────────────────
            print(f"\n=== Conversion ({len(all_records)} raw records) ===")
            listings: list[RawListing] = []
            for rec in all_records:
                listing = _to_raw_listing(rec)
                if listing is not None:
                    listings.append(listing)

            print(f"Valid RawListings from first 2 pages: {len(listings)}")

            print("\n--- Sample rows (first 5) ---")
            for lst in listings[:5]:
                print(
                    f"  pid={lst.source_listing_id!r:12s}  "
                    f"addr={lst.property_address!r:35s}  "
                    f"city={lst.property_city!r}  zip={lst.property_zip!r}"
                )

            # ── 4. Format-lock anchor: pid 6700526 ────────────────────────────
            print(f"\n--- Format-lock anchor: pid {_FORMAT_LOCK_PID!r} ---")
            anchor_found = any(
                normalize_parcel_number(rec.get("pid", "")) == _FORMAT_LOCK_PID
                for rec in all_records
            )
            if anchor_found:
                print(f"  FOUND in first 2 pages: {_FORMAT_LOCK_PID}")
            else:
                print(
                    f"  NOT in first 2 pages (may be on a later page — "
                    f"run full scrape to confirm)"
                )

            # ── 5. Summary ───────────────────────────────────────────────────
            print("\n=== Summary ===")
            print(f"  query token used : {_QUERY_TOKEN!r}")
            print(f"  assetCount       : {asset_count}")
            print(f"  token validated  : {'YES' if asset_count >= _EXPECTED_MIN_COUNT else 'WARN — count below expected'}")
            print(f"  listings emitted : {len(listings)} (from {len(all_records)} raw records, pages 0-1)")
            print(f"  site_name        : {SITE_NAME!r}")
            print(f"  signal_type      : 'land_bank_inventory'")
            print(f"  DB pool needed   : NO")

    _asyncio.run(_dry_run())
