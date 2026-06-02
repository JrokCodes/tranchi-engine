"""
Shelby County (TN) mortgage / trustee-sale foreclosure scraper — multi-source.

Two free, public readers, merged in-Python by parcel (or street+zip) so one
property is one row regardless of how many feeds carry it:

  Reader A — tnforeclosurenotices.com/results/counties/shelby/
    Free, no-captcha, server-rendered HTML table (~24 Shelby rows). The
    substitute-trustee affidavit/notice vendor used by Mackie Wolf, Western
    Progressive, etc. Authoritative for LEGAL VALIDITY + POSTPONEMENT: each row
    carries the stable TNFN# id, Original Sale Date, the *PP (postponed/pending)
    Sale Date*, the trustee Firm, sale location/time, auction vendor, and a
    full-text Notice-of-Sale PDF.

  Reader B — graph.auction.com/graphql  (resiSearch_blueprint_seekListingsFromFilters)
    Free, no-auth GraphQL (~89 results within radius 50 of Shelby; ~51 are
    actually TN/Shelby — the query's nearby_search pulls in Crittenden AR /
    DeSoto MS etc., so results MUST be filtered to state==TN & county==Shelby).
    Richest for VOLUME + bid: full address parts, auction start/end, starting
    bid, and the event.trustee_sale flag (True = pre-sale foreclosure auction,
    False = bank-owned/REO). Both stages are ingested per Marc ("pull volume,
    filter on the Tranchi side"), distinguished via auction_status.

INVARIANT (read before editing):
  - SITE_NAME ("Shelby County Foreclosure") is the stored source_site and the
    key the staleness map + dashboard key on. It MUST be present in
    staleness.py SOURCE_STALENESS (policy = FULL_RESCAN: both feeds return their
    full current set each run, so retire-by-absence is correct).
  - sale_date = PP Sale Date if present else Original Sale Date (Reader A) /
    auction.start_date (Reader B). PP date IS the postponement-tracking signal:
    when a sale is adjourned, tnforeclosurenotices updates PP Sale Date — TN law
    (TCA 35-5-101) lets a sale be postponed at the courthouse WITHOUT newspaper
    re-publication, so the affidavit feed is the only reliable postponement source.
    A past sale_date with no future PP date is retired by _mark_expired_listings.
  - TN mortgage foreclosures are FINAL at sale (redemption is near-universally
    waived in the deed of trust, TCA 66-8-101/103) — NO redemption lifecycle
    here. The redeemable path is TAX sales only (Shelby County Tax Sale source).
  - Parcel join is PRECISION-FIRST: match the source street address to a spine
    tranchi.parcels.situs_address on (house-number + zip + normalized street).
    City is NOT used (Cordova/Bartlett/Memphis are labelled inconsistently across
    sources for the same zip). No/ambiguous match -> source_listing_id stays NULL
    (cross-source dedup falls back to normalized_address) and the row is flagged
    REVIEW via the standard stub-parcel/address_status path. NEVER invent a parcel.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import date, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

try:  # asyncpg only needed for the type hint / pool; scraper degrades without it
    import asyncpg  # noqa: F401
except Exception:  # pragma: no cover
    asyncpg = None  # type: ignore

from app.scrapers.base import ListingScraper
from app.scrapers.db import (
    canonical_address,
    normalize_address,
    normalize_parcel_number,
)
from app.scrapers.models import RawListing
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

SITE_NAME = "Shelby County Foreclosure"
SIGNAL_TYPE = "mortgage_foreclosure"  # maps to "Foreclosure (Mortgage)" in _DIM_MAP

# ── Reader A: tnforeclosurenotices.com ──────────────────────────────────────
_TNFN_URL = "https://tnforeclosurenotices.com/results/counties/shelby/"

# ── Reader B: auction.com GraphQL ───────────────────────────────────────────
_ACOM_URL = "https://graph.auction.com/graphql"
# Shelby County centroid (lat,lon) — the site geocodes "Shelby County, tn" to this.
_ACOM_GEO = "35.1268552,-89.9253233"
_ACOM_QUERY = (
    "query S($filters: ListingCompatabilityFilters!) { "
    "seek_listings_from_filters(filters: $filters) { total_count total_pages content { "
    "... on Listing { listing_id listing_status listing_status_group "
    "formatted_address(format: DOUBLE_LINE) event { trustee_sale } "
    "seller_property { street_description municipality country_primary_subdivision "
    "country_secondary_subdivision postal_code } "
    "auction { start_date end_date starting_bid } } } } }"
)

_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_HOUSE_RE = re.compile(r"^\s*(\d+)\b")
# trailing ", TN 38109" / " TN 38109" / ", TENNESSEE 38109"
_STATE_ZIP_TAIL = re.compile(r",?\s+(?:TN|TENNESSEE|MS|AR)\s+\d{5}(?:-\d{4})?\s*$", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# Date parsing
# ─────────────────────────────────────────────────────────────────────────────

def _parse_iso_date(raw: str | None) -> date | None:
    """'2026-06-25T02:52:39+00:00' (data-sort attr) -> date; None on failure."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_human_date(raw: str | None) -> date | None:
    """'Thu 25, Jun 2026' -> date; None on failure (data-sort is preferred)."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%a %d, %b %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Address parsing helpers (shared by both readers + the parcel join)
# ─────────────────────────────────────────────────────────────────────────────

def _street_zip(raw_addr: str) -> tuple[str | None, str | None]:
    """Return (street_only, zip5) from a full address string.

    street_only strips the trailing ', CITY, ST ZIP' so the join key is
    geography-independent (city labels are inconsistent across sources). zip5 is
    the trailing 5-digit code. Returns (None, None) for unusable input.
    """
    if not raw_addr:
        return None, None
    a = re.sub(r"\s+", " ", raw_addr).strip()
    zip_m = _ZIP_RE.search(a)
    zip5 = zip_m.group(1) if zip_m else None
    # cut everything from a trailing ", CITY, ST ZIP" — first drop state+zip tail,
    # then drop a trailing ", City" segment if a comma remains.
    a = _STATE_ZIP_TAIL.sub("", a).strip().rstrip(",")
    if "," in a:
        a = a.split(",")[0].strip()
    street = canonical_address(a)
    return (street or None), zip5


def _join_key(street: str | None, zip5: str | None) -> str | None:
    """Normalized (street + zip) join key, or None if street is missing."""
    if not street:
        return None
    return f"{normalize_address(street)}|{zip5 or ''}"


# ─────────────────────────────────────────────────────────────────────────────
# Reader A — tnforeclosurenotices.com (httpx + BeautifulSoup)
# ─────────────────────────────────────────────────────────────────────────────

async def _get(client: httpx.AsyncClient, url: str, *, accept: str = "text/html,*/*") -> str | None:
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url, timeout=_TIMEOUT, headers={"Accept": accept})
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error("ShelbyForeclosure: GET %s failed after %d attempts: %s", url, attempt, exc)
                return None
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return None


def _parse_tnfn(html: str) -> list[dict[str, Any]]:
    """Parse the tnforeclosurenotices Shelby results table into raw row dicts.

    Column order (verified live 2026-06-02):
      0 pdf-col (TNFN# anchor -> NOS pdf) · 1 County · 2 Original Sale Date
      (data-sort ISO) · 3 address-col · 4 Firm · 5 PP Sale Date (data-sort ISO,
      may be blank) · 6 Sale Location · 7 Sale Time · 8 Auction Vendor
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        logger.warning("ShelbyForeclosure: Reader A — no <table> found")
        return []
    tbody = table.find("tbody") or table
    rows: list[dict[str, Any]] = []

    def _cell_text(td) -> str:
        # drop the mobile-title <span> label, keep the value text
        for span in td.find_all("span", class_="mobile-title"):
            span.extract()
        return td.get_text(" ", strip=True)

    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        anchor = tds[0].find("a")
        tnfn = (anchor.get_text(strip=True) if anchor else _cell_text(tds[0])).strip()
        pdf_url = anchor.get("href") if anchor else None
        if not tnfn or "TNFN" not in tnfn.upper():
            continue

        orig_date = _parse_iso_date(tds[2].get("data-sort")) or _parse_human_date(_cell_text(tds[2]))
        address = _cell_text(tds[3])
        firm = _cell_text(tds[4]) if len(tds) > 4 else None
        pp_date = None
        if len(tds) > 5:
            pp_date = _parse_iso_date(tds[5].get("data-sort")) or _parse_human_date(_cell_text(tds[5]))
        location = _cell_text(tds[6]) if len(tds) > 6 else None
        sale_time = _cell_text(tds[7]) if len(tds) > 7 else None
        vendor = _cell_text(tds[8]) if len(tds) > 8 else None

        rows.append({
            "tnfn": tnfn,
            "pdf_url": pdf_url,
            "sale_date": pp_date or orig_date,  # PP date wins (postponement)
            "address": address,
            "firm": firm or None,
            "location": location or None,
            "sale_time": sale_time or None,
            "vendor": vendor or None,
            "trustee_sale": True,  # tnforeclosurenotices = substitute-trustee sale notices
        })
    logger.info("ShelbyForeclosure: Reader A parsed %d rows", len(rows))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Reader B — auction.com GraphQL (httpx POST)
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_auction_com(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    payload = {
        "query": _ACOM_QUERY,
        "variables": {
            "filters": {
                "property_state": "TN",
                "property_county": "Shelby",
                "geo_location": _ACOM_GEO,
                "listing_type": "active",
                "sort": "auction_date_order",
                "limit": 500,
                "nearby_search": "y",
                "nearby_search_radius": 50,
                "version": 1,
                "offset": 0,
            }
        },
    }
    headers = {
        "content-type": "application/json",
        "accept": "application/json",
        "auction-graph-source": "auctioncom",
        "x-cid": str(uuid.uuid4()),
        "referer": "https://www.auction.com/",
    }
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.post(_ACOM_URL, json=payload, headers=headers, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                logger.error("ShelbyForeclosure: Reader B GraphQL errors: %s", str(data["errors"])[:300])
                return []
            seek = (data.get("data") or {}).get("seek_listings_from_filters") or {}
            content = seek.get("content") or []
            break
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error("ShelbyForeclosure: Reader B failed after %d attempts: %s", attempt, exc)
                return []
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    else:
        return []

    rows: list[dict[str, Any]] = []
    skipped_geo = 0
    for c in content:
        sp = c.get("seller_property") or {}
        state = (sp.get("country_primary_subdivision") or "").strip().upper()
        county = (sp.get("country_secondary_subdivision") or "").strip().upper()
        # nearby_search radius 50 leaks adjacent TN counties + AR/MS — keep only TN/Shelby
        if state != "TN" or county != "SHELBY":
            skipped_geo += 1
            continue
        street = (sp.get("street_description") or "").strip()
        zip5 = (sp.get("postal_code") or "").strip() or None
        if not street:
            continue
        full_addr = f"{street}, {sp.get('municipality') or 'Memphis'}, TN {zip5 or ''}".strip()
        auc = c.get("auction") or {}
        start = _parse_iso_date(auc.get("start_date"))
        trustee = bool((c.get("event") or {}).get("trustee_sale"))
        bid = auc.get("starting_bid")
        rows.append({
            "address": full_addr,
            "street_only": street,
            "zip5": zip5,
            "sale_date": start,
            "opening_bid_usd": float(bid) if isinstance(bid, (int, float)) else None,
            "trustee_sale": trustee,
            "vendor": "Auction.com",
            "listing_id": c.get("listing_id"),
        })
    logger.info("ShelbyForeclosure: Reader B parsed %d TN/Shelby rows (skipped %d out-of-area)",
                len(rows), skipped_geo)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Parcel resolution against the spine (precision-first: house# + zip + street)
# ─────────────────────────────────────────────────────────────────────────────

async def _resolve_parcels(
    pool: "asyncpg.Pool | None",
    keys: list[tuple[str, str | None]],  # (street_only, zip5) per unique address
) -> dict[str, str]:
    """Return {join_key: normalized_parcel} for confidently-matched addresses.

    Matches each (street, zip) to a tranchi.parcels.situs_address by house-number
    + zip ILIKE candidate fetch, then exact normalized-street comparison. Only a
    UNIQUE street match is accepted (ambiguous -> omitted -> listing stays NULL).
    """
    out: dict[str, str] = {}
    if pool is None or not keys:
        return out

    # Build distinct (house_no, zip) ILIKE patterns to fetch candidates in one query.
    patterns: set[str] = set()
    meta: list[tuple[str, str | None, str | None]] = []  # (jkey, street, zip)
    for street, zip5 in keys:
        jkey = _join_key(street, zip5)
        if not jkey or jkey in {m[0] for m in meta}:
            continue
        house_m = _HOUSE_RE.match(street or "")
        house = house_m.group(1) if house_m else None
        meta.append((jkey, street, zip5))
        if house and zip5:
            patterns.add(f"{house} %{zip5}%")
        elif house:
            patterns.add(f"{house} %")
    if not patterns:
        return out

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT parcel_number, situs_address
                FROM tranchi.parcels
                WHERE situs_address ILIKE ANY($1::text[])
                  AND situs_address IS NOT NULL AND situs_address <> ''
                """,
                list(patterns),
            )
    except Exception as exc:  # pragma: no cover
        logger.warning("ShelbyForeclosure: parcel resolution query failed: %s", exc)
        return out

    # Index spine candidates by join_key; track ambiguity (multiple parcels per key).
    spine: dict[str, set[str]] = {}
    for r in rows:
        s_street, s_zip = _street_zip(r["situs_address"])
        skey = _join_key(s_street, s_zip)
        if not skey:
            continue
        norm = normalize_parcel_number(r["parcel_number"])
        if norm:
            spine.setdefault(skey, set()).add(norm)

    matched = ambiguous = 0
    for jkey, _street, _zip in meta:
        cands = spine.get(jkey)
        if cands and len(cands) == 1:
            out[jkey] = next(iter(cands))
            matched += 1
        elif cands:
            ambiguous += 1  # >1 parcel at same street+zip — leave NULL, flag REVIEW
    logger.info(
        "ShelbyForeclosure: parcel resolution — %d/%d matched (%d ambiguous, %d unresolved)",
        matched, len(meta), ambiguous, len(meta) - matched - ambiguous,
    )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class ShelbyForeclosureScraper(ListingScraper):
    """Shelby County (TN) mortgage / trustee-sale foreclosure — multi-source.

    Readers A (tnforeclosurenotices.com) + B (auction.com GraphQL) are merged by
    parcel (or street+zip) so one property is one RawListing. Needs `pool` for
    address->parcel resolution against tranchi.parcels; degrades gracefully (all
    rows NULL parcel -> REVIEW) without it.

    Staleness: FULL_RESCAN. signal_type = "mortgage_foreclosure".
    """

    site_name = SITE_NAME

    def __init__(self, pool: "asyncpg.Pool | None" = None, dry_run: bool = False) -> None:
        self.pool = pool
        self.dry_run = dry_run

    async def fetch_and_parse(self) -> list[RawListing]:
        headers = default_headers()
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            tnfn_html = await _get(client, _TNFN_URL)
            reader_a = _parse_tnfn(tnfn_html) if tnfn_html else []
            reader_b = await _fetch_auction_com(client)

        if not reader_a and not reader_b:
            logger.warning("ShelbyForeclosure: both readers empty — returning []")
            return []

        # Attach (street, zip) + join_key to every row.
        for row in reader_a:
            row["street_only"], row["zip5"] = _street_zip(row["address"])
        for row in reader_b:
            # Reader B already has street_only/zip5; canonicalize street for the key.
            row["street_only"] = canonical_address(row["street_only"]) or row["street_only"]
        for row in reader_a + reader_b:
            row["jkey"] = _join_key(row.get("street_only"), row.get("zip5"))

        # Resolve parcels for all distinct addresses.
        uniq_keys = list({(r["street_only"], r["zip5"]) for r in reader_a + reader_b if r.get("street_only")})
        parcel_by_jkey = await _resolve_parcels(self.pool, uniq_keys)

        # Merge readers: prefer Reader A (legal validity / postponement) for shared
        # fields, take bid from Reader B. Group by parcel if resolved, else join_key.
        def group_key(row: dict[str, Any]) -> str:
            jkey = row.get("jkey")
            parcel = parcel_by_jkey.get(jkey) if jkey else None
            return f"parcel:{parcel}" if parcel else (f"addr:{jkey}" if jkey else f"raw:{id(row)}")

        merged: dict[str, dict[str, Any]] = {}
        for row in reader_a + reader_b:  # A first so it wins on conflicts
            gk = group_key(row)
            if gk not in merged:
                merged[gk] = dict(row)
            else:
                existing = merged[gk]
                # fill bid / sale_date / fields only if missing on the A-preferred row
                for f in ("opening_bid_usd", "sale_date", "vendor", "location", "sale_time", "pdf_url"):
                    if not existing.get(f) and row.get(f):
                        existing[f] = row[f]
                existing["trustee_sale"] = existing.get("trustee_sale") or row.get("trustee_sale")

        listings: list[RawListing] = []
        for gk, row in merged.items():
            jkey = row.get("jkey")
            parcel = parcel_by_jkey.get(jkey) if jkey else None
            address = canonical_address(row.get("street_only") or row.get("address") or "") or row.get("address")
            if not address:
                continue
            trustee = bool(row.get("trustee_sale"))
            listings.append(RawListing(
                source_site=SITE_NAME,
                source_listing_id=parcel,                 # NULL if unresolved/ambiguous
                case_number=row.get("tnfn") or None,      # stable TNFN# (Reader A)
                signal_type=SIGNAL_TYPE,
                property_address=address,
                property_city=None,                       # derive from parcel registry on read
                property_county="Shelby",
                property_state="TN",                      # override RawListing default "OH"
                property_zip=row.get("zip5"),
                sale_date=row.get("sale_date"),
                sale_time=row.get("sale_time"),
                sale_location=row.get("location"),
                trustee_name=row.get("firm"),
                opening_bid_usd=row.get("opening_bid_usd"),
                status="active",
                # pre-sale trustee auction vs bank-owned/REO (both ingested)
                auction_status="scheduled" if trustee else "bank_owned",
            ))

        logger.info(
            "ShelbyForeclosure: returning %d merged listings (A=%d, B=%d, parcels=%d)",
            len(listings), len(reader_a), len(reader_b), len(parcel_by_jkey),
        )
        return listings
