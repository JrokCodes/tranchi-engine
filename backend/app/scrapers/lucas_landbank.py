"""
Lucas County (OH / Toledo) Land Bank inventory — BUY-NOW listings -> tranchi.listings.

The Toledo-Lucas County Land Reutilization Corporation ("Lucas County Land Bank") holds
~460 parcels acquired through tax foreclosure / forfeiture, available for acquisition
(side-lot, rehab, redevelopment). Source: the Lucas County Auditor's public hosted ArcGIS
CAMA layer (Hosted/CAMA_Parcel_Thematics/9), filtered to owner = the land bank — the same
robust public GIS surface as the forfeited-land + vacant-delinquent feeds, NOT the thin
~12-property broker-listed Framer site (which is only the actively-marketed subset).

This mirrors the other markets' land-bank buy-now (Summit Tolemi 489, Shelby ePropertyPlus
2186, Wayne DLBA 1368): the full owned inventory, queried from the county's own data.

Plain ListingScraper (buy_now), addresses in the feed, no pool. full-rescan (a parcel that
leaves land-bank ownership — sold/transferred — drops out and is retired by absence).

INVARIANT: owner match is `ownernme1 LIKE '%REUTILIZATION%'` (catches the several spelling
variants: CORP / CORPORATION / abbreviated). LUC 770 = "E - COUNTY LAND REUTILIZATION".
"""
from __future__ import annotations

import logging
from typing import Any

from app.scrapers.arcgis_client import query_features
from app.scrapers.base import ListingScraper
from app.scrapers.db import canonical_address, canonical_city, normalize_parcel_for_market
from app.scrapers.models import RawListing

logger = logging.getLogger(__name__)

SITE_NAME = "Lucas Land Bank"   # == source_site in market_config
_MARKET = "lucas"

_LAYER = (
    "https://lcaudgis.co.lucas.oh.us/gisaudserver/rest/services/"
    "Hosted/CAMA_Parcel_Thematics/FeatureServer/9"
)
_WHERE = "ownernme1 LIKE '%REUTILIZATION%'"
_OUT = "parcelid,siteaddress,sitecity,sitezip,ownernme1,totalvalue,usedc,classdscrp"


def _money(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class LucasLandBankScraper(ListingScraper):
    """Lucas County Land Bank owned inventory (Auditor CAMA GIS) as buy-now listings."""

    site_name = SITE_NAME

    async def fetch_and_parse(self) -> list[RawListing]:
        listings: list[RawListing] = []
        seen: set[str] = set()
        rows = 0
        try:
            async for batch in query_features(_LAYER, where=_WHERE, out_fields=_OUT, batch_size=1000):
                for a in batch:
                    rows += 1
                    raw = str(a.get("parcelid") or "").strip()
                    parcel = normalize_parcel_for_market(raw, _MARKET)
                    addr = str(a.get("siteaddress") or "").strip()
                    if not parcel or not addr or parcel in seen:
                        continue
                    seen.add(parcel)
                    city = str(a.get("sitecity") or "").strip()
                    listings.append(RawListing(
                        source_site=SITE_NAME,
                        source_listing_id=parcel,
                        signal_type="land_bank_inventory",
                        property_address=canonical_address(addr) or addr,
                        property_city=(canonical_city(city) if city else None),
                        property_county="Lucas",
                        property_state="OH",
                        property_zip=(str(a.get("sitezip") or "").strip() or None),
                        appraised_value_usd=_money(a.get("totalvalue")),
                        status="active",
                    ))
        except Exception as exc:  # noqa: BLE001
            logger.error("LucasLandBank: GIS query failed: %s", exc)
            return []
        logger.info("LucasLandBank: %d listings from %d rows", len(listings), rows)
        return listings


if __name__ == "__main__":
    import asyncio
    import logging as _l
    import sys

    _l.basicConfig(level=_l.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)

    async def _dry() -> None:
        ls = await LucasLandBankScraper().fetch_and_parse()
        print(f"\nLand bank listings: {len(ls)}")
        for l in ls[:6]:
            print(f"  {l.property_address!r:32} {l.property_city!r:10} parcel={l.source_listing_id} val={l.appraised_value_usd}")

    asyncio.run(_dry())
