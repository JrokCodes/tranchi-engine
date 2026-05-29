"""
Cuyahoga Delinquent Tax scraper — writes to tranchi.signals (NOT tranchi.listings).

Stage 1 of the Ohio tax-distress pipeline: parcels with a CERTIFIED-DELINQUENT tax
balance (owner ≥1yr behind, pre/at foreclosure). This is a SIGNAL source — it tags
parcels with signal_type='tax_delinquent' so the read-time HOT engine
(routers/listings.py:_build_signal_types) stacks "Tax Distress" onto probate /
forfeited-land / foreclosure / land-bank listings. It deliberately does NOT write
listings — there are ~27K delinquent parcels county-wide and raw pre-foreclosure
delinquents (owner still in place) are low-signal as standalone deals; surfacing them
as a signal mints the "probate + tax deed combo" (someone died AND owes taxes → HOT)
without flooding the deal feed.

Source: Cuyahoga County Fiscal Office EPV (Enhanced Property Viewer) parcel service —
the county's own authoritative ~570K-parcel dataset behind MyPlace. Public ArcGIS
Feature Service, Query,Extract, maxRecordCount=5000, no auth. Same REST pattern as
code_violations.py via arcgis_client.query_features().
  https://gis.cuyahogacounty.us/server/rest/services/CCFO/EPV_Prod/FeatureServer/2

THE ≥1-YEAR FLOOR (Marc's requirement): total_net_delq_balance is the certified-
delinquent balance — by Ohio law taxes are certified delinquent only after going
unpaid past the collection cycle (≥1 year). So `total_net_delq_balance > 0` IS the
≥1yr floor. An exact year-count isn't in this layer; foreclosure_flag=1 marks the
higher-signal actively-foreclosing subset (5,454 county-wide).

INVARIANT — observed_at = Jan 1 of cur_tax_year (idempotency). The signals natural key
is (parcel_number, signal_type, source, observed_at::date). A STABLE per-tax-year
observed_at means re-runs UPDATE the same row (no daily 27K-row bloat); a fresh row is
created only when the tax year rolls. A paid-off parcel's signal persists (signals are
not retired) until the read-time freshness gate (RESUME-HERE N9) lands — multiple
tax_delinquent signals still collapse to ONE "Tax Distress" dimension, so no HOT
inflation. Field map: Clients/Marc/tranchi/research/delinquent-tax-field-map.md.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.scrapers.arcgis_client import count_features, query_features
from app.scrapers.base import SignalScraper
from app.scrapers.models import RawSignal

logger = logging.getLogger(__name__)

SITE_NAME = "Cuyahoga Delinquent Tax"
SIGNAL_SOURCE = "cuyahoga_fiscal_officer"

_FEATURE_SERVER_URL = (
    "https://gis.cuyahogacounty.us/server/rest/services/CCFO/EPV_Prod/FeatureServer/2"
)
# Certified-delinquent balance > 0 == the ≥1yr floor (see module docstring).
_WHERE = "total_net_delq_balance > 0"
_BATCH_SIZE = 5000  # service maxRecordCount

_OUT_FIELDS = ",".join([
    "parcelpin", "parcel_id", "total_net_delq_balance", "grand_total_balance",
    "foreclosure_flag", "cur_tax_year", "prev_tax_year", "tax_market_total",
    "parcel_owner", "par_addr_all",
])

# Fallback observed_at year if cur_tax_year is missing/garbage (keeps the natural key
# stable; a constant past Jan-1 still updates-in-place on re-run).
_FALLBACK_TAX_YEAR = 2024


def _to_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class DelinquentTaxScraper(SignalScraper):
    """County certified-delinquent parcels as tax-distress signals.

    Plain SignalScraper — output flows through the signal path in run.py
    (fetch_signals → _cv_upsert_signals, which normalizes the parcel to display
    format and stub-upserts tranchi.parcels to satisfy the signals FK).
    """

    site_name = SITE_NAME
    signal_source = SIGNAL_SOURCE  # run.py reads this for the active-count + dashboard

    async def fetch_signals(self) -> list[RawSignal]:
        total = await count_features(_FEATURE_SERVER_URL, where=_WHERE)
        logger.info("DelinquentTax: %d certified-delinquent parcels (where=%r)", total, _WHERE)

        signals: list[RawSignal] = []
        skipped_no_parcel = 0

        async for batch in query_features(
            _FEATURE_SERVER_URL, where=_WHERE, out_fields=_OUT_FIELDS, batch_size=_BATCH_SIZE,
        ):
            for attrs in batch:
                sig = self._to_signal(attrs)
                if sig is None:
                    skipped_no_parcel += 1
                    continue
                signals.append(sig)

        logger.info(
            "DelinquentTax: parsed %d signals from %d parcels (%d skipped no-parcel)",
            len(signals), total, skipped_no_parcel,
        )
        return signals

    def _to_signal(self, attrs: dict[str, Any]) -> RawSignal | None:
        # parcel_number is the raw value; _cv_upsert_signals normalizes to DDD-NN-NNN.
        parcel_raw = attrs.get("parcelpin") or attrs.get("parcel_id")
        if not parcel_raw or not str(parcel_raw).strip():
            return None
        parcel_number = str(parcel_raw).strip()

        # Stable per-tax-year observed_at for idempotency (see module INVARIANT).
        try:
            tax_year = int(attrs.get("cur_tax_year") or _FALLBACK_TAX_YEAR)
        except (TypeError, ValueError):
            tax_year = _FALLBACK_TAX_YEAR
        observed_at = datetime(tax_year, 1, 1, tzinfo=timezone.utc)

        payload: dict[str, Any] = {
            "delq_balance": _to_float(attrs.get("total_net_delq_balance")),
            "grand_total_balance": _to_float(attrs.get("grand_total_balance")),
            "foreclosure": bool(attrs.get("foreclosure_flag")),
            "cur_tax_year": tax_year,
            "prev_tax_year": attrs.get("prev_tax_year"),
            "market_value": _to_float(attrs.get("tax_market_total")),
            "owner": (attrs.get("parcel_owner") or None),
            "address": (attrs.get("par_addr_all") or None),
        }

        return RawSignal(
            parcel_number=parcel_number,
            signal_type="tax_delinquent",
            source=SIGNAL_SOURCE,
            observed_at=observed_at,
            confidence=1.0,  # official county record
            payload=payload,
        )


async def count_total(where: str = _WHERE) -> int:
    """Total certified-delinquent parcel count. Used by dry-run / verification."""
    return await count_features(_FEATURE_SERVER_URL, where=where)
