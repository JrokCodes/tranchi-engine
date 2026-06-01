"""
Shelby County (TN / Memphis) parcel registry spine scraper.

SPINE SOURCE: ReGIS CurrentParcels MapServer layer
  URL: https://scgis.shelbycountytn.gov/serverhigh/rest/services/Parcel/CurrentParcels/MapServer/0
  ~353,448 parcels. Fields: PARCELID/PARID/PAID (parcel IDs), OWNER/OWNER_EXT,
  OWN_ADDR1/OWN_ADDR2/OWN_CITY/OWN_STATE/OWN_ZIP (owner mailing address),
  PAR_ADDR1/PAR_ADRNO/PAR_ADRSTR/PAR_ADRSUF/PAR_ZIP (situs address),
  LANDUSE/LUC (land use), NBHD (neighborhood), CLASS (property class), MUNI (municipality).

  Source chosen over Assessor public GIS (gis.shelbycountytn.gov): the Assessor endpoint
  is blocked by Cloudflare (HTTP 403) from server IP; ReGIS has equivalent or richer
  owner+situs+land-use fields and is reachable (requires legacy TLS context for old server).

TOS / LAY-LOW NOTE:
  ReGIS description says "for internal use ONLY — cannot be shared with or displayed to
  the general public." We proceed with a conservative posture:
    - 1 req/sec max (1-second sleep between pages)
    - Realistic browser User-Agent from user_agents.py
    - Modest page size: 500 (server max 1000, but we stay well under)
    - No parallel requests; single sequential loop
  This spine is a one-time or infrequent full-sweep (not a frequent cron). The read-only
  query is indistinguishable from a normal web GIS lookup.

CANONICAL TN PARCEL FORMAT: 14-char alphanumeric (e.g. '07204700000160')
  Structure: MAP(6) + '0' + GROUP.zfill(6) + '0'
  All three Shelby source formats normalize to this form via normalize_parcel_number():
    ReGIS PARCELID (spaced):  '072047  00016'  → '07204700000160'
    ReGIS PAID (compact):     '07204700016'    → '07204700000160'
    Tax Sale Alt_Parcel /
    ePropertyPlus parcelNumber: '07204700000160' → '07204700000160' (idempotent)
  The trailing '0' is the sub-parcel qualifier (whole parcel). Alpha-qualified parcels
  (e.g. 'H', 'D' group prefix) are preserved in the 14-char form ('0740370H000810').
  DOWNSTREAM BUILDERS (tax-sale, land-bank, MMLBA): ALWAYS call normalize_parcel_number()
  before using any parcel ID as a FK into tranchi.parcels or tranchi.signals.

INVARIANT: this scraper populates tranchi.parcels ONLY — it NEVER writes to
  tranchi.listings or tranchi.signals. It is a registry source, not a deal feed.
  run.py special-cases it via the fiscal_officer-style registry path.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
from datetime import datetime, timezone
from typing import Any

import httpx

from app.scrapers.base import ListingScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawListing
from app.scrapers.user_agents import random_ua

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_REGIS_URL = (
    "https://scgis.shelbycountytn.gov/serverhigh/rest/services/"
    "Parcel/CurrentParcels/MapServer/0"
)
_QUERY_URL = f"{_REGIS_URL}/query"

# Service maxRecordCount is 1000; use 500 to stay modest (lay-low posture).
_PAGE_SIZE = 500

# 1 req/sec: sleep between pages to avoid hammering the old ArcGIS server.
_PAGE_DELAY = 1.0

# Request timeout (seconds). ReGIS can be slow on large result sets.
_TIMEOUT = 45.0

# Fields we actually need — skip Shape geometry and rarely-used fields.
_OUT_FIELDS = ",".join([
    "OBJECTID",
    "PARCELID",    # spaced canonical: '072047  00016'
    "PAID",        # compact: '07204700016'
    "OWNER",       # owner name
    "OWNER_EXT",   # owner name continuation (c/o, trust name, etc.)
    "OWN_ADDR1",   # owner mailing address line 1
    "OWN_ADDR2",   # owner mailing address line 2
    "OWN_CITY",    # owner mailing city
    "OWN_STATE",   # owner mailing state
    "OWN_ZIP",     # owner mailing zip
    "PAR_ADDR1",   # pre-built situs address string ('369 FOUNTAIN RIVER DR')
    "PAR_ADRNO",   # situs street number (float, e.g. 369.0)
    "PAR_ADRSTR",  # situs street name
    "PAR_ADRSUF",  # situs street suffix
    "PAR_ZIP",     # situs zip
    "MUNI",        # municipality name
    "LANDUSE",     # land use description (e.g. 'SINGLE-FAMILY', 'VACANT')
    "LUC",         # land use code (3-char numeric string, e.g. '052')
    "NBHD",        # neighborhood code
    "CLASS",       # property class ('R'=residential, 'F'=farm, etc.)
])


# ─────────────────────────────────────────────────────────────────────────────
# TLS helper — ReGIS uses legacy TLS renegotiation (old ArcGIS server)
# ─────────────────────────────────────────────────────────────────────────────

def _regis_ssl_context() -> ssl.SSLContext:
    """Build an SSL context that allows legacy TLS renegotiation for ReGIS.

    scgis.shelbycountytn.gov runs an old ArcGIS 10.81 server that requires
    TLS unsafe legacy renegotiation. Python 3.11+ disables this by default;
    we enable it explicitly for this host only. Certificate verification is
    disabled because the legacy renegotiation flag also suppresses cert checks
    on this old server's chain. This is acceptable for a read-only public data
    query against a government GIS endpoint.
    """
    ctx = ssl.create_default_context()
    # OP_LEGACY_SERVER_CONNECT (0x4). The ssl attribute is NOT exposed on every
    # build (absent on EC2's Python against OpenSSL 3.0.2), so getattr(...,0) would
    # silently no-op → "UNSAFE_LEGACY_RENEGOTIATION_DISABLED" at runtime. Hardcode
    # the value with the attribute as a forward-compatible preference.
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4) or 0x4
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Field mapping helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_owner_name(attrs: dict[str, Any]) -> str | None:
    """Combine OWNER + OWNER_EXT into a single owner name string."""
    owner = (attrs.get("OWNER") or "").strip()
    ext = (attrs.get("OWNER_EXT") or "").strip()
    if owner and ext:
        return f"{owner} {ext}"
    return owner or None


def _build_owner_mailing(attrs: dict[str, Any]) -> str | None:
    """Build owner mailing address from OWN_ADDR1/ADDR2/CITY/STATE/ZIP."""
    parts = []
    addr1 = (attrs.get("OWN_ADDR1") or "").strip()
    addr2 = (attrs.get("OWN_ADDR2") or "").strip()
    city = (attrs.get("OWN_CITY") or "").strip()
    state = (attrs.get("OWN_STATE") or "").strip()
    zipcode = (attrs.get("OWN_ZIP") or "").strip()
    if addr1:
        parts.append(addr1)
    if addr2:
        parts.append(addr2)
    if city or state or zipcode:
        csz = " ".join(x for x in [city, state, zipcode] if x)
        if csz:
            parts.append(csz)
    return ", ".join(parts) if parts else None


def _build_situs_address(attrs: dict[str, Any]) -> str | None:
    """Build situs address from PAR_ADDR1 (preferred) or PAR_ADRNO + PAR_ADRSTR + PAR_ADRSUF."""
    # ReGIS pre-builds PAR_ADDR1 — use it when available and non-trivial
    addr1 = (attrs.get("PAR_ADDR1") or "").strip()
    if addr1 and addr1 != "0":
        return addr1.title()

    # Fallback: assemble from components
    parts = []
    num = attrs.get("PAR_ADRNO")
    if num and float(num) > 0:
        parts.append(str(int(float(num))))
    street = (attrs.get("PAR_ADRSTR") or "").strip()
    if street:
        parts.append(street.title())
    suffix = (attrs.get("PAR_ADRSUF") or "").strip()
    if suffix and suffix != " ":
        parts.append(suffix.title())
    return " ".join(parts) if parts else None


def _attrs_to_parcel(attrs: dict[str, Any]) -> dict[str, Any] | None:
    """Map ReGIS attribute dict to the shape expected by _fo_upsert_parcels.

    Returns None if the row lacks a usable parcel number (skip silently).
    """
    # PARCELID is the native spaced form: e.g. '042035  00007' (MAP + 2 spaces + 5-char group).
    # Store it verbatim as native_parcel_id — spaces are required by the Trustee URL.
    # Do NOT strip the internal spaces; only strip leading/trailing whitespace from the raw value.
    parcelid_raw = attrs.get("PARCELID") or ""
    # Strip only outer whitespace; preserve the internal double-space separator.
    parcelid_stripped = parcelid_raw.strip() if parcelid_raw else ""

    # Prefer PARCELID (spaced) for normalization; fall back to PAID (compact)
    raw_id = parcelid_stripped or (attrs.get("PAID") or "").strip()
    if not raw_id:
        return None

    parcel_number = normalize_parcel_number(raw_id)
    if not parcel_number:
        return None

    # native_parcel_id: the verbatim spaced PARCELID from ReGIS (e.g. '042035  00007').
    # Used to build one-click Shelby County Trustee URLs in verify_listings.py.
    # NULL when PARCELID is absent (fallback to PAID path — should be rare).
    native_parcel_id = parcelid_stripped if parcelid_stripped else None

    # Situs address: build from PAR_ADDR1 or components, append city+zip
    situs = _build_situs_address(attrs)
    muni = (attrs.get("MUNI") or "").strip().title()
    zipcode = (attrs.get("PAR_ZIP") or "").strip()
    if situs and muni and muni.lower() not in situs.lower():
        situs_full = f"{situs}, {muni}, TN {zipcode}".strip(", ")
    elif situs:
        situs_full = situs
    else:
        situs_full = None

    return {
        "parcel_number": parcel_number,
        "native_parcel_id": native_parcel_id,  # spaced PARCELID for Trustee URL; NULL if absent
        "owner_name": _build_owner_name(attrs),
        "owner_mailing_address": _build_owner_mailing(attrs),
        "situs_address": situs_full,
        "property_class": (attrs.get("CLASS") or "").strip() or None,
        "land_use_code": (attrs.get("LUC") or "").strip() or None,
        "land_use_description": (attrs.get("LANDUSE") or "").strip() or None,
        # NOTE: key is "neighborhood_code" to match _fo_upsert_parcels' hit.get("neighborhood_code")
        "neighborhood_code": (attrs.get("NBHD") or "").strip() or None,
        "ward": muni or None,                   # closest ward-level field is municipality
        "property_zip": zipcode or None,
        "source_url": _QUERY_URL,
        "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
        # Fields not available in this layer (enriched by downstream scrapers)
        "acreage": None,
        "year_built": None,
        "sq_ft": None,
        "beds": None,
        "baths": None,
        "last_sale_date": None,
        "last_sale_price": None,
        "current_market_value": None,
        "taxable_value": None,
        "current_tax_balance": None,
        "delinquent_flag": False,
        "tax_years_delinquent": None,
        "first_delinquent_year": None,
        "tax_status_flags": None,
        "tax_enriched_at": None,
        "school_district": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Spine scraper class
# ─────────────────────────────────────────────────────────────────────────────

class ShelbyParcelsScraper(ListingScraper):
    """Shelby County (TN) parcel registry spine.

    Registry scraper — mirrors FiscalOfficerScraper's interface for run.py.
    Exposes fetch_parcels() returning raw hit dicts for _fo_upsert_parcels().
    fetch_and_parse() is present only to satisfy the ListingScraper ABC; run.py
    uses the registry path (fetch_parcels + upsert_parcels) and never calls it.

    Full sweep (~353K parcels at 500/page = ~707 requests) takes ~12+ minutes
    at 1 req/sec. For proof-of-concept, pass max_parcels=500 to the constructor.
    """

    site_name = "Shelby County Parcels (ReGIS)"

    def __init__(
        self,
        max_parcels: int | None = None,
        page_size: int = _PAGE_SIZE,
    ) -> None:
        """
        Args:
            max_parcels: Cap total parcels fetched (None = full sweep). Set to
                         a small number (e.g. 500) for dry-run / testing.
            page_size:   Records per ArcGIS request (max 1000; default 500).
        """
        self.max_parcels = max_parcels
        self.page_size = min(page_size, 1000)

    async def fetch_parcels(self) -> list[dict[str, Any]]:
        """Paginate through ReGIS CurrentParcels and return parsed parcel dicts.

        Returns raw dicts in the shape _fo_upsert_parcels expects. Every row
        has parcel_number set to the canonical 14-char TN form.
        """
        ssl_ctx = _regis_ssl_context()
        headers = {
            "User-Agent": random_ua(),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
        }

        all_parcels: list[dict[str, Any]] = []
        seen: set[str] = set()
        offset = 0
        skipped_no_id = 0

        async with httpx.AsyncClient(
            verify=ssl_ctx,
            headers=headers,
            timeout=_TIMEOUT,
            follow_redirects=True,
        ) as client:
            while True:
                if self.max_parcels is not None and len(all_parcels) >= self.max_parcels:
                    logger.info(
                        "ShelbyParcels: reached max_parcels cap (%d), stopping.",
                        self.max_parcels,
                    )
                    break

                params = {
                    "where": "1=1",
                    "outFields": _OUT_FIELDS,
                    "returnGeometry": "false",
                    "resultOffset": str(offset),
                    "resultRecordCount": str(self.page_size),
                    "f": "json",
                }

                try:
                    resp = await client.get(_QUERY_URL, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    logger.error(
                        "ShelbyParcels: fetch failed at offset=%d: %s", offset, exc
                    )
                    break

                if "error" in data:
                    logger.error(
                        "ShelbyParcels: ArcGIS error at offset=%d: %s",
                        offset, data["error"],
                    )
                    break

                features = data.get("features", [])
                if not features:
                    logger.info("ShelbyParcels: no more features at offset=%d — done.", offset)
                    break

                for feat in features:
                    attrs = feat.get("attributes", {})
                    parcel = _attrs_to_parcel(attrs)
                    if parcel is None:
                        skipped_no_id += 1
                        continue
                    pn = parcel["parcel_number"]
                    if pn in seen:
                        continue
                    seen.add(pn)
                    all_parcels.append(parcel)

                    if self.max_parcels is not None and len(all_parcels) >= self.max_parcels:
                        break

                logger.info(
                    "ShelbyParcels: page offset=%d fetched %d features, total so far=%d",
                    offset, len(features), len(all_parcels),
                )

                if len(features) < self.page_size:
                    # Last page — server returned fewer than the batch ceiling
                    break

                offset += self.page_size
                # Lay-low: 1 req/sec
                await asyncio.sleep(_PAGE_DELAY)

        logger.info(
            "ShelbyParcels: sweep complete — %d unique parcels, %d skipped (no id).",
            len(all_parcels), skipped_no_id,
        )
        return all_parcels

    async def fetch_and_parse(self) -> list[RawListing]:
        """Intentionally disabled — this is a registry source, not a listing feed.

        Calling this would emit ~353K RawListings into tranchi.listings, which is
        wrong: the spine belongs in tranchi.parcels only. run.py routes this scraper
        through the registry path (fetch_parcels + _fo_upsert_parcels) and never
        calls fetch_and_parse(). If you see this error, the dispatch in run.py is
        broken — check the 'shelby_parcels' elif branch in _run_scraper.
        """
        raise NotImplementedError(
            "ShelbyParcelsScraper is a registry source — use fetch_parcels() "
            "via the registry path in run.py, not fetch_and_parse()."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run proof
# ─────────────────────────────────────────────────────────────────────────────

async def _dry_run_sample(n: int = 25) -> None:
    """Fetch a small sample of parcels and print them. Proof of parse correctness."""
    import json

    print(f"\n=== Shelby County Parcels — dry-run sample (first {n}) ===\n")
    scraper = ShelbyParcelsScraper(max_parcels=n, page_size=n)
    parcels = await scraper.fetch_parcels()
    print(f"Fetched {len(parcels)} parcels\n")

    for p in parcels[:10]:
        print(
            f"  parcel_number={p['parcel_number']!r:16s}  "
            f"class={p['property_class'] or '?':3s}  "
            f"luc={p['land_use_code'] or '?':5s}  "
            f"owner={str(p['owner_name'])[:30]:30s}  "
            f"situs={str(p['situs_address'])[:45]}"
        )

    print("\n--- Full dict for first parcel ---")
    if parcels:
        print(json.dumps({k: v for k, v in parcels[0].items() if v is not None}, indent=2))


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=logging.INFO)
    asyncio.run(_dry_run_sample(25))
