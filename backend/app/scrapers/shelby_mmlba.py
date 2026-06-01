"""
Memphis MMLBA (City of Memphis Metropolitan Land Bank Authority) scraper.

INVARIANT: This is the CITY land bank — distinct from the Shelby County Land Bank.
  Their inventories are mostly non-overlapping. Both target Memphis parcels but
  MMLBA is city-operated; the county land bank is a separate entity.

MECHANISM: The property gallery on mmlba.org/property-sales/ is an Airtable embed
  (app appg6GMQ0yySEBiHN, share shrlCjIYLPGvJT66s). Airtable's API requires a
  session-scoped accessPolicy token (generated dynamically, not in the static HTML).
  Plain httpx cannot obtain it; Playwright loads the page, intercepts the XHR to
  /v0.3/application/.../readForSharedPages, and decodes the msgpack response.

PARCEL NUMBERS: Shelby spaced format (e.g. '013054 00016') normalized to the
  14-char canonical form via normalize_parcel_number() from db.py.

STALENESS: FULL_RESCAN — the Airtable view returns only "Available" properties
  (the view filter excludes Sold/Under Contract/Closed). Any property absent from
  the current run was sold or removed.

INVENTORY SIZE: Small. As of 2026-06-01, 10 properties were listed.
  This is MMLBA's entire active-for-sale inventory, not a partial page.
  Add a count warning if it drops below 3 (likely a structural change, not empty).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.scrapers.base import ListingScraper
from app.scrapers.db import canonical_address, canonical_city, normalize_parcel_number
from app.scrapers.models import RawListing

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_SITE_NAME = "Memphis MMLBA"
_GALLERY_URL = "https://mmlba.org/property-sales/"
_EMBED_URL = "https://airtable.com/embed/appg6GMQ0yySEBiHN/shrlCjIYLPGvJT66s"

# Airtable field IDs (stable across runs — schema only changes if MMLBA edits their base)
_FIELD_ADDRESS = "fldnOym7heWlTcHL8"   # "Full Property Address" — text, e.g. "431 Lucy  Memphis TN 38106"
_FIELD_PARCEL = "fld5AbEjCxslPAKGQ"    # "Parcel No." — text, Shelby spaced format
_FIELD_PRICE = "fldc8AejroTZNfTM8"     # "Disposition Price" — number (USD)
_FIELD_STATUS = "fldQzEMaWdVhVc8BY"    # "Property Status " — select

# Status select IDs (verified against live Airtable msgpack stream 2026-06-01).
# selu2MROKI7JD84Bu appears as the bare status string for all 10 currently-available
# properties. The schema section of the stream confirms: label = "Avaliable" (sic).
# ALLOWLIST: only emit a record when its status select-id == _STATUS_AVAILABLE.
# Any other select-id (unknown, sold, under-contract, redacted) is skipped with a
# warning so schema drift is caught at runtime rather than silently passed through.
_STATUS_AVAILABLE = "selu2MROKI7JD84Bu"  # "Avaliable" (sic — that is the label in Airtable)

# Playwright timing
_PAGE_LOAD_TIMEOUT_MS = 30_000
_XHR_WAIT_MS = 8_000        # wait after networkidle for XHR to complete


# ─────────────────────────────────────────────────────────────────────────────
# msgpack decoder
# ─────────────────────────────────────────────────────────────────────────────

def _decode_msgpack_stream(body: bytes) -> list[Any]:
    """Decode an Airtable msgpack stream into a flat list of Python objects.

    Airtable returns multi-value msgpack streams (not a single root object).
    msgpack.Unpacker handles the multi-object boundary; each decoded object
    is appended to the list. 5000+ items is normal for the MMLBA gallery page.
    """
    try:
        import msgpack  # soft dependency — only needed at scrape time
    except ImportError as exc:
        raise RuntimeError(
            "msgpack not installed. Run: pip install msgpack"
        ) from exc

    unpacker = msgpack.Unpacker(raw=False, strict_map_key=False)
    unpacker.feed(body)
    return list(unpacker)


# ─────────────────────────────────────────────────────────────────────────────
# Record parser
# ─────────────────────────────────────────────────────────────────────────────

_SHELBY_ZIP_RE = re.compile(r'\b(3[78]\d{3})\b')
_CITY_STATE_ZIP_RE = re.compile(r'^(.*?)\s+(?:Memphis|TN)\s+TN\s+\d{5}', re.IGNORECASE)
_ADDR_CITY_SPLIT_RE = re.compile(r'^(.*?)\s+Memphis\s+TN\b', re.IGNORECASE)


def _parse_address_fields(raw_full: str) -> tuple[str, str | None, str | None]:
    """Split a full-address string into (street_address, city, zip).

    MMLBA stores addresses like "431 Lucy  Memphis TN 38106". We split at the
    city name and return the street portion separately. Extra internal spaces
    are collapsed.
    """
    raw_full = re.sub(r'\s+', ' ', raw_full).strip()

    zipcode: str | None = None
    m_zip = _SHELBY_ZIP_RE.search(raw_full)
    if m_zip:
        zipcode = m_zip.group(1)

    # Split at "Memphis TN" (case-insensitive); everything before is the street address
    m_split = _ADDR_CITY_SPLIT_RE.match(raw_full)
    if m_split:
        street = m_split.group(1).strip()
        city = "Memphis"
    else:
        # Fallback: strip trailing " TN XXXXX" and assume Memphis
        street = re.sub(r'\s+TN\s+\d{5}.*$', '', raw_full, flags=re.IGNORECASE).strip()
        city = None

    return street, city, zipcode


_TIMESTAMP_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')
_REC_ID_RE = re.compile(r'^rec[A-Za-z0-9]{14}$')


def _is_field_list(item: Any) -> bool:
    """Return True if item is an Airtable field-ID list (all elements start with 'fld')."""
    return (
        isinstance(item, list)
        and len(item) >= 2
        and all(isinstance(x, str) and x.startswith("fld") for x in item)
    )


def _is_fixext_ref(item: Any) -> bool:
    """Return True if item is an Airtable msgpack fixext schema reference.

    Airtable uses msgpack ExtType(code=114, data=<byte>) as back-references to a
    previously defined field list. When decoded by msgpack.Unpacker with raw=False,
    they arrive as msgpack.ExtType objects. These markers always precede either a new
    field list (full record) or directly the field values (delta record, reusing the
    last field list).
    """
    try:
        import msgpack as _mp
        return isinstance(item, _mp.ExtType) and item.code == 114
    except ImportError:
        return False


_SEL_ID_RE = re.compile(r'^sel[A-Za-z0-9]{14}$')

# Maximum stream items to scan forward from a record start when hunting for
# the status field. The attachment block on image-heavy records is ~45 items;
# 120 is a conservative upper bound that won't bleed into the next record.
_STATUS_SCAN_WINDOW = 120


def _parse_records(stream: list[Any]) -> list[dict[str, Any]]:
    """Extract property records from the flat Airtable msgpack stream.

    Airtable encodes row data using a positional / delta scheme:

    FULL record (first time a field set appears):
        rec_id, createdTime, ExtType(114), [fld1, fld2, ...], val1, val2, ...

    DELTA record (reuses the most recently seen field list — same field set):
        rec_id, createdTime, <int_ref>, val1, val2, ...
        OR
        rec_id, createdTime, ExtType(114), val1, val2, ...  (fixext without new list)

    We detect standalone records by the Airtable record-ID pattern (rec + 14 alphanum)
    followed by a timestamp string at the next position.

    Field extraction strategy:
      - Address (fldnOym7heWlTcHL8) and Parcel (fld5AbEjCxslPAKGQ) are always the first
        two scalar string values after the schema header — positional extraction is safe.
      - Price (fldc8AejroTZNfTM8) is always the third value — also positional-safe.
      - Status (fldQzEMaWdVhVc8BY) follows the multipleAttachment image field, which
        expands into a variable number of stream items (thumbnail URLs, dimensions, etc.).
        Positional extraction for status is UNRELIABLE. Instead we scan forward from the
        record start for the first bare sel... string within _STATUS_SCAN_WINDOW items.
        All 10 live records confirmed to carry status as a bare string (not in a list).

    Returns a list of raw record dicts. Each dict has at least 'rec_id' and 'raw_address'.
    Missing parcel/price/status are None.
    """
    records: list[dict[str, Any]] = []
    seen_rec_ids: set[str] = set()
    last_field_list: list[str] | None = None

    # Pre-compute standalone record positions so we know each record's upper boundary
    # for the status scan (stop before the next record starts).
    standalone_positions: list[int] = []
    for idx, v in enumerate(stream):
        if (isinstance(v, str) and _REC_ID_RE.match(v)
                and idx + 1 < len(stream)
                and isinstance(stream[idx + 1], str)
                and _TIMESTAMP_RE.match(stream[idx + 1])):
            standalone_positions.append(idx)

    next_rec: dict[int, int] = {}
    for k in range(len(standalone_positions) - 1):
        next_rec[standalone_positions[k]] = standalone_positions[k + 1]

    i = 0
    while i < len(stream):
        item = stream[i]

        # Update last_field_list whenever we encounter one (schema header)
        if _is_field_list(item):
            last_field_list = item
            i += 1
            continue

        # Detect a standalone record: rec_id followed by a timestamp
        if (isinstance(item, str) and _REC_ID_RE.match(item)
                and i + 1 < len(stream)
                and isinstance(stream[i + 1], str)
                and _TIMESTAMP_RE.match(stream[i + 1])):

            rec_id = item
            if rec_id in seen_rec_ids:
                i += 1
                continue
            seen_rec_ids.add(rec_id)

            # Advance past rec_id and timestamp
            # Next item is EITHER:
            #   (a) ExtType(114) then a NEW field list  -> full record
            #   (b) ExtType(114) without a new list     -> delta record (reuse last_field_list)
            #   (c) an int schema_ref                   -> delta record (reuse last_field_list)
            offset = i + 2
            schema_item = stream[offset] if offset < len(stream) else None

            if _is_fixext_ref(schema_item):
                after_fixext = stream[offset + 1] if offset + 1 < len(stream) else None
                if _is_field_list(after_fixext):
                    last_field_list = after_fixext
                    values_start = offset + 2
                else:
                    values_start = offset + 1
            elif isinstance(schema_item, int):
                values_start = offset + 1
            else:
                logger.debug(
                    "MMLBA: unexpected schema marker at [%d] after rec %s: %s",
                    offset, rec_id, schema_item,
                )
                values_start = offset

            if last_field_list is None:
                logger.debug("MMLBA: no field list known yet at rec %s — skipping", rec_id)
                i += 1
                continue

            # ── Positional extraction: address, parcel, price ─────────────────
            # These three fields (indices 0-2 in the field list) are always simple
            # scalar values unaffected by the attachment expansion.
            fid_to_pos = {fid: pos for pos, fid in enumerate(last_field_list)}
            addr_pos  = fid_to_pos.get(_FIELD_ADDRESS)
            parcel_pos = fid_to_pos.get(_FIELD_PARCEL)
            price_pos  = fid_to_pos.get(_FIELD_PRICE)

            def _get_scalar(field_pos: int | None) -> Any:
                if field_pos is None:
                    return None
                stream_idx = values_start + field_pos
                if stream_idx >= len(stream):
                    return None
                v = stream[stream_idx]
                # Attachment blocks start with an ExtType or a list containing ExtType;
                # a scalar field is a plain str, int, or float.
                return v if isinstance(v, (str, int, float)) else None

            raw_address = _get_scalar(addr_pos) or ""
            raw_parcel  = _get_scalar(parcel_pos)
            raw_price   = _get_scalar(price_pos)

            if not raw_address:
                logger.debug("MMLBA: rec %s has no address — skipping", rec_id)
                i += 1
                continue

            # ── Scan-based extraction: status select-id ───────────────────────
            # The Property Status field follows the multipleAttachment image field,
            # which expands into a variable-length block of thumbnail URLs and
            # dimension integers. Positional zip is misaligned from the image field
            # onward. Instead, scan forward for the first bare sel... string.
            # Upper boundary: start of next record, capped by _STATUS_SCAN_WINDOW.
            rec_upper = next_rec.get(i, i + _STATUS_SCAN_WINDOW)
            scan_end = min(rec_upper, i + _STATUS_SCAN_WINDOW, len(stream))

            raw_status: str | None = None
            for j in range(values_start, scan_end):
                v = stream[j]
                if isinstance(v, str) and _SEL_ID_RE.match(v):
                    raw_status = v
                    break

            records.append({
                "rec_id": rec_id,
                "raw_address": raw_address,
                "raw_parcel": raw_parcel,
                "raw_price": raw_price,
                "raw_status": raw_status,
            })
            i += 1
            continue

        i += 1

    return records


def _to_raw_listing(record: dict[str, Any]) -> RawListing | None:
    """Convert a parsed MMLBA record dict to a RawListing.

    Returns None when the status select-id is not _STATUS_AVAILABLE (allowlist),
    or when the address cannot be parsed.

    Allowlist rationale: the Airtable view is already server-filtered to "Available"
    properties, so all returned records should carry _STATUS_AVAILABLE. Checking
    explicitly here guards against the view filter being relaxed or a new status
    option being added — unknown select-ids are logged as warnings so schema drift
    is caught at runtime rather than leaking non-available properties through.
    """
    raw_status = record.get("raw_status")
    if raw_status != _STATUS_AVAILABLE:
        if raw_status is None:
            logger.warning(
                "MMLBA: rec %s has no status select-id — skipping. "
                "Possible schema change in Airtable embed.",
                record["rec_id"],
            )
        else:
            logger.warning(
                "MMLBA: rec %s has unexpected status id %r (expected %r) — skipping. "
                "Schema drift or view filter change.",
                record["rec_id"], raw_status, _STATUS_AVAILABLE,
            )
        return None

    raw_address = record["raw_address"]
    street, city, zipcode = _parse_address_fields(raw_address)
    if not street:
        logger.warning("MMLBA: rec %s has unparseable address %r", record["rec_id"], raw_address)
        return None

    canon_addr = canonical_address(street)
    if not canon_addr:
        logger.warning("MMLBA: rec %s canonical_address returned None for %r", record["rec_id"], street)
        return None

    canon_city = canonical_city(city or "Memphis")

    raw_parcel = record.get("raw_parcel")
    norm_parcel: str | None = None
    if raw_parcel and isinstance(raw_parcel, str) and raw_parcel.strip():
        norm_parcel = normalize_parcel_number(raw_parcel.strip())

    price: float | None = None
    raw_price = record.get("raw_price")
    if raw_price is not None:
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            pass

    return RawListing(
        source_site=_SITE_NAME,
        property_address=canon_addr,
        property_city=canon_city,
        property_county="Shelby",
        property_state="TN",
        property_zip=zipcode,
        signal_type="land_bank_inventory",
        source_listing_id=norm_parcel,
        # No case_number — MMLBA has no judicial case. Dedup falls back to
        # (source_site, property_address, sale_date) where sale_date is NULL.
        # If parcel IS available (norm_parcel), db.py deduplicates on
        # (source_site, case_number=None, source_listing_id=norm_parcel) — see _upsert_one.
        case_number=None,
        opening_bid_usd=price,
        status="active",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class MemphisMMLBAScraper(ListingScraper):
    """Scraper for the Memphis MMLBA (City land bank) property gallery.

    Mechanism: Playwright XHR interception of the Airtable embed on
    mmlba.org/property-sales/. The gallery is hosted in an Airtable shared view
    (appg6GMQ0yySEBiHN / shrlCjIYLPGvJT66s). Airtable returns data as a msgpack
    stream via /v0.3/application/.../readForSharedPages, decoded here with msgpack.

    The view is pre-filtered to "Available" properties only. FULL_RESCAN is correct:
    every run fetches the entire active inventory.

    Staleness: FULL_RESCAN (register in staleness.py).
    Inventory size: ~10 properties as of 2026-06-01 (small but real leads).
    """

    site_name = _SITE_NAME

    async def fetch_and_parse(self) -> list[RawListing]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("MMLBA: playwright not installed — cannot scrape Airtable embed")
            return []

        raw_body: bytes | None = None

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 900},
                )
                page = await ctx.new_page()

                async def on_response(resp: Any) -> None:
                    nonlocal raw_body
                    if "readForSharedPages" in resp.url:
                        try:
                            body = await resp.body()
                            # Keep the largest capture — Airtable sometimes fires the XHR
                            # twice (prefetch + actual render); the second response is the
                            # complete payload. The outer guard no longer short-circuits on
                            # raw_body is None so this comparison always executes.
                            if raw_body is None or len(body) > len(raw_body):
                                raw_body = body
                        except Exception as exc:
                            logger.debug("MMLBA: failed to read XHR body: %s", exc)

                page.on("response", on_response)

                logger.info("MMLBA: loading gallery page %s", _GALLERY_URL)
                await page.goto(
                    _GALLERY_URL,
                    wait_until="networkidle",
                    timeout=_PAGE_LOAD_TIMEOUT_MS,
                )
                # Wait for the Airtable iframe XHR to complete
                await asyncio.sleep(_XHR_WAIT_MS / 1000)

                await browser.close()

        except Exception as exc:
            logger.error("MMLBA: Playwright session failed: %s", exc)
            return []

        if not raw_body:
            logger.error(
                "MMLBA: no readForSharedPages XHR captured. "
                "Airtable embed may have changed or page load timed out."
            )
            return []

        logger.info("MMLBA: captured XHR body (%d bytes), decoding msgpack", len(raw_body))

        try:
            stream = _decode_msgpack_stream(raw_body)
        except Exception as exc:
            logger.error("MMLBA: msgpack decode failed: %s", exc)
            return []

        logger.info("MMLBA: decoded %d msgpack objects", len(stream))

        raw_records = _parse_records(stream)
        logger.info("MMLBA: parsed %d raw records", len(raw_records))

        if len(raw_records) < 3:
            logger.warning(
                "MMLBA: only %d records extracted — expected ≥3. "
                "Possible schema change in Airtable embed. Verify field IDs.",
                len(raw_records),
            )

        listings: list[RawListing] = []
        for record in raw_records:
            listing = _to_raw_listing(record)
            if listing is not None:
                listings.append(listing)

        logger.info("MMLBA: returning %d RawListings", len(listings))
        return listings
