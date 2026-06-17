"""
Detroit Blight Tickets scraper — writes to tranchi.signals (NOT tranchi.listings).

Blight violation notices issued by Detroit's Department of Appeals and Hearings
(DAH). An unpaid ticket (amt_balance_due > 0) signals an owner who has stopped
maintaining and stopped paying — city blight debt attaches to the parcel and
stacked tickets precede tax-foreclosure and motivated/distressed sale.

INVARIANT — ticket_issued_date IS PARTIALLY CORRUPT: some rows carry impossible
years (e.g. '8535-09-25', '3822-10-01', '3035-04-30'). NEVER use ticket_issued_date
for freshness, sorting, or the delta watermark. Use ticket_updated_at (a true
esriFieldTypeDate, refreshed near-real-time) as the delta watermark and for
observed_at. Use hearing_date / judgment_date for adjudication timing only.

Source: City of Detroit, Department of Appeals and Hearings — blight_tickets
hosted FeatureServer (City of Detroit AGO org qvkbeam7Wirps6zC). Public access,
no auth. Probed live 2026-06-11.
  https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest/services/blight_tickets/FeatureServer/0

INGEST SCOPE: only tickets with amt_balance_due > 0 (~362K of 888K as of 2026-06-11).
Paid/dismissed tickets (balance = 0) carry no distress weight and are excluded via
a server-side WHERE. This is Marc's distress thesis filter, not a data-quality filter.

DELTA strategy: incremental on ticket_updated_at. last_run_date (ISO string) passed
at construction; delta WHERE adds `ticket_updated_at >= <last_run - 1 day>` to buffer
source ETL lag. None = full backfill of the balance>0 slice. full=True (or no
last_run_date) triggers the full re-pull for the monthly rescan that catches
back-dated disposition changes.

PARCEL FORMAT: Detroit 8-digit + trailing period e.g. '12004687.' or with hyphen-range
'02000185-6'. Pass parcel_id VERBATIM to normalize_parcel_number() — the Wayne branch
preserves the trailing period/suffix as identity-bearing characters; never pre-strip.

Multiple tickets per parcel are expected and correct — aggregate is done downstream.
Do NOT deduplicate by parcel here; emit one RawSignal per ticket.

Field map: Clients/Marc/tranchi/research/wayne-blight-violations-field-map.md
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.scrapers.arcgis_client import count_features, query_features
from app.scrapers.base import SignalScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawSignal

logger = logging.getLogger(__name__)

# ── FeatureServer endpoint ─────────────────────────────────────────────────────

_FEATURE_SERVER_URL = (
    "https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest/services"
    "/blight_tickets/FeatureServer/0"
)

# Server maxRecordCount is 1000; do not exceed or the server silently truncates.
_BATCH_SIZE = 1000

# Base WHERE: only unpaid/outstanding tickets (the ~362K distress slice).
# Paid/dismissed tickets (balance = 0) are excluded by design — not a data-quality filter.
_BASE_WHERE = "amt_balance_due > 0"

_OUT_FIELDS = ",".join([
    "OBJECTID",
    "ticket_number",
    "parcel_id",
    "address",
    "zip_code",
    "ordinance_description",
    "disposition",
    "hearing_date",
    "judgment_date",
    "amt_fine",
    "amt_judgment",
    "amt_balance_due",
    "payment_status",
    "collection_status",
    "property_owner_name",
    "property_owner_address",
    "property_owner_city",
    "property_owner_state",
    "property_owner_zip_code",
    "neighborhood",
    "council_district",
    "ticket_updated_at",
    "longitude",
    "latitude",
])


def _safe_float(v: Any) -> float | None:
    """Parse a value to float, returning None on blank/None/non-numeric sentinels.

    A single bad row with a sentinel ('N/A', '-', '') must not abort the sweep —
    never call float() bare on a source field.
    """
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _ms_to_datetime(ms: int | None) -> datetime | None:
    """Convert ArcGIS epoch-milliseconds to a UTC-aware datetime, or None."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _ms_to_date_str(ms: int | None) -> str | None:
    """Convert ArcGIS epoch-milliseconds to an ISO date string (YYYY-MM-DD), or None."""
    dt = _ms_to_datetime(ms)
    return dt.date().isoformat() if dt is not None else None


