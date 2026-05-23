"""
Cleveland Code Violations scraper — writes to tranchi.signals (not tranchi.listings).

Violations are per-parcel distress signals, not standalone listings. Two ArcGIS
FeatureServer datasets are scraped from the Cleveland Open Data portal:

  PRIMARY — Building Complaint Violation Notices (one row per complaint/violation):
    https://services3.arcgis.com/dty2kHktVXHrqO8i/arcgis/rest/services/
      Complaint_Violation_Notices/FeatureServer/0
    Total rows: ~32,700   maxRecordCount: 1000   Discovered: 2026-05-23

  SUPPLEMENTAL (optional, large) — Building Violation Status History (one row per
  workflow task step; same RECORD_ID shares many rows):
    https://services3.arcgis.com/dty2kHktVXHrqO8i/arcgis/rest/services/
      Violation_Status_History/FeatureServer/0
    Total rows: ~485,500  maxRecordCount: 1000   Discovered: 2026-05-23

Both services are owned by opendataCLE (org ID: dty2kHktVXHrqO8i), public access,
no authentication required.

INVARIANT: tranchi.signals has a FK → tranchi.parcels(parcel_number). This scraper
upserts a stub row into tranchi.parcels before writing any signal for that parcel,
satisfying the FK without requiring fiscal_officer to run first. Stub rows are
superseded when fiscal_officer populates full parcel detail later.

PARCEL NUMBER FORMAT NOTE: Cleveland violations store parcel_number as an 8-digit
string (e.g. "01602062"), not the DDD-NN-NNN display format used by MyPlace and
Sheriff Sales. The raw 8-digit form is stored as-is. Future join logic should
normalize to a common format before cross-source matching.

SCRAPE STRATEGY:
  - First run (backfill): where=1=1 — pulls all records since ~2015 in 1000-row pages.
  - Subsequent runs: where=FILE_DATE >= '<last_run_date - 1 day>' for delta pulls.
    The 1-day buffer guards against records that arrive in the source slightly after
    their nominal FILE_DATE (city ETL lag). last_run_date is persisted in
    tranchi.scrape_runs by the orchestrator; this scraper reads it from there.
  - Both datasets use resultOffset pagination with batch_size=1000 (server cap).

DISCOVERED VIA:
  opendata.arcgis.com/api/v3/datasets?q=Building+Violation+Cleveland
  Dataset IDs: ec88c469a2794ac689315ed18b26912f_0 (Status History)
               (Complaint Violation Notices — same org, different service name)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg

from app.scrapers._time import today_et, n_days_ago_et
from app.scrapers.arcgis_client import count_features, query_features
from app.scrapers.base import SignalScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawSignal

logger = logging.getLogger(__name__)

# ── FeatureServer endpoints ────────────────────────────────────────────────────

_VIOLATION_NOTICES_URL = (
    "https://services3.arcgis.com/dty2kHktVXHrqO8i/arcgis/rest/services"
    "/Complaint_Violation_Notices/FeatureServer/0"
)

_STATUS_HISTORY_URL = (
    "https://services3.arcgis.com/dty2kHktVXHrqO8i/arcgis/rest/services"
    "/Violation_Status_History/FeatureServer/0"
)

# Server cap — both layers return maxRecordCount=1000. Setting batch_size above
# this causes the server to silently truncate pages. Keep at 1000.
_BATCH_SIZE = 1000


def _ms_to_datetime(ms: int | None) -> datetime | None:
    """Convert ArcGIS epoch-milliseconds to a UTC-aware datetime, or None."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _format_status(raw_status: str | None) -> str:
    """Normalize VIOLATION_APP_STATUS to one of Open | Closed | Pending Compliance.

    The Complaint Violation Notices layer uses free-text statuses. We bucket them
    into three canonical values for consistent signal payload filtering.
    """
    if not raw_status:
        return "Unknown"
    s = raw_status.strip().lower()
    if "closed" in s:
        return "Closed"
    if any(kw in s for kw in ("pending", "packet", "reinspect", "await", "court stat")):
        return "Pending Compliance"
    return "Open"


