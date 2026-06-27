"""
Montgomery County (OH / Dayton) parcel registry spine scraper.

SPINE SOURCE: Montgomery County Auditor — VantagePoints ArcGIS, AUDGIS_B1 MapServer, Layer 7.
  URL: https://gis.mcohio.org/server/rest/services/VantagePoints/AUDGIS_B1/MapServer/7
  ⚠️ Path is /server/ NOT /arcgis/ (the /arcgis/ path 404s). Layer is id 7 "Parcels", NOT 5.
  273,039 parcels (2026-06-26). maxRecordCount 2000. ArcGIS Server 11.3. Open, no auth.
  Layer 7 is a server-side join of SDE.mc_parcel_polygon (geometry + tax ids) → SDE.WEB_CAMA
  (full Tyler iasWorld CAMA). Pagination: resultOffset + orderByFields=SDE.WEB_CAMA.OBJECTID.

INVARIANT — TABLE-QUALIFIED outFields ARE REQUIRED: unqualified names (e.g. 'PARID') return an
  ArcGIS 400 error because Layer 7 is a server-side join. All outFields and WHERE clauses MUST
  use the fully-qualified form 'SDE.WEB_CAMA.<FIELD>' (e.g. 'SDE.WEB_CAMA.PARID'). The JSON
  response attributes dict ALSO uses these qualified names as keys — attrs['SDE.WEB_CAMA.PARID'],
  not attrs['PARID']. Verified live 2026-06-26.

INVARIANT — STRING-CAST TRAP (money / acreage / price fields): APPRTOTAL, ACRES, and SALE_PRICE
  are esriFieldTypeString WITH number FORMATTING — e.g. APPRTOTAL='42,810', ACRES='.0000',
  SALE_PRICE=' ' (space on non-sale transfer). DWEL_YRBLT is esriFieldTypeDouble (float). NEVER
  call float() or int() bare on any source value. Always use _safe_float() (which strips commas
  and leading/trailing whitespace) and _parse_sale_date() (which parses 'MM/DD/YYYY' strings).
  One unguarded cast on a malformed row aborts a 137-page sweep mid-flight and leaves the spine
  half-updated — the same class of bug that prompted the Summit GIS-truncation guard.

INVARIANT: this scraper populates tranchi.parcels ONLY — it NEVER writes tranchi.listings or
  tranchi.signals. run.py special-cases it via the registry path ('dayton_parcels' in the
  registry tuple in _run_scraper). It is a registry source, not a deal feed.
  registry_in_full_run=False in MARKET_SCRAPERS — too heavy (273K) for the 3h cron; it runs
  on its own weekly cron Sunday 0:00 UTC via `--site dayton_parcels`.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from app.scrapers.arcgis_client import query_features
from app.scrapers.base import ListingScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawListing

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_FEATURE_SERVER = (
    "https://gis.mcohio.org/server/rest/services/VantagePoints/AUDGIS_B1/MapServer/7"
)

# Service maxRecordCount is 2000. 273K parcels / 2000 = ~137 pages.
_PAGE_SIZE = 2000

# TABLE-QUALIFIED field names are mandatory (Layer 7 = server-side join; unqualified names ERROR).
# The JSON response attributes dict also uses these exact qualified strings as keys.
_OUT_FIELDS = ",".join([
    "SDE.WEB_CAMA.PARID",        # alphanumeric PRINT_KEY, e.g. 'R72 11703 0016'
    "SDE.WEB_CAMA.OWNER_NAME1",  # owner of record (primary)
    "SDE.WEB_CAMA.PARLOC",       # situs address, e.g. '2064 RUSTIC RD '
    "SDE.WEB_CAMA.CLASS",        # property class: R / C / etc.
    "SDE.WEB_CAMA.LUC",          # land use code (3-digit, e.g. '510')
    "SDE.WEB_CAMA.APPRTOTAL",    # appraised total — esriFieldTypeString WITH commas ('42,810')
    "SDE.WEB_CAMA.DWEL_YRBLT",   # year built (residential) — esriFieldTypeDouble, float or None
    "SDE.WEB_CAMA.ACRES",        # acreage — esriFieldTypeString ('.0000')
    "SDE.WEB_CAMA.SALE_DATE",    # last sale date — string 'MM/DD/YYYY' or blank
    "SDE.WEB_CAMA.SALE_PRICE",   # last sale price — esriFieldTypeString (' ' on non-sale transfer)
])

# Stable pagination: orderByFields on OBJECTID prevents page drift across requests.
_ORDER_BY = {"orderByFields": "SDE.WEB_CAMA.OBJECTID"}

_MULTISPACE = re.compile(r"\s+")


# ─────────────────────────────────────────────────────────────────────────────
# Field mapping helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean(s: Any) -> str:
    """Strip and collapse internal whitespace on a raw value."""
    if s is None:
        return ""
    return _MULTISPACE.sub(" ", str(s)).strip()


def _safe_float(v: Any) -> float | None:
    """Parse a string-formatted numeric to float.

    Montgomery AUDGIS returns money/acreage/price fields as esriFieldTypeString WITH
    FORMATTING: APPRTOTAL='42,810', ACRES='.0000', SALE_PRICE=' ' (space on non-sale
    transfers). Strip commas and whitespace before parsing. Return None on blank or
    non-numeric. Never call float() bare on a source value from this layer.
    """
    if v in (None, ""):
        return None
    s = str(v).replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _parse_sale_date(v: Any) -> date | None:
    """Parse a 'MM/DD/YYYY' date string to a python date.

    Montgomery AUDGIS returns SALE_DATE as a string 'MM/DD/YYYY' (e.g. '03/17/2016').
    Blank or whitespace-only means no recorded sale transfer. Returns None on blank or
    any unparseable value — never raises.
    """
    if not v:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        return None


def _attrs_to_parcel(attrs: dict[str, Any]) -> dict[str, Any] | None:
    """Map a Montgomery AUDGIS attribute dict to the shape _fo_upsert_parcels expects.

    Returns None if the row lacks a usable parcel number (skip silently).

    Attribute keys use the TABLE-QUALIFIED form ('SDE.WEB_CAMA.PARID') as returned by
    the Layer 7 join response — NOT the short alias form ('PARID').
    """
    raw_id = _clean(attrs.get("SDE.WEB_CAMA.PARID"))
    if not raw_id:
        return None
    parcel_number = normalize_parcel_number(raw_id)
    if not parcel_number:
        return None

    # APPRTOTAL: string WITH comma formatting ('42,810'). Strip commas via _safe_float.
    mv = _safe_float(attrs.get("SDE.WEB_CAMA.APPRTOTAL"))
    market_value = mv if (mv is not None and mv > 0) else None

    # SALE_PRICE: string; ' ' on non-sale transfer → _safe_float → None.
    sp = _safe_float(attrs.get("SDE.WEB_CAMA.SALE_PRICE"))
    sale_price = sp if (sp is not None and sp > 0) else None

    # DWEL_YRBLT: esriFieldTypeDouble → arrives as float (e.g. 1928.0) or None.
    # Guard: 0.0 / missing → None; valid year → int.
    _yrblt = attrs.get("SDE.WEB_CAMA.DWEL_YRBLT")
    year_built = int(_yrblt) if (_yrblt is not None and _yrblt > 0) else None

    # ACRES: string ('.0000'); useful for downstream filtering but not in core upsert.
    acreage = _safe_float(attrs.get("SDE.WEB_CAMA.ACRES"))

    situs = _clean(attrs.get("SDE.WEB_CAMA.PARLOC")) or None
    if situs:
        situs = situs.title()

    return {
        "parcel_number": parcel_number,
        # Preserve the original spaced PRINT_KEY as native_parcel_id for URL/UI display.
        # parcel_number is the collapsed join key; native_parcel_id is the human-readable form.
        "native_parcel_id": raw_id,
        "owner_name": _clean(attrs.get("SDE.WEB_CAMA.OWNER_NAME1")).title() or None,
        "owner_mailing_address": None,   # not on this layer
        "situs_address": situs,
        "property_class": _clean(attrs.get("SDE.WEB_CAMA.CLASS")) or None,
        "land_use_code": _clean(attrs.get("SDE.WEB_CAMA.LUC")) or None,
        "land_use_description": None,    # LUC description not on this layer
        "neighborhood_code": None,
        "ward": None,
        "property_zip": None,            # situs ZIP not on this layer
        "current_market_value": market_value,
        "source_url": f"{_FEATURE_SERVER}/query",
        "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
        "tax_balance_due": None,         # Treasurer data, not on this layer
        "taxable_value": None,           # ASSDTOTAL available but not in core outFields
        "year_built": year_built,
        "acreage": acreage,
        "school_district": None,
        "last_sale_date": _parse_sale_date(attrs.get("SDE.WEB_CAMA.SALE_DATE")),
        "last_sale_price": sale_price,
        "delinquent_flag": False,
        "tax_years_delinquent": None,
        "first_delinquent_year": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Spine scraper class
# ─────────────────────────────────────────────────────────────────────────────

class DaytonParcelsScraper(ListingScraper):
    """Montgomery County (OH / Dayton) parcel registry spine.

    Registry scraper — mirrors SummitParcelsScraper / WayneParcelsScraper for run.py.
    Exposes fetch_parcels() returning raw hit dicts for _fo_upsert_parcels().
    fetch_and_parse() is present only to satisfy the ListingScraper ABC; run.py routes
    this through the registry path (fetch_parcels + upsert_parcels) and never calls it.

    Full sweep (273K parcels at 2000/page = ~137 requests) takes several minutes.
    For a proof/dry run, pass max_parcels=N to the constructor.
    """

    site_name = "Montgomery County Parcels (AUDGIS)"

    def __init__(self, max_parcels: int | None = None, page_size: int = _PAGE_SIZE) -> None:
        self.max_parcels = max_parcels
        self.page_size = min(page_size, 2000)

    async def fetch_parcels(self) -> list[dict[str, Any]]:
        """Paginate through Montgomery AUDGIS Layer 7 and return parsed parcel dicts."""
        all_parcels: list[dict[str, Any]] = []
        seen: set[str] = set()
        skipped_no_id = 0

        async for batch in query_features(
            _FEATURE_SERVER,
            # Filter to rows where the CAMA join matched — the polygon layer has ~26K rows
            # with no WEB_CAMA counterpart (all fields null); including them wastes pages
            # and produces 100% skip_no_id. Verified live: IS NOT NULL gives 246,872 rows
            # vs 273,039 total (the ~26K difference are empty/non-taxable polygon stubs).
            where="SDE.WEB_CAMA.PARID IS NOT NULL",
            out_fields=_OUT_FIELDS,
            batch_size=self.page_size,
            extra_params=_ORDER_BY,
        ):
            for attrs in batch:
                parcel = _attrs_to_parcel(attrs)
                if parcel is None:
                    skipped_no_id += 1
                    continue
                pn = parcel["parcel_number"]
                if pn in seen:
                    continue
                seen.add(pn)
                all_parcels.append(parcel)

            logger.info(
                "DaytonParcels: total so far=%d (skipped no-id=%d)",
                len(all_parcels), skipped_no_id,
            )
            if self.max_parcels is not None and len(all_parcels) >= self.max_parcels:
                logger.info(
                    "DaytonParcels: reached max_parcels cap (%d), stopping.", self.max_parcels
                )
                all_parcels = all_parcels[: self.max_parcels]
                break

        logger.info(
            "DaytonParcels: sweep complete — %d unique parcels, %d skipped (no id).",
            len(all_parcels), skipped_no_id,
        )
        return all_parcels

    async def fetch_and_parse(self) -> list[RawListing]:
        """Intentionally disabled — registry source, not a listing feed.

        run.py routes this through the registry path (fetch_parcels + _fo_upsert_parcels)
        and never calls fetch_and_parse(). If you hit this error, the 'dayton_parcels'
        elif branch in run.py::_run_scraper is broken.
        """
        raise NotImplementedError(
            "DaytonParcelsScraper is a registry source — use fetch_parcels() via the "
            "registry path in run.py, not fetch_and_parse()."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run proof
# ─────────────────────────────────────────────────────────────────────────────

async def _dry_run_sample(n: int = 25) -> None:
    import json

    print(f"\n=== Montgomery County (Dayton) Parcels — dry-run sample (first {n}) ===\n")
    scraper = DaytonParcelsScraper(max_parcels=n, page_size=min(n, 2000))
    parcels = await scraper.fetch_parcels()
    print(f"Fetched {len(parcels)} parcels\n")
    for p in parcels[:10]:
        print(
            f"  parcel={p['parcel_number']!r:14s} class={p['property_class'] or '?':2s} "
            f"luc={p['land_use_code'] or '?':4s} mv={p['current_market_value']} "
            f"yr={p['year_built']} owner={str(p['owner_name'])[:26]:26s} situs={p['situs_address']}"
        )
    if parcels:
        print("\n--- Full dict for first parcel ---")
        print(json.dumps(
            {k: str(v) for k, v in parcels[0].items() if v is not None}, indent=2
        ))


async def _trace_proof_parcel() -> None:
    """Query the R72 proof parcel directly and print its resolved fields.

    Expected: OWNER='Vanzant Donnie', PARLOC='2064 Rustic Rd', CLASS='R',
    normalized key='R72117030016'.
    """
    from app.scrapers.arcgis_client import query_features as qf

    print("\n=== R72 proof parcel trace: PARID='R72 11703 0016' ===\n")
    found = []
    async for batch in qf(
        _FEATURE_SERVER,
        where="SDE.WEB_CAMA.PARID = 'R72 11703 0016'",
        out_fields=_OUT_FIELDS,
        batch_size=10,
    ):
        for attrs in batch:
            p = _attrs_to_parcel(attrs)
            if p:
                found.append(p)
                print(f"  parcel_number  : {p['parcel_number']!r}")
                print(f"  native_parcel_id: {p['native_parcel_id']!r}")
                print(f"  owner_name     : {p['owner_name']!r}")
                print(f"  situs_address  : {p['situs_address']!r}")
                print(f"  property_class : {p['property_class']!r}")
                print(f"  land_use_code  : {p['land_use_code']!r}")
                print(f"  market_value   : {p['current_market_value']}")
                print(f"  year_built     : {p['year_built']}")
                print(f"  last_sale_date : {p['last_sale_date']}")
                print(f"  last_sale_price: {p['last_sale_price']}")
    if not found:
        print("  ERROR: proof parcel not found!")
    print()


if __name__ == "__main__":
    import asyncio
    import sys
    logging.basicConfig(level=logging.INFO)
    if "--trace" in sys.argv:
        asyncio.run(_trace_proof_parcel())
    else:
        asyncio.run(_dry_run_sample(25))
