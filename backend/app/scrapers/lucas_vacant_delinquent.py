"""
Lucas County (OH / Toledo) VACANT + tax-delinquent parcels — writes to tranchi.signals.

A VACANT parcel that owes back taxes is one of the strongest pre-distress signals there is —
an absentee/abandoned owner with a growing certified tax burden. Surfaced as a
distress_stage='distress_signal' LEAD (market_config lucas distress_lead_rules['vacant_delinquent'])
once its tranchi.distress_lead_types row is enabled.

SOURCE — the Lucas County Auditor's OWN hosted ArcGIS layer (PUBLIC, no auth, no ASP.NET,
no Cloudflare):
  Hosted/Vacant_Delinquent___100  FeatureServer/21  ("Vacant Delinquent >$100")
It joins the county tax duplicate (balance, taxdue, certified year) to the parcel record
(parid, owner, situs, land-use class). ~705 parcels; the county refreshes the hosted layer,
so this is a near-live feed — queryable exactly like the parcel spine (arcgis_client).

WHY THIS, vs the other tax sources (documented in LUCAS-PREDISTRESS-DEPLOY.md):
  - Column 19K full list: Cloudflare/JWT-blocked.
  - PublicNoticesOhio full list: annual (publishes ~late Oct), ASP.NET/ViewState, stale most of
    the year.
  - AREIS daily Access-DB: the only full-universe DAILY path, but login-gated.
  This GIS layer is the cleanest PUBLIC source. It is the VACANT delinquent SUBSET (not the full
  ~19K occupied+vacant roll), but vacant+delinquent is a higher-quality distress slice.

INVARIANTS:
  1. `parid` is the 7-digit Lucas PARID — normalize_parcel_for_market(raw, 'lucas'); FK-stubbed
     into tranchi.parcels by upsert_signals.
  2. payload uses the Lucas tax convention keys (`delq_amount`, `luc`) so the shared RULE #1
     gate fires unchanged: balance >= $2000 AND residential LUC 5xx.
  3. observed_at = monthly anchor (first of month, UTC). Idempotent monthly refresh; a parcel
     that CURES drops out of the layer, stops refreshing last_seen_at, and surface_distress
     retires its lead. No DB writes in this class.

Anchor (verified live 2026-06-23): parid 0701121 -> 518 TECUMSEH ST -> balance 2554.23, certyr 2020.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.scrapers.arcgis_client import query_features
from app.scrapers.base import SignalScraper
from app.scrapers.db import normalize_parcel_for_market
from app.scrapers.models import RawSignal

logger = logging.getLogger(__name__)

SITE_NAME = "Lucas Vacant Delinquent (GIS)"   # signal scrape_runs identity (distinct from the lead)
SIGNAL_SOURCE = "lucas_areis_vacant_delinquent"
_MARKET = "lucas"

_LAYER = (
    "https://lcaudgis.co.lucas.oh.us/gisaudserver/rest/services/"
    "Hosted/Vacant_Delinquent___100/FeatureServer/21"
)
_OUT_FIELDS = "parid,owner,property_address,mailing_address,luc,class,balance,taxdue,certyr"


def _num(v: Any) -> str:
    """Bare numeric string ('2554.23') or '' — keeps the gate regex '^[0-9.]+$' happy."""
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return ""


class LucasVacantDelinquentScraper(SignalScraper):
    """Lucas vacant + certified-delinquent parcels (Auditor GIS) as pre-distress signals.

    No DB pool needed — the parcel is the join key; situs/owner come from the spine later.
    Output flows through the signal path in run.py (fetch_signals -> upsert_signals).
    """

    site_name = SITE_NAME
    signal_source = SIGNAL_SOURCE

    async def fetch_signals(self) -> list[RawSignal]:
        now = datetime.now(timezone.utc)
        observed_at = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

        signals: list[RawSignal] = []
        seen: set[str] = set()
        rows = 0
        try:
            async for batch in query_features(
                _LAYER, where="1=1", out_fields=_OUT_FIELDS, batch_size=2000
            ):
                for a in batch:
                    rows += 1
                    raw = str(a.get("parid") or "").strip()
                    parcel = normalize_parcel_for_market(raw, _MARKET)
                    if not parcel or parcel in seen:
                        continue
                    seen.add(parcel)
                    payload: dict[str, Any] = {
                        "delq_amount": _num(a.get("balance")),
                        "taxdue": _num(a.get("taxdue")),
                        "luc": str(a.get("luc") or "").strip(),
                        "class": str(a.get("class") or "").strip(),
                        "owner": str(a.get("owner") or "").strip(),
                        "property_address": str(a.get("property_address") or "").strip(),
                        "taxbill_address": str(a.get("mailing_address") or "").strip(),
                        "certified_yr": str(a.get("certyr") or "").strip(),
                        "vacant": True,
                    }
                    signals.append(RawSignal(
                        parcel_number=parcel,
                        signal_type="vacant_delinquent",
                        source=SIGNAL_SOURCE,
                        observed_at=observed_at,
                        confidence=1.0,
                        payload=payload,
                    ))
        except Exception as exc:  # noqa: BLE001
            logger.error("LucasVacantDelinquent: GIS query failed: %s", exc)
            return []

        def _gated(s: RawSignal) -> bool:
            amt = s.payload.get("delq_amount") or ""
            try:
                a = float(amt)
            except ValueError:
                return False
            return a >= 2000 and (s.payload.get("luc") or "").startswith("5")

        gated = sum(1 for s in signals if _gated(s))
        logger.info(
            "LucasVacantDelinquent: %d signals from %d rows, %d GATED "
            "(residential LUC 5xx + balance >= $2000)",
            len(signals), rows, gated,
        )
        return signals


if __name__ == "__main__":
    import asyncio
    import json
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)

    async def _dry_run() -> None:
        print("\n=== Lucas Vacant Delinquent (Auditor GIS) dry-run ===\n")
        signals = await LucasVacantDelinquentScraper().fetch_signals()
        print(f"\nTotal signals: {len(signals)}")
        anchor = [s for s in signals if s.parcel_number == "0701121"]
        if anchor:
            print(f"ANCHOR 0701121: balance={anchor[0].payload['delq_amount']} "
                  f"luc={anchor[0].payload['luc']} addr={anchor[0].payload['property_address']!r}")
        for s in signals[:5]:
            print(json.dumps({"parcel": s.parcel_number, "observed_at": s.observed_at.isoformat(),
                              "payload": s.payload}, indent=2))
            print()

    asyncio.run(_dry_run())
