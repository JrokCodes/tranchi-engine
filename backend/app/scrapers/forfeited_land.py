"""
Cuyahoga Forfeited Land scraper — writes to tranchi.listings.

Stage 4 of the Ohio tax-distress pipeline, and the truest "tax deed": parcels that
failed to sell at the sheriff's tax-foreclosure auction are forfeited to the State of
Ohio and sold directly by the County Fiscal Officer for ~back-taxes + costs. This is
DISTINCT from the DLN delinquent-tax feed (dln.py, signal_type
'tax_delinquent_foreclosure'), which is Stage 3 — UPCOMING sheriff auctions. Forfeited
parcels are already forfeited and acquirable now, often the deepest discounts in the
pipeline (minimum bid ≈ taxes + costs).

Source: Cuyahoga County "Forfeited Lands Locator" ArcGIS web map. Data endpoint traced
through the web map's operational layers (layer 1 = parcels):
  https://services7.arcgis.com/GXM8JipKyc0m6HBi/arcgis/rest/services/
    2024_Forfeited_Land_Sale_Update/FeatureServer/1
Public ArcGIS Online hosted feature service — no auth, no EULA. Same REST query pattern
as code_violations.py via arcgis_client.query_features().

INVARIANT — YEAR-VERSIONED ENDPOINT: the service name embeds a cycle year
("2024_Forfeited_Land_Sale_Update", currently serving the "Forfeited Land Sale Parcels
- 2025" layer). Cuyahoga republishes a fresh service each cycle (~mid-summer, before the
Sept sale). When the new cycle publishes, this URL may 404 or go stale — re-discover the
current service from the Forfeited Lands page's web map and update _FEATURE_SERVER_URL.
A 404 yields found=0, which by design does NOT retire existing listings (the stale-mark
pass only runs for sources with found>0 — see run.py:_mark_stale_listings), so a missed
cycle is safe; the feed simply goes stale until the URL is refreshed.

INVARIANT — FORFEITED = STATE-OWNED: the deeded owner is literally "STATE OF OHIO FORF
CV # …". The deal is acquiring from the State at back-taxes (like Land Bank), NOT
contacting a distressed owner. We store the prior owner (grantor) as trustee_name for
reference only.

DEAL QUALITY (recon 2026-05-29, 291 parcels): all flagged foreclosure, none
redeemed/pulled. ~190 are 1-3 family residential, ~72 vacant lots, rest commercial.
172/291 have positive equity (market value > tax+costs); 35 strong (value ≥ 3× bid).
We ingest ALL valid parcels (Marc: "pull everything") and store opening_bid + appraised
so equity = appraised_value_usd - opening_bid_usd is the high-signal indicator the UI
sorts on. Field map: Clients/Marc/tranchi/research/forfeited-land-field-map.md.
"""
from __future__ import annotations

import logging
from typing import Any

from app.scrapers.arcgis_client import count_features, query_features
from app.scrapers.base import ListingScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawListing

logger = logging.getLogger(__name__)

SITE_NAME = "Cuyahoga Forfeited Land"

_FEATURE_SERVER_URL = (
    "https://services7.arcgis.com/GXM8JipKyc0m6HBi/arcgis/rest/services"
    "/2024_Forfeited_Land_Sale_Update/FeatureServer/1"
)
_BATCH_SIZE = 1000  # ~291 rows total — one page

# Field name constants (the service double-prefixes two joined tables; several
# L2EPV_* names are truncated by ArcGIS's field-name length limit).
_F_PARCEL_DISPLAY = "ForGIS__PROPERTY_"          # '008-24-064' (already display fmt)
_F_PARCEL_COMPACT = "L2EPV_Survey_Parcel_parcel_id"  # '00824064'
_F_ADDRESS = "ForGIS__ADDRESS"                   # '2469 Dobson CT '
_F_ADDR_ALL = "L2EPV_Survey_Parcel_par_addr_al"  # full 'NUM ST, CITY, OH, ZIP'
_F_CITY = "L2EPV_Survey_Parcel_parcel_city"
_F_ZIP = "L2EPV_Survey_Parcel_parcel_zip"
_F_TAX_AND_COSTS = "ForGIS__Tax_and_COSTS"       # opening bid
_F_MARKET = "L2EPV_Survey_Parcel_tax_market_"    # county market value
_F_CASE = "ForGIS__CASE_"                         # 'CV983792'
_F_GRANTOR = "ForGIS__GRANTOR1"                   # prior owner
_F_GRANTOR_ALT = "L2EPV_Survey_Parcel_grantor"
_F_REDEEMED = "ForGIS__Vacated_Redeemed_Pulled"  # filter: skip if != 0

