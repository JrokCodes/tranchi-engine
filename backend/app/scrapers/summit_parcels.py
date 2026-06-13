"""
Summit County (OH / Akron) parcel registry spine scraper.

SPINE SOURCE: Summit County GIS hosted Tax Parcels FeatureServer
  URL: https://scgis.summitoh.net/hosted/rest/services/parcels_web_GEODATA_Tax_Parcels/FeatureServer/0
  261,484 parcels (2026-06-13). maxRecordCount 3000. Plain hosted ArcGIS — NO legacy
  TLS needed (unlike Shelby ReGIS), reachable with a normal browser UA. We use the
  shared arcgis_client.query_features (resultOffset pagination + transient-error retry).

CANONICAL SUMMIT PARCEL FORMAT: 7-digit zero-padded numeric string (e.g. '7000697',
  '6700526', '0101379'). LEADING ZEROS ARE LOAD-BEARING — string compare only. One
  format for the whole market (unlike Cuyahoga/Wayne's two formats). The Fiscal Office
  DISPLAY form on RealAuction / Akron Legal News inserts a dash after digit 2
  ('67-08383'); normalize_parcel_number(summit) strips it. Format-lock proven live
  2026-06-13: parcel 7000697 joins GIS spine + RealAuction + ALN ('70-00697') + the
  delinquent-tax tape; land bank pid 6700526 + DELQ 0101379 also resolve 1:1.

FIELD NOTES (live-verified 2026-06-13):
  - siteaddress carries leading/trailing + double spaces ('976  HAMPTON RIDGE DR ') and
    is sometimes house-number-less for vacant land ('  PRINCETON ST '); strip + collapse.
  - cvttxdscrp is the taxing district ~= city ('AKRON ', 'BARBERTON', 'NEW FRANKLIN CITY ').
  - There is NO situs ZIP on this layer; pstlzip5 is the OWNER MAILING zip, not the
    property's — so property_zip stays NULL on the spine (the listing sources carry it).
  - cntmarval (current market value) IS populated (~256K rows > 0); Shelby's spine had
    no value, Summit's does — surfaced for equity ranking.

INVARIANT: this scraper populates tranchi.parcels ONLY — it NEVER writes tranchi.listings
  or tranchi.signals. run.py special-cases it via the fiscal_officer-style registry path
  ('summit_parcels' in the registry tuple in _run_scraper). It is a registry source, not
  a deal feed. registry_in_full_run=False in MARKET_SCRAPERS — too heavy (261K) for the
  3h cron; it runs on its own weekly cron via `--site summit_parcels`.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
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
    "https://scgis.summitoh.net/hosted/rest/services/"
    "parcels_web_GEODATA_Tax_Parcels/FeatureServer/0"
)

# Service maxRecordCount is 3000; 2000 (the shared client default) stays under it
# with margin. 261K parcels / 2000 = ~131 pages.
_PAGE_SIZE = 2000

# Fields we actually need (skip geometry + the change-tracking *cg/*chg columns).
_OUT_FIELDS = ",".join([
    "parcelid",     # 7-digit canonical parcel id
    "ownernme1",    # owner name
    "ownernme2",    # owner name continuation (trust / second owner)
    "pstladdress",  # owner MAILING address (not situs)
    "pstlcity",
    "pstlstate",
    "pstlzip5",
    "siteaddress",  # situs street address (no zip on this layer)
    "cvttxdscrp",   # taxing district ~= city
    "usecd",        # land use code (5xx = residential)
    "usedscrp",     # land use description
    "classcd",      # property class (R/C/O)
    "cntmarval",    # current market value (populated)
    "cnttxblval",   # current taxable value
    "resyrblt",     # year built
])

_MULTISPACE = re.compile(r"\s+")


# ─────────────────────────────────────────────────────────────────────────────
# Field mapping helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean(s: Any) -> str:
    """Strip + collapse internal whitespace on a raw string-ish value."""
    if s is None:
        return ""
    return _MULTISPACE.sub(" ", str(s)).strip()


def _safe_float(v: Any) -> float | None:
    """Parse a value to float, returning None on blank/None/non-numeric.

    ArcGIS numeric fields are normally JSON numbers, but a single feature with a
    data-quality issue can emit a sentinel string ('N/A', '-'). An unguarded float()
    on one bad row in a 261K-parcel sweep would abort mid-sweep and leave the spine
    partially updated — so never call float() bare on a source value here.
    """
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _build_owner_name(attrs: dict[str, Any]) -> str | None:
    owner = _clean(attrs.get("ownernme1"))
    ext = _clean(attrs.get("ownernme2"))
    if owner and ext:
        return f"{owner} {ext}"
    return owner or None


def _build_owner_mailing(attrs: dict[str, Any]) -> str | None:
    addr = _clean(attrs.get("pstladdress"))
    city = _clean(attrs.get("pstlcity"))
    state = _clean(attrs.get("pstlstate"))
    zipc = _clean(attrs.get("pstlzip5"))
    parts = []
    if addr:
        parts.append(addr)
    csz = " ".join(x for x in (city, state, zipc) if x)
    if csz:
        parts.append(csz)
    return ", ".join(parts) if parts else None


def _build_situs(attrs: dict[str, Any]) -> str | None:
    """Build situs string '<street>, <city>, OH' (no situs zip exists on this layer)."""
    street = _clean(attrs.get("siteaddress"))
    if not street:
        return None
    street = street.title()
    city = _clean(attrs.get("cvttxdscrp")).title()
    if city and city.lower() not in street.lower():
        return f"{street}, {city}, OH"
    return f"{street}, OH" if street else None


def _attrs_to_parcel(attrs: dict[str, Any]) -> dict[str, Any] | None:
    """Map a Summit GIS attribute dict to the shape _fo_upsert_parcels expects.

    Returns None if the row lacks a usable parcel number (skip silently).
    """
    raw_id = _clean(attrs.get("parcelid"))
    if not raw_id:
        return None
    parcel_number = normalize_parcel_number(raw_id)
    if not parcel_number:
        return None

    mv = _safe_float(attrs.get("cntmarval"))
    market_value = mv if (mv is not None and mv > 0) else None

    return {
        "parcel_number": parcel_number,
        # Summit has a single canonical format → no native spaced form like Shelby.
        "native_parcel_id": None,
        "owner_name": _build_owner_name(attrs),
        "owner_mailing_address": _build_owner_mailing(attrs),
        "situs_address": _build_situs(attrs),
        "property_class": _clean(attrs.get("classcd")) or None,
        "land_use_code": _clean(attrs.get("usecd")) or None,
        "land_use_description": _clean(attrs.get("usedscrp")) or None,
        "neighborhood_code": None,
        "ward": None,
        # No situs ZIP on this layer (pstlzip5 is owner mailing) — leave NULL.
        "property_zip": None,
        "current_market_value": market_value,
        "source_url": f"{_FEATURE_SERVER}/query",
        "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
        # Fields not on this layer (enriched downstream / not applicable).
        "tax_balance_due": None,
        "taxable_value": _safe_float(attrs.get("cnttxblval")),
        "year_built": attrs.get("resyrblt") or None,
        "school_district": None,
        "last_sale_date": None,
        "last_sale_price": None,
        "delinquent_flag": False,
        "tax_years_delinquent": None,
        "first_delinquent_year": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Spine scraper class
# ─────────────────────────────────────────────────────────────────────────────

class SummitParcelsScraper(ListingScraper):
    """Summit County (OH) parcel registry spine.

    Registry scraper — mirrors FiscalOfficerScraper / ShelbyParcelsScraper for run.py.
    Exposes fetch_parcels() returning raw hit dicts for _fo_upsert_parcels().
    fetch_and_parse() is present only to satisfy the ListingScraper ABC; run.py routes
    this through the registry path (fetch_parcels + upsert_parcels) and never calls it.

    Full sweep (261K parcels at 2000/page = ~131 requests) takes a few minutes.
    For a proof run, pass max_parcels=500 to the constructor.
    """

    site_name = "Summit County Parcels (GIS)"

    def __init__(self, max_parcels: int | None = None, page_size: int = _PAGE_SIZE) -> None:
        self.max_parcels = max_parcels
        self.page_size = min(page_size, 3000)

    async def fetch_parcels(self) -> list[dict[str, Any]]:
        """Paginate through Summit GIS Tax Parcels and return parsed parcel dicts."""
        all_parcels: list[dict[str, Any]] = []
        seen: set[str] = set()
        skipped_no_id = 0

        async for batch in query_features(
            _FEATURE_SERVER,
            where="1=1",
            out_fields=_OUT_FIELDS,
            batch_size=self.page_size,
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
                "SummitParcels: total so far=%d (skipped no-id=%d)",
                len(all_parcels), skipped_no_id,
            )
            if self.max_parcels is not None and len(all_parcels) >= self.max_parcels:
                logger.info("SummitParcels: reached max_parcels cap (%d), stopping.", self.max_parcels)
                all_parcels = all_parcels[: self.max_parcels]
                break

        logger.info(
            "SummitParcels: sweep complete — %d unique parcels, %d skipped (no id).",
            len(all_parcels), skipped_no_id,
        )
        return all_parcels

    async def fetch_and_parse(self) -> list[RawListing]:
        """Intentionally disabled — registry source, not a listing feed.

        run.py routes this through the registry path (fetch_parcels + _fo_upsert_parcels)
        and never calls fetch_and_parse(). If you hit this error, the 'summit_parcels'
        elif branch in run.py::_run_scraper is broken.
        """
        raise NotImplementedError(
            "SummitParcelsScraper is a registry source — use fetch_parcels() via the "
            "registry path in run.py, not fetch_and_parse()."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run proof
# ─────────────────────────────────────────────────────────────────────────────

async def _dry_run_sample(n: int = 25) -> None:
    import asyncio  # noqa: F401  (kept local; module is import-light)
    import json

    print(f"\n=== Summit County Parcels — dry-run sample (first {n}) ===\n")
    scraper = SummitParcelsScraper(max_parcels=n, page_size=min(n, 3000))
    parcels = await scraper.fetch_parcels()
    print(f"Fetched {len(parcels)} parcels\n")
    for p in parcels[:10]:
        print(
            f"  parcel={p['parcel_number']!r:10s} class={p['property_class'] or '?':2s} "
            f"luc={p['land_use_code'] or '?':4s} mv={p['current_market_value']} "
            f"owner={str(p['owner_name'])[:28]:28s} situs={p['situs_address']}"
        )
    if parcels:
        print("\n--- Full dict for first parcel ---")
        print(json.dumps({k: v for k, v in parcels[0].items() if v is not None}, indent=2))


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_dry_run_sample(25))
