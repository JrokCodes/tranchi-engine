"""
Lucas County (OH / Toledo) Forfeited Land — tax-deed BUY-NOW listings -> tranchi.listings.

The truest "tax deed": parcels that went through tax foreclosure, failed to sell at the
sheriff sale, were FORFEITED to the state, and are now available for direct purchase. Source:
the Lucas County Auditor's public hosted ArcGIS layer Hosted/Forfeited_Land_Sales
(FeatureServer/3) — the same public GIS surface as the vacant-delinquent + AREIS feeds.
~31 parcels; full-rescan (absence => sold / redeemed). Mirrors cuyahoga forfeited_land.py.

Plain ListingScraper (buy_now). Addresses are in the feed, no pool needed. The prior owner
(grantor to contact) is stored as trustee_name.
"""
from __future__ import annotations

import logging
from typing import Any

from app.scrapers.arcgis_client import query_features
from app.scrapers.base import ListingScraper
from app.scrapers.db import canonical_address, canonical_city, normalize_parcel_for_market
from app.scrapers.models import RawListing

logger = logging.getLogger(__name__)

SITE_NAME = "Lucas Forfeited Land"   # == source_site in market_config
_MARKET = "lucas"

_LAYER = (
    "https://lcaudgis.co.lucas.oh.us/gisaudserver/rest/services/"
    "Hosted/Forfeited_Land_Sales/FeatureServer/3"
)
_F = "lucas_parcels_cama_"   # the layer prefixes its CAMA-join fields
_OUT = ",".join(_F + x for x in
                ["parcelid", "siteaddress", "sitecity", "sitezip", "ownernme1", "totalvalue"])


def _money(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class LucasForfeitedLandScraper(ListingScraper):
    """Lucas forfeited-land tax-deed inventory (Auditor GIS) as buy-now listings."""

    site_name = SITE_NAME

    async def fetch_and_parse(self) -> list[RawListing]:
        listings: list[RawListing] = []
        seen: set[str] = set()
        rows = 0
        try:
            async for batch in query_features(_LAYER, where="1=1", out_fields=_OUT, batch_size=1000):
                for a in batch:
                    rows += 1
                    raw = str(a.get(_F + "parcelid") or "").strip()
                    parcel = normalize_parcel_for_market(raw, _MARKET)
                    addr = str(a.get(_F + "siteaddress") or "").strip()
                    if not parcel or not addr or parcel in seen:
                        continue
                    seen.add(parcel)
                    city = str(a.get(_F + "sitecity") or "").strip()
                    listings.append(RawListing(
                        source_site=SITE_NAME,
                        source_listing_id=parcel,
                        signal_type="forfeited_land",
                        property_address=canonical_address(addr) or addr,
                        property_city=(canonical_city(city) if city else None),
                        property_county="Lucas",
                        property_state="OH",
                        property_zip=(str(a.get(_F + "sitezip") or "").strip() or None),
                        trustee_name=(str(a.get(_F + "ownernme1") or "").strip() or None),
                        appraised_value_usd=_money(a.get(_F + "totalvalue")),
                        status="active",
                    ))
        except Exception as exc:  # noqa: BLE001
            logger.error("LucasForfeitedLand: GIS query failed: %s", exc)
            return []
        logger.info("LucasForfeitedLand: %d listings from %d rows", len(listings), rows)
        return listings


if __name__ == "__main__":
    import asyncio
    import logging as _l
    import sys

    _l.basicConfig(level=_l.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)

    async def _dry() -> None:
        ls = await LucasForfeitedLandScraper().fetch_and_parse()
        print(f"\nForfeited-land listings: {len(ls)}")
        for l in ls[:6]:
            print(f"  {l.property_address!r:34} {l.property_city!r:12} parcel={l.source_listing_id} "
                  f"owner={l.trustee_name!r} val={l.appraised_value_usd}")

    asyncio.run(_dry())