def _coerce_date_str(v: Any) -> str | None:
    """Return a clean ISO date string from either an epoch-ms int or a bare 'YYYY-MM-DD'
    string, guarding against None/empty. hearing_date and judgment_date on this layer
    are served as DateOnly strings (not epoch-ms), but we guard both forms defensively.
    """
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return _ms_to_date_str(int(v))
    s = str(v).strip()
    return s if s else None


class WayneBlightScraper(SignalScraper):
    """Detroit DAH blight violation tickets as pre-distress signals.

    Emits one RawSignal per ticket with amt_balance_due > 0. Multiple signals
    per parcel are expected and intentional — aggregate is done downstream by
    the signal-stack join.

    To use:
        scraper = WayneBlightScraper(last_run_date="2026-06-01")  # delta
        scraper = WayneBlightScraper()                            # full backfill
        signals = await scraper.fetch_signals()
    """

    # MUST match the string wired into market_config source_meta and _DIM_MAP.
    site_name = "Detroit Open Data — Blight Tickets"
    signal_source = "detroit_blight_tickets"

    def __init__(
        self,
        *,
        last_run_date: str | None = None,
        full: bool = False,
    ) -> None:
        """
        Args:
            last_run_date: ISO date string (YYYY-MM-DD) of the previous successful
                run. When set, adds `ticket_updated_at >= <last_run - 1 day>` to the
                WHERE for an incremental delta pull. Ignored when full=True.
            full: Force a full re-pull of the balance>0 slice (monthly rescan for
                back-dated disposition changes). Takes precedence over last_run_date.
        """
        self.last_run_date = last_run_date
        self.full = full

    def _build_where_clause(self) -> str:
        """Build the ArcGIS SQL WHERE clause combining balance filter + optional delta."""
        if self.full or self.last_run_date is None:
            return _BASE_WHERE

        from datetime import date
        cutoff = date.fromisoformat(self.last_run_date) - timedelta(days=1)
        # ArcGIS DATE literal syntax for a true esriFieldTypeDate column.
        return f"{_BASE_WHERE} AND ticket_updated_at >= DATE '{cutoff.isoformat()}'"

    async def fetch_signals(self) -> list[RawSignal]:
        """Fetch blight tickets and return as RawSignal objects.

        Counts total first (for verification logging), then paginates in 1000-row
        batches. Rows missing parcel_id are skipped. One RawSignal per ticket.
        """
        where = self._build_where_clause()
        logger.info(
            "WayneBlightScraper: fetching tickets (where=%r, full=%s, last_run_date=%r)",
            where, self.full, self.last_run_date,
        )

        total = await count_features(_FEATURE_SERVER_URL, where=where)
        logger.info("WayneBlightScraper: %d tickets to fetch", total)

        signals: list[RawSignal] = []
        skipped_no_parcel = 0

        async for batch in query_features(
            _FEATURE_SERVER_URL,
            where=where,
            out_fields=_OUT_FIELDS,
            batch_size=_BATCH_SIZE,
        ):
            for attrs in batch:
                sig = self._to_signal(attrs)
                if sig is None:
                    skipped_no_parcel += 1
                    continue
                signals.append(sig)

        logger.info(
            "WayneBlightScraper: parsed %d signals from %d tickets (%d skipped no-parcel)",
            len(signals), total, skipped_no_parcel,
        )
        return signals

    def _to_signal(self, attrs: dict[str, Any]) -> RawSignal | None:
        """Convert one blight ticket attribute dict to a RawSignal.

        Returns None for rows with no parcel_id (cannot be signal-stacked).
        """
        parcel_raw: str | None = attrs.get("parcel_id")
        if not parcel_raw or not str(parcel_raw).strip():
            logger.debug(
                "WayneBlightScraper: skipping OBJECTID=%s — no parcel_id",
                attrs.get("OBJECTID"),
            )
            return None

        # Pass verbatim — the Wayne branch of normalize_parcel_number preserves the
        # trailing period/suffix that distinguishes Detroit parcels. Never pre-strip.
        parcel_number = normalize_parcel_number(str(parcel_raw).strip()) or str(parcel_raw).strip()

        # observed_at = ticket_updated_at (the stable per-ticket idempotency anchor).
        # DO NOT use ticket_issued_date — it is partially corrupt (impossible years).
        updated_dt = _ms_to_datetime(attrs.get("ticket_updated_at"))
        observed_at = updated_dt if updated_dt is not None else datetime.now(tz=timezone.utc)

        # Build owner mailing address for absentee detection (mail addr != situs addr).
        owner_parts = [
            attrs.get("property_owner_address"),
            attrs.get("property_owner_city"),
            attrs.get("property_owner_state"),
            attrs.get("property_owner_zip_code"),
        ]
        owner_mailing = ", ".join(p for p in owner_parts if p) or None

        payload: dict[str, Any] = {
            "ticket_number": attrs.get("ticket_number"),
            "violation_type": attrs.get("ordinance_description"),
            "disposition": attrs.get("disposition"),
            "amt_balance_due": _safe_float(attrs.get("amt_balance_due")),
            "amt_fine": _safe_float(attrs.get("amt_fine")),
            "amt_judgment": _safe_float(attrs.get("amt_judgment")),
            "payment_status": attrs.get("payment_status"),
            "collection_status": attrs.get("collection_status"),
            "property_owner_name": attrs.get("property_owner_name"),
            "owner_mailing_address": owner_mailing,
            "address": attrs.get("address"),
            "zip_code": attrs.get("zip_code"),
            "hearing_date": _coerce_date_str(attrs.get("hearing_date")),
            "judgment_date": _coerce_date_str(attrs.get("judgment_date")),
            "neighborhood": attrs.get("neighborhood"),
            "council_district": attrs.get("council_district"),
            "lat": _safe_float(attrs.get("latitude")),
            "lng": _safe_float(attrs.get("longitude")),
        }

        return RawSignal(
            parcel_number=parcel_number,
            signal_type="blight_violation",
            source="detroit_blight_tickets",
            observed_at=observed_at,
            confidence=1.0,  # official city record (DAH)
            payload=payload,
        )


