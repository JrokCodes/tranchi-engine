"""
Wayne County (MI / Detroit) mortgage-foreclosure scraper — multi-source.

Two free, public readers, merged in-Python by parcel (or street+zip) so one property
is one row regardless of how many feeds carry it:

  Reader A — mipublicnotices.com/api/v1/search/search  (MI Press Assn / Column platform)
    The verbatim "Notice of Foreclosure by Advertisement" that runs in Detroit Legal News —
    the LEGAL ORGAN OF RECORD that gates validity (MCL 600.3201+, foreclosure-by-advertisement
    is NON-judicial: 4 weekly publications, no court docket). Free JSON, no auth/captcha. The
    structured envelope is thin — the gold is the `content` free-text, parsed here.

  Reader B — graph.auction.com/graphql  (same endpoint already in prod for Shelby Reader B)
    Free, no-auth GraphQL. Richest for VOLUME + bid. nearby_search radius 50 leaks adjacent
    MI counties + OH Lucas — results MUST be filtered to state==MI & county==WAYNE.

INVARIANT (read before editing):
  - SITE_NAME ("Wayne County Foreclosure") is the stored source_site, shared by BOTH readers
    (cross-source dedup collapses them by parcel/normalized_address). It is wired into
    market_config 'wayne' source_sites + staleness_policies (FULL_RESCAN) + source_meta.
  - area=82 is the NUMERIC county alias for Wayne on mipublicnotices. `area=Wayne` returns 0
    SILENTLY — this is the #1 trap. The noticeType UUID (_MIPN_MORTGAGE_TYPE) is the Mortgage
    Foreclosure discriminator; do NOT use the fuzzy `for` full-text param as the filter.
  - MI REDEMPTION (MCL 600.3240): a sheriff-deed is NOT final at sale — residential owners
    redeem for 6 MONTHS post-sale. So a PAST sale_date is NOT stale here: we surface BOTH
    pre-sale notices (sale_date >= today) AND post-sale in-redemption rows (today-180d <=
    sale_date < today), tagged via auction_status ('scheduled' | 'in_redemption' | 'bank_owned').
    run.py:_mark_expired_listings has a MARKET-SCOPED carve-out that keeps wayne
    mortgage_foreclosure rows alive for 180 days past sale_date; run.py:_compute_mi_redemption
    stamps redemption_window_days/redemption_ends/redemption_basis for display. (We default ALL
    residential foreclosures to the 6-month window; the 1-month abandoned case, MCL 600.3241a,
    is not reliably detectable from the notice feed — Jayden 2026-06-16.)
  - LEXICAL-SORT / SCAN-COMPLETE GUARD: never early-stop on the feed's own order. Paginate the
    FULL `from`-bounded set, filter sale_date client-side, and assert scan_complete (the DLN
    lesson — a feed's date sort can be lexical, so an early-stop would silently drop leads).
  - PARCEL JOIN is PRECISION-FIRST + MARKET-SCOPED: match the notice street address to a
    tranchi.parcels.situs_address (market='wayne') on house-number + zip + normalized street.
    No/ambiguous match -> source_listing_id NULL -> REVIEW. NEVER invent a parcel.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

import httpx

try:  # asyncpg only needed for the pool type hint; scraper degrades without it
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

SITE_NAME = "Wayne County Foreclosure"
SIGNAL_TYPE = "mortgage_foreclosure"  # → "Foreclosure (Mortgage)" in _DIM_MAP

# MI residential redemption window (MCL 600.3240). Mirrors the run.py carve-out constant.
MI_REDEMPTION_DAYS = 180
# How far back to pull notices: a notice is published ~4 weeks before the sale, and we want
# any sale within the 180-day redemption window → look back ~8 months of publications, then
# filter sale_date client-side.
_LOOKBACK_DAYS = 240

# ── Reader A: mipublicnotices ────────────────────────────────────────────────
_MIPN_URL = "https://www.mipublicnotices.com/api/v1/search/search"
_MIPN_MORTGAGE_TYPE = "4170ee87-9201-46e1-9b93-d2ba9fb27e89"  # noticeType = Mortgage Foreclosure
_MIPN_WAYNE_AREA = "82"                                       # numeric county alias (NOT 'Wayne')
_MIPN_PAGE_SIZE = 10                                          # API fixed 10/page

# ── Reader B: auction.com GraphQL ────────────────────────────────────────────
_ACOM_URL = "https://graph.auction.com/graphql"
_ACOM_GEO = "42.2791,-83.2803"  # Wayne County centroid
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
_STATE_ZIP_TAIL = re.compile(r",?\s+(?:MI|MICHIGAN)\s+\d{5}(?:-\d{4})?\s*$", re.IGNORECASE)

# ── content free-text extractors (Reader A) ──────────────────────────────────
_RE_ADDR = re.compile(r"[Cc]ommonly known as[:\s]+(.+?)(?:\.\s|;|\sThe redemption|\sTogether|$)", re.S)
_RE_SALE = re.compile(
    r"\bon\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", re.S)  # "...on July 16, 2026"
_RE_AMOUNT = re.compile(r"\$([0-9][0-9,]*\.?\d{0,2})")
_RE_MORTGAGOR = re.compile(r"[Mm]ortgagor\(?s?\)?[:\s]+(.+?)(?:[\.;]|\sand\b|$)", re.S)
_RE_ABANDONED = re.compile(r"abandoned", re.IGNORECASE)


def _parse_human_date(raw: str | None) -> date | None:
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_iso_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Address helpers (shared by both readers + the parcel join)
# ─────────────────────────────────────────────────────────────────────────────

def _street_zip(raw_addr: str | None) -> tuple[str | None, str | None]:
    if not raw_addr:
        return None, None
    a = re.sub(r"\s+", " ", raw_addr).strip()
    # Zip search MUST skip the leading house number: a Detroit address parsed from a notice
    # is often just '16878 Ashton Ave' (no zip), and a bare \d{5} search would grab the
    # 5-digit HOUSE NUMBER as the zip (the latent Cuyahoga zip-parse bug). Search only the
    # span AFTER the leading house number.
    house_m = _HOUSE_RE.match(a)
    zip_region = a[house_m.end():] if house_m else a
    zip_m = _ZIP_RE.search(zip_region)
    zip5 = zip_m.group(1) if zip_m else None
    a = _STATE_ZIP_TAIL.sub("", a).strip().rstrip(",")
    if "," in a:
        a = a.split(",")[0].strip()
    street = canonical_address(a)
    return (street or None), zip5


def _join_key(street: str | None, zip5: str | None) -> str | None:
    if not street:
        return None
    return f"{normalize_address(street)}|{zip5 or ''}"


# Street-type suffixes + directionals to normalize away for the spine join. The Detroit
# ASSESSOR situs omits the street suffix ('712 CASS', '16878 ASHTON') while foreclosure
# notices include it ('16878 Ashton Ave') — an exact street match aligns on ~5% of rows.
# The loose key (house# + street, suffix dropped, directionals → single letter, no zip)
# lifts unique parcel-resolution to ~45% (validated 2026-06-17: 38→340 of 754, 4/4 correct).
# Used ONLY for the spine parcel join — NEVER for normalized_address storage/dedup.
_STREET_SUFFIX = {
    "ST", "STREET", "AVE", "AVENUE", "RD", "ROAD", "DR", "DRIVE", "BLVD", "BOULEVARD",
    "CT", "COURT", "LN", "LANE", "PL", "PLACE", "CIR", "CIRCLE", "TER", "TERRACE",
    "PKWY", "PARKWAY", "WAY", "HWY", "HIGHWAY", "SQ", "SQUARE", "PT", "POINT",
}
_DIRECTIONAL = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}


def _loose_street_key(street: str | None) -> str | None:
    """house# + normalized street with the street-type suffix dropped + directionals
    standardized — to match the suffix-less Detroit assessor situs. Returns None if empty.
    Unique-match-only at the call site, so collisions (e.g. '100 Main St' vs '100 Main Ave')
    are left UNRESOLVED rather than mis-joined."""
    if not street:
        return None
    toks = [t for t in re.sub(r"[^A-Za-z0-9 ]", " ", street.upper()).split() if t]
    if not toks:
        return None
    toks = [_DIRECTIONAL.get(t, t) for t in toks]
    if len(toks) > 2 and toks[-1] in _STREET_SUFFIX:  # keep >2 so '12 ST' (a named st) survives
        toks = toks[:-1]
    return " ".join(toks)


# ─────────────────────────────────────────────────────────────────────────────
# Reader A — mipublicnotices (httpx JSON, full pagination + scan-complete guard)
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_mipublicnotices(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Pull all Wayne (area=82) Mortgage-Foreclosure notices published in the lookback
    window, parse the `content` free-text. FULL pagination + scan_complete guard."""
    today = datetime.now().date()
    from_date = (today - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    base_params = {
        "type": _MIPN_MORTGAGE_TYPE,
        "area": _MIPN_WAYNE_AREA,
        "from": from_date,
    }

    async def _page(page: int) -> dict[str, Any] | None:
        params = dict(base_params, page=str(page))
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                resp = await client.get(_MIPN_URL, params=params, timeout=_TIMEOUT,
                                        headers={"accept": "application/json"})
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
                if attempt == _RETRY_ATTEMPTS:
                    logger.error("WayneForeclosure: Reader A page %d failed: %s", page, exc)
                    return None
                await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
        return None

    first = await _page(1)
    if not first:
        return []
    total = int(first.get("total") or 0)
    hits = list(first.get("hits") or [])
    pages = max(1, -(-total // _MIPN_PAGE_SIZE))  # ceil
    # Paginate the FULL set — never early-stop (the lexical-sort lesson).
    for p in range(2, pages + 1):
        data = await _page(p)
        if data:
            hits.extend(data.get("hits") or [])
    scan_complete = len(hits) >= total
    if not scan_complete:
        logger.error("WayneForeclosure: Reader A SCAN INCOMPLETE — got %d of %d notices "
                     "(a future early-stop/regression would silently drop leads)", len(hits), total)

    rows: list[dict[str, Any]] = []
    for h in hits:
        notice = h.get("notice") or h
        content = notice.get("content") or ""
        if not content:
            continue
        addr_m = _RE_ADDR.search(content)
        address = re.sub(r"\s+", " ", addr_m.group(1)).strip() if addr_m else None
        if not address:
            continue
        sale_m = _RE_SALE.search(content)
        sale_date = _parse_human_date(sale_m.group(1)) if sale_m else None
        amount = None
        amt_m = _RE_AMOUNT.search(content)
        if amt_m:
            try:
                amount = float(amt_m.group(1).replace(",", ""))
            except ValueError:
                amount = None
        mort_m = _RE_MORTGAGOR.search(content)
        mortgagor = re.sub(r"\s+", " ", mort_m.group(1)).strip()[:200] if mort_m else None
        street, zip5 = _street_zip(address)
        rows.append({
            "address": address,
            "street_only": street,
            "zip5": zip5,
            "sale_date": sale_date,
            "amount_owed_usd": amount,
            "mortgagor": mortgagor,
            "abandoned": bool(_RE_ABANDONED.search(content)),
            "notice_id": notice.get("id") or h.get("id"),
            "vendor": "Detroit Legal News",
        })
    logger.info("WayneForeclosure: Reader A parsed %d notices (%d hits, total=%d, scan_complete=%s)",
                len(rows), len(hits), total, scan_complete)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Reader B — auction.com GraphQL (httpx POST), filtered to MI/Wayne
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_auction_com(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    payload = {
        "query": _ACOM_QUERY,
        "variables": {"filters": {
            "property_state": "MI", "property_county": "Wayne",
            "geo_location": _ACOM_GEO, "listing_type": "active",
            "sort": "auction_date_order", "limit": 500, "nearby_search": "y",
            "nearby_search_radius": 50, "version": 1, "offset": 0,
        }},
    }
    headers = {
        "content-type": "application/json", "accept": "application/json",
        "auction-graph-source": "auctioncom", "x-cid": str(uuid.uuid4()),
        "referer": "https://www.auction.com/",
    }
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.post(_ACOM_URL, json=payload, headers=headers, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                logger.error("WayneForeclosure: Reader B GraphQL errors: %s", str(data["errors"])[:300])
                return []
            content = ((data.get("data") or {}).get("seek_listings_from_filters") or {}).get("content") or []
            break
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error("WayneForeclosure: Reader B failed after %d attempts: %s", attempt, exc)
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
        if state != "MI" or county != "WAYNE":  # radius leaks Macomb/Oakland/Washtenaw + OH Lucas
            skipped_geo += 1
            continue
        street = (sp.get("street_description") or "").strip()
        if not street:
            continue
        zip5 = (sp.get("postal_code") or "").strip() or None
        full_addr = f"{street}, {sp.get('municipality') or 'Detroit'}, MI {zip5 or ''}".strip()
        auc = c.get("auction") or {}
        bid = auc.get("starting_bid")
        rows.append({
            "address": full_addr,
            "street_only": canonical_address(street) or street,
            "zip5": zip5,
            "sale_date": _parse_iso_date(auc.get("start_date")),
            "opening_bid_usd": float(bid) if isinstance(bid, (int, float)) else None,
            "trustee_sale": bool((c.get("event") or {}).get("trustee_sale")),
            "vendor": "Auction.com",
            "listing_id": c.get("listing_id"),
        })
    logger.info("WayneForeclosure: Reader B parsed %d MI/Wayne rows (skipped %d out-of-area)",
                len(rows), skipped_geo)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Parcel resolution against the spine — PRECISION-FIRST + MARKET-SCOPED (wayne)
# ─────────────────────────────────────────────────────────────────────────────

async def _resolve_parcels(
    pool: "asyncpg.Pool | None",
    streets: list[str],
) -> dict[str, str]:
    """{loose_street_key: normalized_parcel} for UNIQUELY-matched addresses (market='wayne').

    Bulk-loads the whole wayne spine situs ONCE into an in-memory loose-key index, then
    matches the notice streets against it — far faster than the old per-address
    ILIKE ANY(...) × 377K seq-scan (~6.5 min → seconds) AND suffix-insensitive so it
    actually matches the suffix-less Detroit assessor situs. Only UNIQUE loose-key matches
    are accepted (a key with >1 candidate parcel is left unresolved → REVIEW, never mis-joined).
    """
    out: dict[str, str] = {}
    if pool is None or not streets:
        return out
    wanted = {lk for lk in (_loose_street_key(s) for s in streets) if lk}
    if not wanted:
        return out
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT parcel_number, situs_address
                FROM tranchi.parcels
                WHERE market = 'wayne'
                  AND situs_address IS NOT NULL AND situs_address <> ''
                """
            )
    except Exception as exc:  # pragma: no cover
        logger.warning("WayneForeclosure: parcel resolution query failed: %s", exc)
        return out
    spine: dict[str, set[str]] = {}
    for r in rows:
        s_street, _s_zip = _street_zip(r["situs_address"])
        skey = _loose_street_key(s_street)
        if not skey or skey not in wanted:
            continue
        norm = normalize_parcel_number(r["parcel_number"])
        if norm:
            spine.setdefault(skey, set()).add(norm)
    matched = ambiguous = 0
    for lk in wanted:
        cands = spine.get(lk)
        if cands and len(cands) == 1:
            out[lk] = next(iter(cands))
            matched += 1
        elif cands:
            ambiguous += 1
    logger.info("WayneForeclosure: parcel resolution — %d/%d unique loose-key matches (%d ambiguous), spine rows scanned=%d",
                matched, len(wanted), ambiguous, len(rows))
    return out


def _auction_status(sale_date: date | None, trustee_sale: bool | None) -> str:
    """Lifecycle tag. Pre-sale future date = 'scheduled'; past date within the 180-day
    MI redemption window = 'in_redemption'; auction.com REO (trustee_sale False) = 'bank_owned'."""
    if trustee_sale is False:
        return "bank_owned"
    today = datetime.now().date()
    if sale_date and sale_date < today:
        return "in_redemption"
    return "scheduled"


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class WayneForeclosureScraper(ListingScraper):
    """Wayne County (MI) mortgage foreclosure — mipublicnotices (legal record) +
    auction.com (volume/bid), merged by parcel. Needs `pool` for the address→parcel
    join; degrades gracefully (NULL parcel → REVIEW) without it. FULL_RESCAN."""

    site_name = SITE_NAME

    def __init__(self, pool: "asyncpg.Pool | None" = None, dry_run: bool = False) -> None:
        self.pool = pool
        self.dry_run = dry_run

    async def fetch_and_parse(self) -> list[RawListing]:
        async with httpx.AsyncClient(headers=default_headers(), follow_redirects=True) as client:
            reader_a = await _fetch_mipublicnotices(client)
            reader_b = await _fetch_auction_com(client)
        if not reader_a and not reader_b:
            logger.warning("WayneForeclosure: both readers empty — returning []")
            return []

        today = datetime.now().date()
        cutoff = today - timedelta(days=MI_REDEMPTION_DAYS)
        # Keep pre-sale (>= today) AND in-redemption (within 180d). Drop sales older than the
        # redemption window. Rows with no parsed sale_date are kept (REVIEW; don't silently drop).
        def _keep(row: dict[str, Any]) -> bool:
            sd = row.get("sale_date")
            return sd is None or sd >= cutoff

        reader_a = [r for r in reader_a if _keep(r)]
        reader_b = [r for r in reader_b if _keep(r)]

        for row in reader_a + reader_b:
            if not row.get("street_only"):
                row["street_only"], row["zip5"] = _street_zip(row.get("address"))
            row["jkey"] = _join_key(row.get("street_only"), row.get("zip5"))
            row["lkey"] = _loose_street_key(row.get("street_only"))  # spine parcel-join key

        uniq_streets = [r["street_only"] for r in reader_a + reader_b if r.get("street_only")]
        parcel_by_lkey = await _resolve_parcels(self.pool, uniq_streets)

        def group_key(row: dict[str, Any]) -> str:
            lk = row.get("lkey")
            parcel = parcel_by_lkey.get(lk) if lk else None
            jkey = row.get("jkey")
            return f"parcel:{parcel}" if parcel else (f"addr:{jkey}" if jkey else f"raw:{id(row)}")

        # Reader A first (legal record) so it wins on shared fields; B fills bid.
        merged: dict[str, dict[str, Any]] = {}
        for row in reader_a + reader_b:
            gk = group_key(row)
            if gk not in merged:
                merged[gk] = dict(row)
            else:
                ex = merged[gk]
                for f in ("opening_bid_usd", "sale_date", "amount_owed_usd", "mortgagor", "vendor"):
                    if not ex.get(f) and row.get(f):
                        ex[f] = row[f]
                if "trustee_sale" in row:
                    ex["trustee_sale"] = ex.get("trustee_sale") if ex.get("trustee_sale") is not None else row.get("trustee_sale")

        listings: list[RawListing] = []
        for row in merged.values():
            lk = row.get("lkey")
            parcel = parcel_by_lkey.get(lk) if lk else None
            address = canonical_address(row.get("street_only") or row.get("address") or "") or row.get("address")
            if not address:
                continue
            listings.append(RawListing(
                source_site=SITE_NAME,
                source_listing_id=parcel,                 # NULL if unresolved/ambiguous → REVIEW
                signal_type=SIGNAL_TYPE,
                property_address=address,
                property_city=None,                       # derived from the spine on read
                property_county="Wayne",
                property_state="MI",                      # override RawListing default "OH"
                property_zip=row.get("zip5"),
                sale_date=row.get("sale_date"),
                opening_bid_usd=row.get("opening_bid_usd"),
                status="active",
                auction_status=_auction_status(row.get("sale_date"), row.get("trustee_sale")),
            ))
        logger.info("WayneForeclosure: returning %d merged listings (A=%d, B=%d, parcels=%d)",
                    len(listings), len(reader_a), len(reader_b), len(parcel_by_jkey))
        return listings


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run proof
# ─────────────────────────────────────────────────────────────────────────────

async def _dry_run() -> None:
    print("\n=== Wayne County Foreclosure — dry-run (no pool, no DB) ===\n")
    scraper = WayneForeclosureScraper(pool=None, dry_run=True)
    listings = await scraper.fetch_and_parse()
    print(f"\nReturned {len(listings)} listings\n")
    from collections import Counter
    by_status = Counter(l.auction_status for l in listings)
    print("auction_status distribution:", dict(by_status))
    for l in listings[:15]:
        print(f"  {l.auction_status:13s} sale={l.sale_date} bid={l.opening_bid_usd} "
              f"parcel={l.source_listing_id} addr={l.property_address!r} zip={l.property_zip}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_dry_run())