_OUT_FIELDS = ",".join([
    _F_PARCEL_DISPLAY, _F_PARCEL_COMPACT, _F_ADDRESS, _F_ADDR_ALL, _F_CITY, _F_ZIP,
    _F_TAX_AND_COSTS, _F_MARKET, _F_CASE, _F_GRANTOR, _F_GRANTOR_ALT, _F_REDEEMED,
])


def _clean_str(v: Any) -> str | None:
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


class ForfeitedLandScraper(ListingScraper):
    """Cuyahoga forfeited-land parcels from the Fiscal Officer's ArcGIS locator.

    Plain ListingScraper — no pool needed (addresses are present in the feed; the
    post-run _ensure_parcels_for_listings stub + enrich_parcels cron fill the
    parcel registry). Output flows through the standard listing path in run.py.
    """

    site_name = SITE_NAME

    async def fetch_and_parse(self) -> list[RawListing]:
        total = await count_features(_FEATURE_SERVER_URL, where="1=1")
        logger.info("ForfeitedLand: %d parcels in the forfeited-land locator", total)

        listings: list[RawListing] = []
        skipped_redeemed = 0
        skipped_no_parcel = 0

        async for batch in query_features(
            _FEATURE_SERVER_URL, where="1=1", out_fields=_OUT_FIELDS, batch_size=_BATCH_SIZE,
        ):
            for attrs in batch:
                # Skip parcels that have been redeemed/pulled from the sale — no
                # longer acquirable (field is 0 for all currently-available rows).
                redeemed = attrs.get(_F_REDEEMED)
                if redeemed not in (0, None):
                    skipped_redeemed += 1
                    continue
                rl = self._to_listing(attrs)
                if rl is None:
                    skipped_no_parcel += 1
                    continue
                listings.append(rl)

        logger.info(
            "ForfeitedLand: %d listings parsed (%d skipped redeemed/pulled, %d skipped no-parcel)",
            len(listings), skipped_redeemed, skipped_no_parcel,
        )
        return listings

    def _to_listing(self, attrs: dict[str, Any]) -> RawListing | None:
        # Parcel: ForGIS__PROPERTY_ is already display format; fall back to the
        # compact 8-digit parcel_id normalized to DDD-NN-NNN.
        parcel = _clean_str(attrs.get(_F_PARCEL_DISPLAY))
        parcel = normalize_parcel_number(parcel) if parcel else None
        if not parcel:
            parcel = normalize_parcel_number(_clean_str(attrs.get(_F_PARCEL_COMPACT)))
        if not parcel:
            return None  # no parcel → cannot join/dedup; drop (the 2 null rows)

        # Address: prefer the clean ForGIS__ADDRESS, else first comma-part of the
        # full county address string.
        address = _clean_str(attrs.get(_F_ADDRESS))
        if not address:
            full = _clean_str(attrs.get(_F_ADDR_ALL))
            address = full.split(",")[0].strip() if full else None
        if not address:
            # Parcel-anchored placeholder so prefilter passes; parcel is the join key.
            city = _clean_str(attrs.get(_F_CITY))
            address = f"Parcel {parcel}" + (f", {city}" if city else "")

        zip_raw = attrs.get(_F_ZIP)
        property_zip = str(zip_raw).strip() if zip_raw not in (None, "") else None

        return RawListing(
            source_site=SITE_NAME,
            source_listing_id=parcel,
            case_number=_clean_str(attrs.get(_F_CASE)),
            signal_type="forfeited_land",
            property_address=address,
            property_city=_clean_str(attrs.get(_F_CITY)),
            property_county="Cuyahoga",
            property_state="OH",
            property_zip=property_zip,
            sale_date=None,  # annual sale, not per-parcel; stays acquirable → NULL
            opening_bid_usd=_to_float(attrs.get(_F_TAX_AND_COSTS)),
            appraised_value_usd=_to_float(attrs.get(_F_MARKET)),
            trustee_name=_clean_str(attrs.get(_F_GRANTOR)) or _clean_str(attrs.get(_F_GRANTOR_ALT)),
            status="active",
            auction_status="forfeited",
        )


async def count_total(where: str = "1=1") -> int:
    """Total forfeited-land parcel count. Used by dry-run / verification."""
    return await count_features(_FEATURE_SERVER_URL, where=where)
