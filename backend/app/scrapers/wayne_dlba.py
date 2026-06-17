"""
Detroit Land Bank Authority (DLBA) scraper — writes to tranchi.listings.

Two feeds are emitted as listings:
  (a) STRUCTURES from buildingdetroit.org (Feed 1): Auction / Own It Now / Rehabbed & Ready
      programs.  Price, auction dates, and program info are present; parcel_id is NOT in
      the list JSON so we join to Feed 2 (DLBA_For_Sale ArcGIS) by normalized address.
  (b) BUYABLE LOTS from Feed 3 (DLBA_Owned_Properties ArcGIS): only the publicly-buyable
      classes — Marketed Lot For Sale, Improved Lot For Sale, Oversized Lot For Sale,
      Accessory Structure Lot.  Parcel is present; no price field exists.

INVARIANT — SIDE-LOT API ZERO: buildingdetroit.org Browse returns 0 rows for category 9
(Side Lot).  Absence of lots on the site is NOT a removal event; lots live only in Feed 3.

INVARIANT — DETROIT TRAILING PERIOD: Detroit parcel_id carries a trailing period
('14001271.') or occasionally a '-N' suffix.  Pass VERBATIM through normalize_parcel_number
— the Wayne branch already handles this.  NEVER strip the trailing period; stripping causes
silent join failures against the assessor parcel registry.

INVARIANT — FEED 1 + FEED 2 SPLIT: DLBA_For_Sale (Feed 2) has parcel_id but no price.
buildingdetroit.org (Feed 1) has price but no parcel in list JSON.  Join them by normalized
address (street + city) to populate parcel_id on structure rows.  If no match, source_listing_id
stays NULL — the row still lists (precision-first; no parcel is not a fatal error).

INVARIANT — VACANT LAND SALES LAG: dlba_vacant_land_program_sales lags months (latest
2026-05-04 as of 2026-06-11). Never gate lot removal on that layer.

SKIPPED (deferred, no table yet): Neighborhood Lot For Sale (~30,221), Side Lot For Sale
(~1,435), IHOA Side Lot (~3) — adjacent-owner-only / deed-restricted programs.  Coverage
storage for these is a deferred follow-up (G1 decision).
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import date, datetime
from typing import Any

import httpx

from app.scrapers.arcgis_client import query_features
from app.scrapers.base import ListingScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawListing
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SITE_NAME = "Detroit Land Bank Authority"
_SIGNAL_TYPE = "land_bank_inventory"

# Feed 1 — buildingdetroit.org JSON browse
_BD_URL = "https://buildingdetroit.org/properties"
_BD_CATEGORIES = [2, 3, 8]   # 2=Auction, 3=Own It Now, 8=Rehabbed & Ready
_BD_PAGE_LIMIT = 50
_BD_TIMEOUT = 30.0
_BD_RETRY_ATTEMPTS = 3
_BD_RETRY_BACKOFF = 2.0

# Feed 2 — DLBA For Sale ArcGIS (parcel-join companion for structures)
_DLBA_FOR_SALE_URL = (
    "https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest"
    "/services/DLBA_For_Sale/FeatureServer/0"
)

# Feed 3 — DLBA Owned Properties (master inventory, lots)
_DLBA_OWNED_URL = (
    "https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest"
    "/services/DLBA_Owned_Properties/FeatureServer/0"
)
_ARCGIS_BATCH = 2000

# Lot programs emitted as listings (publicly buyable by anyone).
# The restricted classes (Neighborhood Lot, Side Lot, IHOA Side Lot) are SKIPPED
# per G1 decision — adjacent-owner-only/deed-restricted, no open-market use.
_BUYABLE_LOT_PROGRAMS: frozenset[str] = frozenset({
    "Marketed Lot For Sale",
    "Improved Lot For Sale",
    "Oversized Lot For Sale",
    "Accessory Structure Lot",
})

_RESTRICTED_LOT_PROGRAMS: frozenset[str] = frozenset({
    "Neighborhood Lot For Sale",
    "Side Lot For Sale",
    "IHOA Side Lot",
})

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean(v: Any) -> str | None:
    """Return stripped string or None."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _to_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_sale_date(raw: Any) -> date | None:
    """Parse '2026-06-12 09:00:00', '2026-06-12', or similar date strings."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in ("None", "0000-00-00", "0000-00-00 00:00:00"):
        return None
    # Try datetime first, then date-only
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    logger.debug("DLBA: could not parse date %r", raw)
    return None


def _normalize_address_key(address: str | None) -> str | None:
    """Reduce an address to a lowercase, whitespace-collapsed, punctuation-stripped key
    for fuzzy-enough matching between Feed 1 (full city) and Feed 2 (street only).

    Strips directional abbreviations at end (e.g. 'N', 'S'), city/state suffixes,
    and normalises unicode to ASCII so 'W Kirby' matches '3875 W Kirby'.
    Only the street portion (number + street name) is used as the key.
    """
    if not address:
        return None
    # Normalise unicode → ASCII
    s = unicodedata.normalize("NFKD", address).encode("ascii", "ignore").decode()
    s = s.lower()
    # Strip city/state suffixes like 'detroit mi 48221', 'detroit, mi'
    s = re.sub(r",?\s+detroit.*$", "", s)
    s = re.sub(r",?\s+mi\s+\d{5}.*$", "", s)
    # Remove punctuation except spaces
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ─────────────────────────────────────────────────────────────────────────────
# Feed 2 — DLBA_For_Sale ArcGIS (address → parcel_id lookup)
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_for_sale_parcel_map() -> dict[str, str]:
    """Return {normalized_address_key: raw_parcel_id} from the DLBA_For_Sale layer.

    Feed 2 carries parcel_id + address but no price.  Used to enrich Feed 1
    structure rows that lack a parcel in their list JSON.  Normalised address is
    the join key (both sides run through _normalize_address_key).
    """
    parcel_map: dict[str, str] = {}
    async for batch in query_features(
        _DLBA_FOR_SALE_URL,
        where="1=1",
        out_fields="parcel_id,address",
        batch_size=_ARCGIS_BATCH,
    ):
        for attrs in batch:
            raw_parcel = _clean(attrs.get("parcel_id"))
            raw_address = _clean(attrs.get("address"))
            if not raw_parcel or not raw_address:
                continue
            # Append Detroit so the key matches Feed 1's full address string
            key = _normalize_address_key(raw_address + " Detroit")
            if key:
                parcel_map[key] = raw_parcel
    logger.info("DLBA: Feed 2 loaded %d parcel-map entries", len(parcel_map))
    return parcel_map


# ─────────────────────────────────────────────────────────────────────────────
# Feed 1 — buildingdetroit.org structures
# ─────────────────────────────────────────────────────────────────────────────

async def _post_bd(
    client: httpx.AsyncClient,
    category: int,
    page: int,
) -> dict | None:
    """POST one page of the buildingdetroit.org property browse API."""
    data = {
        "location": "",
        "category": str(category),
        "bedrooms": "",
        "bathrooms": "",
        "district": "",
        "minsqft": "",
        "maxsqft": "",
        "limit": str(_BD_PAGE_LIMIT),
        "fromsaledate": "",
        "tosaledate": "",
        "page": str(page),
        "sortorder": "",
        "isJson": "1",
    }
    headers = {
        **default_headers(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://buildingdetroit.org/properties",
        "X-Requested-With": "XMLHttpRequest",
    }
    for attempt in range(1, _BD_RETRY_ATTEMPTS + 1):
        try:
            resp = await client.post(_BD_URL, data=data, headers=headers, timeout=_BD_TIMEOUT)
            resp.raise_for_status()
            body = resp.json()
            # buildingdetroit.org returns 200 even on errors — check for data key
            if not isinstance(body, dict):
                raise ValueError(f"unexpected response type: {type(body)}")
            return body
        except Exception as exc:
            if attempt == _BD_RETRY_ATTEMPTS:
                logger.error(
                    "DLBA: POST buildingdetroit.org cat=%d page=%d failed after %d attempts: %s",
                    category, page, attempt, exc,
                )
                return None
            delay = _BD_RETRY_BACKOFF * (2 ** (attempt - 1))
            logger.warning(
                "DLBA: POST cat=%d page=%d attempt %d failed, retrying in %.1fs: %s",
                category, page, attempt, delay, exc,
            )
            await asyncio.sleep(delay)
    return None


async def _fetch_bd_structures() -> list[dict]:
    """Fetch all active structure listings from buildingdetroit.org.

    Paginates all three category groups (Auction/OIN/R&R), deduplicates on
    property_id, and excludes rows where marketable_feature == 'Under Contract'.
    """
    seen_ids: set[str] = set()
    all_rows: list[dict] = []
    skipped_under_contract = 0

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for cat in _BD_CATEGORIES:
            page = 1
            while True:
                body = await _post_bd(client, cat, page)
                if body is None:
                    break

                # buildingdetroit.org returns "listings" (not "properties" or "data")
                properties = body.get("listings") or body.get("properties") or body.get("data") or []
                pagination = body.get("pagination") or {}

                if not isinstance(properties, list):
                    logger.warning("DLBA: unexpected properties key structure for cat=%d page=%d", cat, page)
                    break

                for prop in properties:
                    # Under Contract rows are excluded from the deal feed
                    mf = (prop.get("marketable_feature") or "").strip()
                    if mf == "Under Contract":
                        skipped_under_contract += 1
                        continue

                    prop_id = _clean(prop.get("property_id"))
                    if not prop_id:
                        continue
                    if prop_id in seen_ids:
                        continue  # category overlap dedup
                    seen_ids.add(prop_id)
                    all_rows.append(prop)

                last_page = pagination.get("last_page", 1)
                if page >= last_page or not properties:
                    break
                page += 1
                await asyncio.sleep(0.5)

    logger.info(
        "DLBA: Feed 1 fetched %d structure rows (%d skipped Under Contract)",
        len(all_rows), skipped_under_contract,
    )
    return all_rows


def _structure_to_listing(
    prop: dict,
    parcel_map: dict[str, str],
) -> RawListing:
    """Convert a buildingdetroit.org property dict to RawListing.

    Joins parcel_id from Feed 2 via normalized address.  If no match,
    source_listing_id stays NULL — the row still lists (precision-first).
    """
    prop_id = _clean(prop.get("property_id"))
    address_full = _clean(prop.get("address"))  # e.g. '18978 Prairie Detroit MI 48221'
    city = canonical_city_mi(_clean(prop.get("city")))
    state = _clean(prop.get("state")) or "MI"
    prop_zip = _clean(prop.get("zipcode"))

    # Parcel join via Feed 2 address key
    parcel_id: str | None = None
    if address_full:
        key = _normalize_address_key(address_full)
        if key:
            raw_parcel = parcel_map.get(key)
            if raw_parcel:
                parcel_id = normalize_parcel_number(raw_parcel)

    # Price: prefer price field, fall back to current_minimum_bid
    price_raw = prop.get("price") or prop.get("current_minimum_bid") or prop.get("minimum_offer")
    opening_bid = _to_float(price_raw)

    # Sale date: prefer auction_closing_time, else sale_date
    sale_date = _parse_sale_date(
        prop.get("auction_closing_time") or prop.get("sale_date")
    )

    # Program name → payload
    program_name = _clean(prop.get("name"))  # 'Auction', 'Own It Now', 'Rehabbed & Ready'
    category_type = _clean(prop.get("category_type"))

    # Slug → detail URL
    slug = _clean(prop.get("property_identifier"))
    detail_url = f"https://buildingdetroit.org/properties/{slug}" if slug else None

    payload: dict[str, Any] = {}
    for k in ("name", "category_type", "neighbourhood", "district", "area",
              "bedrooms", "bathrooms", "auction_start_time", "auction_closing_time",
              "sale_date", "latitude", "longitude"):
        v = prop.get(k)
        if v not in (None, "", 0):
            payload[k] = v
    if detail_url:
        payload["detail_url"] = detail_url

    return RawListing(
        source_site=SITE_NAME,
        source_listing_id=parcel_id,   # parcel_id from Feed 2 join (may be None)
        case_number=prop_id,           # Magento property_id — stable dedup key
        signal_type=_SIGNAL_TYPE,
        property_address=address_full or f"Parcel {prop_id}",
        property_city=city,
        property_county="Wayne",
        property_state=state,
        property_zip=prop_zip,
        sale_date=sale_date,
        opening_bid_usd=opening_bid,
        status="active",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Feed 3 — DLBA_Owned_Properties buyable lots
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_buyable_lots() -> list[RawListing]:
    """Fetch DLBA-owned lots whose program is in _BUYABLE_LOT_PROGRAMS.

    Queries DLBA_Owned_Properties WHERE inventory_status_socrata LIKE '%For Sale%'
    and emits only the publicly-buyable-by-anyone programs.  Restricted classes
    (Neighborhood Lot, Side Lot, IHOA Side Lot) are skipped per G1 decision.
    """
    listings: list[RawListing] = []
    skipped_restricted = 0
    skipped_other = 0

    # NOTE: 'Accessory Structure Lot' is a BUYABLE program whose status string does NOT
    # contain "For Sale", so a bare LIKE '%For Sale%' would silently drop it (~200 lots).
    # Include it explicitly; the Python filter below keeps only _BUYABLE_LOT_PROGRAMS.
    async for batch in query_features(
        _DLBA_OWNED_URL,
        where="inventory_status_socrata LIKE '%For Sale%' OR inventory_status_socrata = 'Accessory Structure Lot'",
        out_fields="parcel_id,name,inventory_status_socrata,neighborhood,council_district,longitude,latitude",
        batch_size=_ARCGIS_BATCH,
    ):
        for attrs in batch:
            program = _clean(attrs.get("inventory_status_socrata")) or ""

            if program in _RESTRICTED_LOT_PROGRAMS:
                skipped_restricted += 1
                continue
            if program not in _BUYABLE_LOT_PROGRAMS:
                # Structures (Marketed Structure, Own It Now Structure, etc.) handled
                # via Feed 1; skip here to avoid duplication.
                skipped_other += 1
                continue

            raw_parcel = _clean(attrs.get("parcel_id"))
            # Detroit parcel format: pass VERBATIM through normalize_parcel_number —
            # trailing period is meaningful and must be preserved for assessor joins.
            parcel_id = normalize_parcel_number(raw_parcel) if raw_parcel else None

            street_addr = _clean(attrs.get("name"))  # field map: 'name' = street address
            property_address = (
                f"{street_addr} Detroit MI" if street_addr else (f"Parcel {parcel_id}" if parcel_id else "Unknown")
            )

            payload: dict[str, Any] = {}
            for k in ("neighborhood", "council_district", "longitude", "latitude"):
                v = attrs.get(k)
                if v not in (None, ""):
                    payload[k] = v

            listings.append(RawListing(
                source_site=SITE_NAME,
                source_listing_id=parcel_id,
                case_number=parcel_id,      # parcel# is the dedup key
                signal_type=_SIGNAL_TYPE,
                property_address=property_address,
                property_city="Detroit",
                property_county="Wayne",
                property_state="MI",
                sale_date=None,             # no auction date on lots
                opening_bid_usd=None,       # no price in Feed 3
                status="active",
            ))

    logger.info(
        "DLBA: Feed 3 fetched %d buyable-lot listings "
        "(%d skipped restricted [Neighborhood/Side/IHOA], %d skipped other)",
        len(listings), skipped_restricted, skipped_other,
    )
    return listings


# ─────────────────────────────────────────────────────────────────────────────
# City canonicalization for MI
# ─────────────────────────────────────────────────────────────────────────────

def canonical_city_mi(city: str | None) -> str | None:
    """Title-case a city name for storage (mirrors db.canonical_city)."""
    if city is None:
        return None
    c = city.strip()
    if not c:
        return None
    return " ".join(w.capitalize() for w in c.lower().split())


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class WayneDLBAScraper(ListingScraper):
    """Detroit Land Bank Authority (DLBA) buy-now listing scraper.

    Emits two tiers of listings:
      (a) ~30 active structures (Auction / Own It Now / Rehabbed & Ready) from
          buildingdetroit.org with price and auction dates.  Parcel joined from
          DLBA_For_Sale ArcGIS layer by address.
      (b) ~1.5K publicly-buyable lots (Marketed/Improved/Oversized/Accessory) from
          DLBA_Owned_Properties ArcGIS.  No price; parcel present.

    Skipped: Neighborhood Lot (~30K) and Side Lot (~1.5K) — restricted per G1.
    """

    site_name = SITE_NAME

    async def fetch_and_parse(self) -> list[RawListing]:
        # Fetch Feed 2 first (small, ~36 rows) — needed to join parcel_id onto
        # Feed 1 structure rows, which carry no parcel in their list JSON.
        logger.info("DLBA: fetching Feed 2 (DLBA_For_Sale ArcGIS) for parcel join map")
        parcel_map = await _fetch_for_sale_parcel_map()

        logger.info("DLBA: fetching Feed 1 (buildingdetroit.org structures)")
        structure_rows = await _fetch_bd_structures()
        structure_listings = [
            _structure_to_listing(row, parcel_map) for row in structure_rows
        ]

        logger.info("DLBA: fetching Feed 3 (DLBA_Owned_Properties buyable lots)")
        lot_listings = await _fetch_buyable_lots()

        all_listings = structure_listings + lot_listings
        logger.info(
            "DLBA: returning %d total listings (%d structures, %d lots)",
            len(all_listings), len(structure_listings), len(lot_listings),
        )
        return all_listings


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run proof — no DB writes
# ─────────────────────────────────────────────────────────────────────────────

async def _dry_run_sample() -> None:
    """Fetch live, print structure listings + lot sample by program. No DB writes."""
    import logging as _logging
    _logging.basicConfig(level=logging.INFO)

    print("\n=== DLBA dry-run: Structures (Feed 1 + Feed 2 join) ===\n")
    parcel_map = await _fetch_for_sale_parcel_map()
    structure_rows = await _fetch_bd_structures()

    print(f"Feed 2 parcel-map entries: {len(parcel_map)}")
    print(f"Feed 1 structure rows (excl. Under Contract): {len(structure_rows)}\n")

    joined = 0
    for row in structure_rows:
        addr_full = _clean(row.get("address")) or ""
        key = _normalize_address_key(addr_full)
        raw_parcel = parcel_map.get(key) if key else None
        parcel_id = normalize_parcel_number(raw_parcel) if raw_parcel else None
        if parcel_id:
            joined += 1
        price_raw = row.get("price") or row.get("current_minimum_bid")
        sale_dt = _parse_sale_date(row.get("auction_closing_time") or row.get("sale_date"))
        mf = (row.get("marketable_feature") or "").strip()
        prog = _clean(row.get("name")) or "?"
        print(
            f"  id={row.get('property_id', '?'):>8}  prog={prog:<20}  "
            f"price={str(price_raw or '?'):>8}  "
            f"sale={str(sale_dt or '?'):12}  "
            f"parcel={str(parcel_id or '[none]'):16}  "
            f"mf={mf!r:20}  "
            f"addr={addr_full}"
        )

    print(f"\nStructures with parcel joined: {joined}/{len(structure_rows)}\n")

    print("=== DLBA dry-run: Buyable Lots (Feed 3) ===\n")
    # Collect raw for counting before building RawListing objects
    lot_counts: dict[str, int] = {}
    lot_samples: list[dict] = []
    restricted_count = 0

    async for batch in query_features(
        _DLBA_OWNED_URL,
        where="inventory_status_socrata LIKE '%For Sale%'",
        out_fields="parcel_id,name,inventory_status_socrata,neighborhood",
        batch_size=_ARCGIS_BATCH,
    ):
        for attrs in batch:
            prog = _clean(attrs.get("inventory_status_socrata")) or "Unknown"
            if prog in _RESTRICTED_LOT_PROGRAMS:
                restricted_count += 1
                continue
            if prog not in _BUYABLE_LOT_PROGRAMS:
                continue
            lot_counts[prog] = lot_counts.get(prog, 0) + 1
            if len(lot_samples) < 5 or lot_counts.get(prog, 0) <= 2:
                raw_p = _clean(attrs.get("parcel_id"))
                parcel = normalize_parcel_number(raw_p) if raw_p else None
                lot_samples.append({
                    "program": prog,
                    "address": _clean(attrs.get("name")),
                    "parcel": parcel,
                })

    print("Buyable lot counts by program:")
    for prog, cnt in sorted(lot_counts.items(), key=lambda x: -x[1]):
        print(f"  {prog:<35} {cnt:>5}")
    print(f"\nRestricted lots SKIPPED (Neighborhood/Side/IHOA): {restricted_count}")

    print("\nSample lot rows (up to 12):")
    for s in lot_samples[:12]:
        parcel = s["parcel"] or "[none]"
        trailing_ok = "." in str(s["parcel"]) if s["parcel"] else False
        print(
            f"  prog={s['program']:<35}  "
            f"parcel={parcel:<18}  trailing_period={'yes' if trailing_ok else 'no':3}  "
            f"addr={s['address']}"
        )

    total_buyable = sum(lot_counts.values())
    print(f"\nTotal buyable lots: {total_buyable}")
    print(f"Total restricted lots skipped: {restricted_count}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_dry_run_sample())
