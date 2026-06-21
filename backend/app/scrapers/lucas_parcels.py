"""
Lucas County (OH / Toledo) parcel registry spine scraper.

SPINE SOURCE: Lucas County Auditor AREIS — ArcGIS MapServer (OPEN, no gated export).
  Base: https://lcaudgis.co.lucas.oh.us/gisaudserver/rest/services/AREIS_Web_Map_MIL1/MapServer
  - Layer 38 "Parcels Land Use Classification" — PARID (7-digit deal parcel) + ASSESSOR_NUM
    (GIS-internal crosswalk) + OWNER / PROPERTY_ADDRESS / MAILING_ADDRESS / LUC / CLASS /
    LEGAL_DESCRIPTION / TAXDIST / TAXYR. ~192,691 records.
  - Layer 84 (appraised values) — APRLAND / APRBLDG / APRTOT, joined to L38 by PARID.
  Reachable only from the engine box (the host accepts the TLS handshake but withholds the
  HTTP response to non-allowlisted IPs — same anti-bot posture as the rest of Lucas recon).

CANONICAL LUCAS PARCEL FORMAT: PARID = 7-digit zero-padded numeric STRING (e.g. '1210314',
  '0100001'). LEADING ZEROS ARE LOAD-BEARING — string compare only. PARID is the canonical
  JOIN KEY across every Lucas source (NOT ASSESSOR_NUM, which is the 8-digit GIS-internal id).
  RealAuction / Toledo Legal News DISPLAY inserts a dash ('12-10314'); normalize strips it.
  96 condo/split parcels are 8-digit base + 'S' (e.g. '04349092S'). Because a bare 7-digit
  PARID is FORM-IDENTICAL to a Summit parcel, normalization is MARKET-DISPATCHED:
  normalize_parcel_for_market(raw, 'lucas') -> normalize_parcel_lucas (F-008). Spine writes
  go through _fo_upsert_parcels with market='lucas', so identity is (parcel_number, 'lucas').

PROOF ROW (recon 2026-06-22): PARID '1210314' -> ASSESSOR_NUM '02188002', OWNER
  'STEPHENS CORNELIUS', 3941 VERMAAS AVE, LUC 510 (residential), APRTOT $71,800
  (land 13,600 / bldg 58,200) — = RealAuction '12-10314'.

INVARIANT: populates tranchi.parcels ONLY — never tranchi.listings/signals. run.py routes it
  via the registry path (fetch_parcels + _fo_upsert_parcels, market='lucas'), like
  summit_parcels / wayne_parcels. registry_in_full_run=False (192K is too heavy for the 3h
  cron; own weekly cron via `--site lucas_parcels`).

ENGINE-BOX VALIDATION (before go-live): field names below are from the recon doc, not a live
  schema probe (the host is unreachable off-box). Run `python -m app.scrapers.lucas_parcels`
  (the dry-run sample) on the engine box and confirm: (1) the L38 field names resolve, (2) the
  L84 value join keys on PARID, (3) usecd/LUC residential prefix, (4) the proof row above. If a
  field name differs, fix the _OUT_FIELDS / _attrs_to_parcel map — the structure stays.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.scrapers.arcgis_client import query_features
from app.scrapers.base import ListingScraper
from app.scrapers.db import normalize_parcel_for_market
from app.scrapers.models import RawListing

logger = logging.getLogger(__name__)

_MARKET = "lucas"

_MAPSERVER = (
    "https://lcaudgis.co.lucas.oh.us/gisaudserver/rest/services/"
    "AREIS_Web_Map_MIL1/MapServer"
)
_PARCEL_LAYER = f"{_MAPSERVER}/38"   # PARID + owner/address/LUC/CLASS
_VALUE_LAYER = f"{_MAPSERVER}/84"    # APRLAND / APRBLDG / APRTOT, keyed by PARID

_PAGE_SIZE = 2000

# L38 fields (recon-documented; confirm live on the engine box via the dry-run).
_PARCEL_FIELDS = ",".join([
    "PARID",              # 7-digit canonical deal parcel
    "ASSESSOR_NUM",       # GIS-internal crosswalk id (NOT the join key)
    "OWNER",              # owner of record
    "PROPERTY_ADDRESS",   # situs street address
    "MAILING_ADDRESS",    # owner mailing address
    "LUC",                # land use code (5xx = residential)
    "CLASS",              # property class (R/C/O)
    "LEGAL_DESCRIPTION",
    "TAXDIST",            # taxing district ~= city
])

# L84 value fields, joined to the parcel layer by PARID.
_VALUE_FIELDS = ",".join(["PARID", "APRLAND", "APRBLDG", "APRTOT"])

_MULTISPACE = re.compile(r"\s+")


def _clean(s: Any) -> str:
    if s is None:
        return ""
    return _MULTISPACE.sub(" ", str(s)).strip()


def _safe_float(v: Any) -> float | None:
    """Parse to float, None on blank/non-numeric. NEVER bare float() on a source value:
    one bad row ('N/A', '-') in a 192K sweep would abort it mid-spine (Summit blockers 1+2)."""
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _build_situs(attrs: dict[str, Any]) -> str | None:
    street = _clean(attrs.get("PROPERTY_ADDRESS"))
    if not street:
        return None
    street = street.title()
    city = _clean(attrs.get("TAXDIST")).title()
    if city and city.lower() not in street.lower():
        return f"{street}, {city}, OH"
    return f"{street}, OH"


def _attrs_to_parcel(attrs: dict[str, Any], values: dict[str, dict]) -> dict[str, Any] | None:
    """Map an L38 attribute dict (+ joined L84 value row) to the _fo_upsert_parcels shape.

    Returns None if the row lacks a usable PARID (skip silently).
    """
    raw_id = _clean(attrs.get("PARID"))
    if not raw_id:
        return None
    parcel_number = normalize_parcel_for_market(raw_id, _MARKET)
    if not parcel_number:
        return None

    val = values.get(parcel_number) or {}
    aprtot = _safe_float(val.get("APRTOT"))
    if aprtot is None:
        land = _safe_float(val.get("APRLAND"))
        bldg = _safe_float(val.get("APRBLDG"))
        aprtot = (land or 0) + (bldg or 0) if (land or bldg) else None
    market_value = aprtot if (aprtot and aprtot > 0) else None

    return {
        "parcel_number": parcel_number,
        # ASSESSOR_NUM is the GIS-internal id, surfaced as native_parcel_id for cross-ref /
        # debugging — it is NOT the deal/join key (that is PARID = parcel_number).
        "native_parcel_id": _clean(attrs.get("ASSESSOR_NUM")) or None,
        "owner_name": _clean(attrs.get("OWNER")) or None,
        "owner_mailing_address": _clean(attrs.get("MAILING_ADDRESS")) or None,
        "situs_address": _build_situs(attrs),
        "property_class": _clean(attrs.get("CLASS")) or None,
        "land_use_code": _clean(attrs.get("LUC")) or None,
        "land_use_description": None,
        "neighborhood_code": None,
        "ward": None,
        # No situs ZIP on this layer — listing sources carry it.
        "property_zip": None,
        "current_market_value": market_value,
        "source_url": f"{_PARCEL_LAYER}/query",
        "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
        "tax_balance_due": None,
        "taxable_value": None,
        "year_built": None,
        "school_district": None,
        "last_sale_date": None,
        "last_sale_price": None,
        "delinquent_flag": False,
        "tax_years_delinquent": None,
        "first_delinquent_year": None,
    }


class LucasParcelsScraper(ListingScraper):
    """Lucas County (OH) parcel registry spine.

    Registry scraper — mirrors SummitParcelsScraper / WayneParcelsScraper for run.py.
    Exposes fetch_parcels() returning raw hit dicts for _fo_upsert_parcels(market='lucas').
    fetch_and_parse() is present only to satisfy the ABC; run.py routes this through the
    registry path and never calls it.

    For a proof run, pass max_parcels=500 to the constructor.
    """

    site_name = "Lucas County Parcels (AREIS)"

    def __init__(self, max_parcels: int | None = None, page_size: int = _PAGE_SIZE) -> None:
        self.max_parcels = max_parcels
        self.page_size = min(page_size, 2000)

    async def _fetch_value_map(self) -> dict[str, dict]:
        """Sweep the L84 appraised-value layer into {parcel_number: value_row}, keyed by
        the SAME normalized PARID the parcel layer uses, so the in-memory join is exact."""
        values: dict[str, dict] = {}
        async for batch in query_features(
            _VALUE_LAYER, where="1=1", out_fields=_VALUE_FIELDS, batch_size=self.page_size,
        ):
            for attrs in batch:
                pid = normalize_parcel_for_market(_clean(attrs.get("PARID")), _MARKET)
                if pid:
                    values[pid] = attrs
        logger.info("LucasParcels: loaded %d appraised-value rows (L84).", len(values))
        return values

    async def fetch_parcels(self) -> list[dict[str, Any]]:
        """Sweep L38 parcels, join L84 values by PARID, return parsed parcel dicts."""
        # Skip the (heavy) value sweep on a small proof run — values enrich, don't gate.
        values = {} if self.max_parcels else await self._fetch_value_map()

        all_parcels: list[dict[str, Any]] = []
        seen: set[str] = set()
        skipped_no_id = 0

        async for batch in query_features(
            _PARCEL_LAYER, where="1=1", out_fields=_PARCEL_FIELDS, batch_size=self.page_size,
        ):
            for attrs in batch:
                parcel = _attrs_to_parcel(attrs, values)
                if parcel is None:
                    skipped_no_id += 1
                    continue
                pn = parcel["parcel_number"]
                if pn in seen:
                    continue
                seen.add(pn)
                all_parcels.append(parcel)

            logger.info(
                "LucasParcels: total so far=%d (skipped no-id=%d)", len(all_parcels), skipped_no_id,
            )
            if self.max_parcels is not None and len(all_parcels) >= self.max_parcels:
                logger.info("LucasParcels: reached max_parcels cap (%d), stopping.", self.max_parcels)
                all_parcels = all_parcels[: self.max_parcels]
                break

        logger.info(
            "LucasParcels: sweep complete — %d unique parcels, %d skipped (no id).",
            len(all_parcels), skipped_no_id,
        )
        return all_parcels

    async def fetch_and_parse(self) -> list[RawListing]:
        raise NotImplementedError(
            "LucasParcelsScraper is a registry source — use fetch_parcels() via the registry "
            "path in run.py, not fetch_and_parse()."
        )


async def _dry_run_sample(n: int = 25) -> None:
    import json

    print(f"\n=== Lucas County Parcels — dry-run sample (first {n}) ===\n")
    scraper = LucasParcelsScraper(max_parcels=n, page_size=min(n, 2000))
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