# ── Standalone dry-run proof ───────────────────────────────────────────────────

async def _dry_run_sample(n: int = 25) -> None:
    """Fetch a small live sample and print results. No DB writes."""
    import json

    print(f"\n=== Detroit Blight Tickets — dry-run sample (first {n}) ===\n")
    print(f"Endpoint: {_FEATURE_SERVER_URL}")
    print(f"WHERE:    {_BASE_WHERE}  (amt_balance_due > 0 distress slice)\n")

    scraper = WayneBlightScraper()  # full backfill, first N tickets
    signals: list[RawSignal] = []
    skipped = 0

    where = scraper._build_where_clause()
    total = await count_features(_FEATURE_SERVER_URL, where=where)
    print(f"Total qualifying tickets (server count): {total}\n")

    # Fetch just one page to get sample rows without paginating ~362 pages
    from app.scrapers.arcgis_client import query_features as _qf
    async for batch in _qf(
        _FEATURE_SERVER_URL,
        where=where,
        out_fields=_OUT_FIELDS,
        batch_size=n,
    ):
        for attrs in batch:
            sig = scraper._to_signal(attrs)
            if sig is None:
                skipped += 1
                continue
            signals.append(sig)
        break  # first page only

    print(f"Parsed {len(signals)} signals from first page ({skipped} skipped no-parcel)\n")
    print(f"{'parcel':<16}  {'disposition':<35}  {'balance':>8}  {'collection':<15}  owner")
    print("-" * 110)
    for sig in signals[:n]:
        p = sig.payload
        disposition = (p.get("disposition") or "")[:34]
        collection = (p.get("collection_status") or "")[:14]
        owner = (p.get("property_owner_name") or "")[:30]
        balance = p.get("amt_balance_due")
        print(
            f"  {sig.parcel_number:<14}  {disposition:<35}  {balance!s:>8}  {collection:<15}  {owner}"
        )

    if signals:
        print("\n--- Full RawSignal for first ticket ---")
        first = signals[0]
        print(f"parcel_number : {first.parcel_number!r}")
        print(f"signal_type   : {first.signal_type!r}")
        print(f"source        : {first.source!r}")
        print(f"observed_at   : {first.observed_at.isoformat()!r}")
        print(f"confidence    : {first.confidence}")
        print(f"payload       :")
        print(json.dumps(first.payload, indent=4, default=str))


if __name__ == "__main__":
    import asyncio
    import logging as _logging
    _logging.basicConfig(level=logging.INFO)
    asyncio.run(_dry_run_sample(25))
