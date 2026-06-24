"""
Lucas County (OH / Toledo) Forfeited Land — tax-deed BUY-NOW listings -> tranchi.listings.

The truest "tax deed": parcels that went through tax foreclosure, failed to sell at the
sheriff sale, were FORFEITED to the state, and are now available for direct purchase. Source:
the Lucas County Auditor's public hosted ArcGIS layer Hosted/Forfeited_Land_Sales
(FeatureServer/3) — the same public GIS surface as the vacant-delinquent + AREIS feeds.
full-rescan (absence => sold / redeemed). Mirrors cuyahoga forfeited_land.py.

INVARIANT — the Forfeited_Land_Sales layer is a STALE CATALOG, not a live outcome list.
Verified 2026-06-24: of 31 catalog rows, only 6 were still state-held; 14 were already
privately resold (e.g. GLC CUSTOMS LLC, with 2025-26 sale dates) and 11 had no live owner.
This is the exact 69%-already-sold trap cuyahoga/forfeited_land.py documents. Therefore
EVERY catalog row MUST be gated against the live AREIS spine owner (parcels.owner_name for
market='lucas') and kept ONLY when the current owner is still FORFEITED / STATE OF OHIO.
Trusting the catalog verbatim surfaces already-sold parcels as false buy-now listings.
Needs the DB pool for that gate (was previously a no-pool plain scraper).

The prior/record owner (grantor to contact) is stored as trustee_name.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.scrapers.arcgis_client import query_features
from app.scrapers.base import ListingScraper
from app.scrapers.db import canonical_address, canonical_city, normalize_parcel_for_market
from app.scrapers.models import RawListing

try:  # asyncpg only needed for the pool type hint
    import asyncpg
except Exception:  # pragma: no cover
    asyncpg = None  # type: ignore

logger = logging.getLogger(__name__)

SITE_NAME = "Lucas Forfeited Land"   # == source_site in market_config
_MARKET = "lucas"

# Current-owner markers that confirm a parcel is still state-held / forfeited (so still a
# genuine buy-now). Matches Lucas's "FORFEITED LAND" owner string + the generic state form.
_FORFEITED_OWNER_RE = re.compile(r"STATE OF OHIO|FORF", re.IGNORECASE)

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
    """Lucas forfeited-land tax-deed inventory (Auditor GIS) as buy-now listings.

    Gated against the live AREIS spine: a catalog row is emitted ONLY when the parcel's
    current spine owner is still FORFEITED / STATE OF OHIO (see module INVARIANT).
    """

    site_name = SITE_NAME

    def __init__(self, pool: "asyncpg.Pool | None" = None, dry_run: bool = False) -> None:
        self.pool = pool
        self.dry_run = dry_run

    async def _spine_forfeited(self, parcels: set[str]) -> set[str]:
        """Return the subset of `parcels` whose live spine owner is still state-held/forfeited.

        Returns the input set unchanged when there is no pool (dry-run without DB) so a
        local dry-run can still inspect the raw catalog; production always passes a pool.
        """
        if not parcels:
            return set()
        if self.pool is None:
            logger.warning("LucasForfeitedLand: no pool — owner gate SKIPPED (dry-run only)")
            return set(parcels)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT parcel_number, owner_name FROM tranchi.parcels "
                "WHERE market = $1 AND parcel_number = ANY($2::text[])",
                _MARKET, list(parcels),
            )
        return {
            r["parcel_number"]
            for r in rows
            if r["owner_name"] and _FORFEITED_OWNER_RE.search(r["owner_name"])
        }

    async def fetch_and_parse(self) -> list[RawListing]:
        candidates: dict[str, RawListing] = {}
        rows = 0
        try:
            async for batch in query_features(_LAYER, where="1=1", out_fields=_OUT, batch_size=1000):
                for a in batch:
                    rows += 1
                    raw = str(a.get(_F + "parcelid") or "").strip()
                    parcel = normalize_parcel_for_market(raw, _MARKET)
                    addr = str(a.get(_F + "siteaddress") or "").strip()
                    if not parcel or not addr or parcel in candidates:
                        continue
                    city = str(a.get(_F + "sitecity") or "").strip()
                    candidates[parcel] = RawListing(
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
                    )
        except Exception as exc:  # noqa: BLE001
            logger.error("LucasForfeitedLand: GIS query failed: %s", exc)
            return []

        # Spine-owner gate: drop catalog rows whose parcel has already been resold.
        still_forfeited = await self._spine_forfeited(set(candidates))
        listings = [candidates[p] for p in candidates if p in still_forfeited]
        dropped = len(candidates) - len(listings)
        logger.info(
            "LucasForfeitedLand: %d listings (kept) / %d dropped as already-sold / %d catalog rows",
            len(listings), dropped, rows,
        )
        return listings


if __name__ == "__main__":
    import asyncio
    import logging as _l
    import sys

    _l.basicConfig(level=_l.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)

    async def _dry() -> None:
        import os
        pool = None
        dsn = os.environ.get("DATABASE_URL", "")
        if dsn and asyncpg is not None:
            try:
                pool = await asyncpg.create_pool(
                    dsn.replace("postgresql+asyncpg://", "postgresql://"), min_size=1, max_size=2
                )
            except Exception as exc:  # noqa: BLE001
                print(f"(no pool — owner gate disabled: {exc})")
        ls = await LucasForfeitedLandScraper(pool=pool, dry_run=True).fetch_and_parse()
        print(f"\nForfeited-land listings (gated): {len(ls)}")
        for l in ls[:10]:
            print(f"  {l.property_address!r:34} {l.property_city!r:12} parcel={l.source_listing_id} "
                  f"owner={l.trustee_name!r} val={l.appraised_value_usd}")
        if pool is not None:
            await pool.close()

    asyncio.run(_dry())