def _parse_address(full_address: str | None) -> tuple[str | None, str | None]:
    """Split 'NUMBER STREET, CLEVELAND, OH, ZIP' into (street_address, zip).

    The ArcGIS address field includes city/state/zip. We extract the street
    portion and ZIP for the payload; the city is always Cleveland for this dataset.
    """
    if not full_address:
        return None, None
    parts = [p.strip() for p in full_address.split(",")]
    street = parts[0] if parts else full_address
    zip_code: str | None = None
    if len(parts) >= 4:
        zip_code = parts[3].strip() or None
    return street, zip_code


class CodeViolationsScraper(SignalScraper):
    """Scrape Cleveland building code violations from two ArcGIS FeatureServer datasets.

    Primary source: Complaint Violation Notices (~32K rows, one per complaint).
    The Status History dataset (~485K task-step rows) is NOT fetched by default
    because it would require ~486 round-trips and is redundant for signal purposes.
    Signals from the Notices layer are sufficient for the distress-score stack.

    To enable Status History fetch (e.g. for task-level audit), set
    include_status_history=True at construction.
    """

    site_name = "code_violations"

    def __init__(
        self,
        *,
        include_status_history: bool = False,
        last_run_date: str | None = None,
    ) -> None:
        """
        Args:
            include_status_history: If True, also fetch the 485K-row Status History
                layer. Adds significant latency (~8 min on first run). Default: False.
            last_run_date: ISO date string (YYYY-MM-DD) of the previous successful
                run. When set, fetches only records with FILE_DATE >= that date minus
                1 day. When None, fetches all records (full backfill).
        """
        self.include_status_history = include_status_history
        self.last_run_date = last_run_date

    def _build_where_clause(self) -> str:
        """Build the ArcGIS SQL WHERE clause for delta vs. full pull."""
        if self.last_run_date is None:
            return "1=1"
        # ArcGIS DATE literals use TIMESTAMP format. Pull from 1 day before
        # last_run_date to buffer for source ETL lag.
        from datetime import date, timedelta
        cutoff = date.fromisoformat(self.last_run_date) - timedelta(days=1)
        # ArcGIS SQL: DATE 'YYYY-MM-DD' syntax
        return f"FILE_DATE >= DATE '{cutoff.isoformat()}'"

    async def fetch_signals(self) -> list[RawSignal]:
        """Fetch violation notices and return as RawSignal objects.

        First fetches total count (for logging), then paginates through all
        records in 1000-row batches. Each complaint notice becomes one signal row.
        """
        where = self._build_where_clause()
        logger.info(
            "CodeViolationsScraper: fetching Complaint Violation Notices "
            "(where=%r, include_status_history=%s)",
            where, self.include_status_history,
        )

        # Count first — gives us the ground-truth total for verification logging
        total = await count_features(_VIOLATION_NOTICES_URL, where=where)
        logger.info("CodeViolationsScraper: %d Complaint Violation Notices to fetch", total)

        signals: list[RawSignal] = []

        async for batch in query_features(
            _VIOLATION_NOTICES_URL,
            where=where,
            out_fields=(
                "OBJECTID,RECORD_ID,FILE_DATE,PARCEL_NUMBER,PRIMARY_ADDRESS,"
                "SOURCE,VIOLATION_NUMBER,VIOLATION_APP_STATUS,"
                "VIOLATION_ACCELA_CITIZEN_ACCESS_URL,"
                "DW_Neighborhood,DW_Ward,LON,LAT"
            ),
            batch_size=_BATCH_SIZE,
        ):
            for attrs in batch:
                signal = self._parse_notice(attrs)
                if signal is not None:
                    signals.append(signal)

        logger.info(
            "CodeViolationsScraper: parsed %d signals from %d fetched notices",
            len(signals), total,
        )

        if self.include_status_history:
            history_signals = await self._fetch_status_history(where)
            signals.extend(history_signals)
            logger.info(
                "CodeViolationsScraper: appended %d Status History signals (total=%d)",
                len(history_signals), len(signals),
            )

        return signals

    def _parse_notice(self, attrs: dict[str, Any]) -> RawSignal | None:
        """Convert a Complaint Violation Notices attribute dict to a RawSignal.

        Returns None for rows with no PARCEL_NUMBER (cannot be signal-stacked).
        """
        parcel_raw: str | None = attrs.get("PARCEL_NUMBER")
        if not parcel_raw or not parcel_raw.strip():
            logger.debug(
                "CodeViolationsScraper: skipping OBJECTID=%s — no PARCEL_NUMBER",
                attrs.get("OBJECTID"),
            )
            return None

        parcel_number = parcel_raw.strip()

        # FILE_DATE is ArcGIS epoch-milliseconds
        file_dt = _ms_to_datetime(attrs.get("FILE_DATE"))
        observed_at = file_dt if file_dt is not None else datetime.now(tz=timezone.utc)

        street_address, zip_code = _parse_address(attrs.get("PRIMARY_ADDRESS"))
        raw_status = attrs.get("VIOLATION_APP_STATUS") or ""
        canonical_status = _format_status(raw_status)

        payload: dict[str, Any] = {
            "record_id": attrs.get("RECORD_ID"),
            "violation_number": attrs.get("VIOLATION_NUMBER"),
            "violation_description": None,  # not present in Notices layer; in Status History
            "status": canonical_status,
            "status_raw": raw_status,
            "open_date": file_dt.date().isoformat() if file_dt else None,
            "close_date": None,  # not a field in Notices layer
            "address": street_address,
            "address_full": attrs.get("PRIMARY_ADDRESS"),
            "zip": zip_code,
            "complaint_source": attrs.get("SOURCE"),
            "neighborhood": attrs.get("DW_Neighborhood"),
            "ward": attrs.get("DW_Ward"),
            "lat": attrs.get("LAT"),
            "lng": attrs.get("LON"),
            "accela_url": attrs.get("VIOLATION_ACCELA_CITIZEN_ACCESS_URL"),
        }

        return RawSignal(
            parcel_number=parcel_number,
            signal_type="code_violation",
            source="cleveland_open_data",
            observed_at=observed_at,
            confidence=1.0,   # official city record
            payload=payload,
        )

    async def _fetch_status_history(self, where: str) -> list[RawSignal]:
        """Fetch Building Violation Status History task steps as supplemental signals.

        Each task step on a violation produces a signal row with signal_type
        'code_violation_task'. Only use when include_status_history=True.
        """
        total = await count_features(_STATUS_HISTORY_URL, where=where)
        logger.info(
            "CodeViolationsScraper: fetching %d Status History rows "
            "(~%d pages at batch_size=%d)",
            total, (total + _BATCH_SIZE - 1) // _BATCH_SIZE, _BATCH_SIZE,
        )

        history_signals: list[RawSignal] = []

        async for batch in query_features(
            _STATUS_HISTORY_URL,
            where=where,
            out_fields=(
                "OBJECTID,RECORD_ID,FILE_DATE,PRIMARY_ADDRESS,PARCEL_NUMBER,"
                "TASK_NAME,TASK_STATUS,TASK_DATE,TYPE_OF_VIOLATION,"
                "OCCUPANCY_OR_USE,ISSUE_DATE,LON,LAT"
            ),
            batch_size=_BATCH_SIZE,
        ):
            for attrs in batch:
                signal = self._parse_history_row(attrs)
                if signal is not None:
                    history_signals.append(signal)

        return history_signals

    def _parse_history_row(self, attrs: dict[str, Any]) -> RawSignal | None:
        """Convert a Status History attribute dict to a RawSignal."""
        parcel_raw: str | None = attrs.get("PARCEL_NUMBER")
        if not parcel_raw or not parcel_raw.strip():
            return None

        parcel_number = parcel_raw.strip()

        task_dt = _ms_to_datetime(attrs.get("TASK_DATE"))
        file_dt = _ms_to_datetime(attrs.get("FILE_DATE"))
        issue_dt = _ms_to_datetime(attrs.get("ISSUE_DATE"))
        observed_at = task_dt or file_dt or datetime.now(tz=timezone.utc)

        task_status: str = (attrs.get("TASK_STATUS") or "").strip()
        is_resolved = any(
            kw in task_status.lower()
            for kw in ("resolved", "closed", "case closed", "not needed")
        )

        street_address, _ = _parse_address(attrs.get("PRIMARY_ADDRESS"))

        payload: dict[str, Any] = {
            "record_id": attrs.get("RECORD_ID"),
            "task_name": attrs.get("TASK_NAME"),
            "task_status": task_status,
            "task_date": task_dt.date().isoformat() if task_dt else None,
            "violation_type": attrs.get("TYPE_OF_VIOLATION"),
            "violation_description": attrs.get("OCCUPANCY_OR_USE"),
            "status": "Closed" if is_resolved else "Open",
            "open_date": file_dt.date().isoformat() if file_dt else None,
            "issue_date": issue_dt.date().isoformat() if issue_dt else None,
            "address": street_address,
            "lat": attrs.get("LAT"),
            "lng": attrs.get("LON"),
        }

        return RawSignal(
            parcel_number=parcel_number,
            signal_type="code_violation_task",
            source="cleveland_open_data",
            observed_at=observed_at,
            confidence=1.0,
            payload=payload,
        )


async def count_total(where: str = "1=1") -> int:
    """Return total Complaint Violation Notices count. Used by dry-run / verification."""
    return await count_features(_VIOLATION_NOTICES_URL, where=where)


async def upsert_signals(
    pool: asyncpg.Pool,
    signals: list[RawSignal],
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Write RawSignal rows to tranchi.signals.

    Idempotency key: (parcel_number, signal_type, source, observed_at::date).
    Two signals from the same source, same parcel, same type, same calendar day
    are treated as the same event — UPDATE rather than INSERT.

    NOTE: This requires a unique index on (parcel_number, signal_type, source,
    (observed_at::date)) in tranchi.signals. If that index does not exist yet
    (migration 001 does not create it), each run will INSERT duplicates. Add the
    index via:
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tranchi_signals_natural_key
            ON tranchi.signals (parcel_number, signal_type, source, (observed_at::date));

    Before each signal insert, upserts a stub row into tranchi.parcels to satisfy
    the FK constraint. Stub rows are superseded when fiscal_officer populates full
    parcel detail later (ON CONFLICT DO NOTHING on parcels).

    Returns a dict with keys: inserted, updated, skipped, errors.
    """
    import json as _json

    if dry_run:
        logger.info("[DRY RUN] Would upsert %d signal rows (no DB writes)", len(signals))
        return {"inserted": 0, "updated": 0, "skipped": 0, "errors": 0}

    counters = {"inserted": 0, "updated": 0, "skipped": 0, "errors": 0}

    async with pool.acquire() as conn:
        for sig in signals:
            try:
                # Normalize to display format (DDD-NN-NNN) so cross-source parcel
                # joins work. Cleveland Open Data uses compact 8-digit format.
                parcel_number = normalize_parcel_number(sig.parcel_number) or sig.parcel_number

                # Stub-upsert into tranchi.parcels to satisfy FK.
                # ON CONFLICT DO NOTHING: if fiscal_officer has already populated
                # the full parcel record, we leave it untouched.
                await conn.execute(
                    """
                    INSERT INTO tranchi.parcels (parcel_number)
                    VALUES ($1)
                    ON CONFLICT (parcel_number) DO NOTHING
                    """,
                    parcel_number,
                )

                # Upsert signal using the natural idempotency key.
                # Requires unique index uq_tranchi_signals_natural_key (migration 003).
                result = await conn.fetchrow(
                    """
                    INSERT INTO tranchi.signals
                        (parcel_number, signal_type, source, observed_at, confidence, payload)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                    ON CONFLICT (parcel_number, signal_type, source, ((observed_at AT TIME ZONE 'UTC')::date))
                    DO UPDATE SET
                        last_seen_at = NOW(),
                        confidence   = EXCLUDED.confidence,
                        payload      = EXCLUDED.payload
                    RETURNING (xmax = 0) AS is_insert
                    """,
                    parcel_number,
                    sig.signal_type,
                    sig.source,
                    sig.observed_at,
                    sig.confidence,
                    _json.dumps(sig.payload),
                )
                if result and result["is_insert"]:
                    counters["inserted"] += 1
                else:
                    counters["updated"] += 1

            except Exception as exc:
                logger.error(
                    "Error upserting signal parcel=%r type=%r: %s",
                    sig.parcel_number, sig.signal_type, exc,
                )
                counters["errors"] += 1

    return counters
