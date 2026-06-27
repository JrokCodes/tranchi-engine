"""
Dayton / Montgomery County OH — Code Violation / Blight SIGNAL scraper.

Writes to tranchi.signals (pre-distress signal, signal_type='code_violation').
market='dayton'. site_name='Montgomery Code Violations' (Wave-0 canonical — verbatim).

INVARIANTS (non-obvious constraints — read before editing):
  1. ACCELA DELTA FREEZES STATUS: a complaint fetched on 2026-06-01 as OPEN will stay
     OPEN in the DB forever if we never re-fetch it — the RECORD_DATE delta only pulls
     NEW records, not updated ones. Daily `--full` rescan (WHERE 1=1) is REQUIRED so
     status updates (OPEN → CLOSED/ABATED) propagate into the payload and the gate_sql
     correctly retires resolved violations. Cron: `45 6 * * *  run.py --site
     dayton_code_violations --full`.
  2. HCS LEADING NON-BREAKING SPACE: HCS Grade_Description values have a U+00A0 prefix
     ('\xa0Sound', '\xa0Minor Repair', etc.). Always strip() before storing — Python
     str.strip() covers U+00A0 since isspace() is True. Verified live 2026-06-27.
  3. TAXPINNO NORMALIZATION: both Accela and HCS carry TAXPINNO in spaced PRINT_KEY form
     (e.g. 'R72 07711 0027'). Always normalize via the global normalize_parcel_number()
     before joining or writing — never compare raw forms directly (three delimiter
     variants exist: spaced, hyphenated, and no-delimiter).

Sources:
  A. Accela live (PRIMARY — freshness):
       https://maps.daytonohio.gov/gisservices/rest/services/Accela_UPDATES/
         AccelaIncidents_UPDATE/MapServer/0
     ~12,095 rows (rolling/live); ~5,987 OPEN/ACTIVE (verified 2026-06-27).
     maxRecordCount=2000. RECORD_DATE = esriFieldTypeDate (epoch-ms).
     Delta-pull on RECORD_DATE; `--full` re-pulls all rows to refresh STATUS.

  B. HCS-2023 structural-condition backbone (severity, point-in-time):
       https://services2.arcgis.com/3dDB2Kk6kuA2gIGw/arcgis/rest/services/
         Dayton_Housing_Condition_Survey_Parcels_2023/FeatureServer/0
     66,033 parcels; Grade_Code 3-5 (Major/Rehab/Dilapidated) = 5,894 blight-tier
     parcels (verified 2026-06-27). maxRecordCount=2000. Full-pull always (static
     2023 survey). Leading U+00A0 on Grade_Description — see INVARIANT 2.

  C. Overlays (ARPA Nuisance Points, Water-Zero vacancy proxy) — DEFERRED to v2.
     Low row count / stale snapshots; v1 bar cleared without them.

Payload keys consumed by market_config gate_sql:
  status       : 'OPEN'|'ACTIVE'|'CLOSED'|'ABATED'|None  (ILIKE 'open'/'active' gate)
  hcs_grade    : int|None  (::numeric >= 3 gate)
  address_full : str|None  (distress_lead_rules address_key)

Emit strategy:
  - Delta run (last_run_date set, full_rescan=False):
      Accela WHERE RECORD_DATE >= (last_run_date - 1 day). Emit OPEN/ACTIVE only.
      HCS fetched as a merge-dict; HCS-only standalone signals NOT emitted (we do not
      know which HCS parcels are covered by pre-existing Accela DB signals).
  - Full rescan (last_run_date=None OR full_rescan=True):
      Accela WHERE 1=1. Emit ALL statuses (CLOSED/ABATED included so they refresh
      payload.status in existing DB rows — the gate then rejects them).
      HCS grade>=3 parcels with no Accela row → standalone HCS-only signals emitted.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.scrapers.arcgis_client import count_features, query_features
from app.scrapers.base import SignalScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawSignal

logger = logging.getLogger(__name__)

# ── Endpoints ─────────────────────────────────────────────────────────────────

_ACCELA_URL = (
    "https://maps.daytonohio.gov/gisservices/rest/services"
    "/Accela_UPDATES/AccelaIncidents_UPDATE/MapServer/0"
)

_HCS_URL = (
    "https://services2.arcgis.com/3dDB2Kk6kuA2gIGw/arcgis/rest/services"
    "/Dayton_Housing_Condition_Survey_Parcels_2023/FeatureServer/0"
)

# Both services report maxRecordCount=2000 (verified live 2026-06-27).
_BATCH_SIZE = 2000

# Accela fields to request. FullAddress gives canonical mixed-case situs used as
# address_full (the distress_lead_rules address_key). ADDRESS is retained as the
# raw upstream value. LUC_Int / LUC_Description come from the address join within
# the Accela MapServer layer (present in every live row, verified).
_ACCELA_FIELDS = ",".join([
    "OBJECTID",
    "COMPLAINT_NO",
    "RECORD_DATE",
    "COMPLAINT_TYPE",
    "DESCRIPTION",
    "STATUS",
    "ACTION_TAKEN",
    "ADDRESS",
    "FullAddress",
    "CITY",
    "ZIP",
    "NEIGHBORHOOD",
    "TAXPINNO",
    "LUC_Int",
    "LUC_Description",
])

# HCS fields to request. APPRBLDG is the appraised building value (may be a
# formatted string — always pass through _safe_float()).
_HCS_FIELDS = ",".join([
    "OBJECTID",
    "TAXPINNO",
    "PARLOC",
    "OWNER_NAME",
    "APPRBLDG",
    "LUC_Description",
    "Grade_Code",
    "Grade_Description",
    "Status_Description",
    "NEIGHBORHOOD",
])

# HCS observed_at: fixed 2023-01-01 UTC for all standalone HCS signals.
# The natural idempotency key (parcel, type, source, observed_at::date) stays
# stable so each full rescan UPDATE-upserts the same row rather than inserting a
# new one per run. last_seen_at tracks freshness (updated on every conflict-UPDATE).
_HCS_OBSERVED_AT = datetime(2023, 1, 1, tzinfo=timezone.utc)

# Accela STATUS values that represent an unresolved / active complaint.
_OPEN_STATUSES: frozenset[str] = frozenset({"OPEN", "ACTIVE"})

# Accela STATUS values that indicate the complaint is resolved.
_CLOSED_STATUSES: frozenset[str] = frozenset({"CLOSED", "ABATED"})


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ms_to_datetime(ms: int | None) -> datetime | None:
    """Convert ArcGIS epoch-milliseconds to a UTC-aware datetime, or None."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OSError, ValueError, OverflowError):
        return None


def _safe_float(v: Any) -> float | None:
    """Coerce to float, stripping commas and whitespace. Returns None on failure.

    APPRBLDG and other numeric fields in these ArcGIS layers may be formatted
    strings (e.g. '42,810') — the dayton_parcels INVARIANT applies here too.
    """
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _trim_grade_desc(raw: str | None) -> str | None:
    """Strip the leading U+00A0 (non-breaking space) from HCS Grade_Description.

    The live HCS FeatureServer returns '\xa0Sound', '\xa0Minor Repair', etc.
    Python str.strip() covers U+00A0 because chr(0xa0).isspace() is True in
    CPython 3. Confirmed by live hex verification 2026-06-27 (c2a0 prefix).
    """
    if not raw:
        return None
    cleaned = raw.strip()
    return cleaned or None


def _str(v: Any) -> str | None:
    """Coerce to stripped string, returning None for empty/None values."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# ── Scraper ───────────────────────────────────────────────────────────────────


class DaytonCodeViolationsScraper(SignalScraper):
    """Dayton/Montgomery County code-violation and blight SIGNAL scraper.

    Two-layer fetch joined on TAXPINNO (= parcel spine PRINT_KEY):
      A. Accela live complaints — rolling freshness, delta-pulled on RECORD_DATE.
      B. HCS-2023 structural survey — Grade_Code 3-5 = blight tier (severity score).

    HCS data is merged into each Accela signal payload when the TAXPINNO overlaps.
    Parcels with grade>=3 but no Accela complaint emit a standalone HCS-only signal
    on full rescan (delta runs skip HCS-only to avoid DB blindness about coverage).

    Lifecycle:
      Delta (last_run_date set, full_rescan=False):
        • Accela WHERE RECORD_DATE >= last_run_date - 1 day. OPEN/ACTIVE only.
        • HCS fetched as a merge-dict; no standalone HCS signals.
      Full rescan (last_run_date=None OR full_rescan=True):
        • Accela WHERE 1=1 ALL statuses. CLOSED/ABATED signals update payload.status
          in existing DB rows so the gate_sql correctly retires resolved violations.
        • HCS grade>=3 parcels not covered by any Accela row → standalone HCS signals.
    """

    site_name = "Montgomery Code Violations"
    signal_source = "montgomery_code_violations"

    def __init__(
        self,
        *,
        last_run_date: str | None = None,
        full_rescan: bool = False,
    ) -> None:
        """
        Args:
            last_run_date: ISO date (YYYY-MM-DD) of the previous successful run.
                When set (and full_rescan=False), only Accela records with
                RECORD_DATE >= (last_run_date - 1 day) are fetched. The 1-day
                buffer guards against source ETL lag. When None, all Accela rows
                are fetched (first-run backfill or explicit full pull).
            full_rescan: Force a full pull of all Accela rows regardless of
                last_run_date. Required daily (see INVARIANT 1 in module docstring).
        """
        self.last_run_date = last_run_date
        self.full_rescan = full_rescan

    @property
    def _is_full(self) -> bool:
        """True when this run should pull all Accela rows (not just the delta)."""
        return self.last_run_date is None or self.full_rescan

    def _accela_where(self) -> str:
        """Build the ArcGIS SQL WHERE clause for the Accela layer.

        Full pull: WHERE 1=1 (all ~12K rows).
        Delta: WHERE RECORD_DATE >= DATE 'YYYY-MM-DD' using the ArcGIS DATE literal
        syntax for esriFieldTypeDate fields, with a 1-day buffer for ETL lag.
        """
        if self._is_full:
            return "1=1"
        cutoff: date = date.fromisoformat(self.last_run_date) - timedelta(days=1)
        return f"RECORD_DATE >= DATE '{cutoff.isoformat()}'"

    async def _fetch_hcs(self) -> dict[str, dict[str, Any]]:
        """Fetch HCS grade>=3 parcels and return a lookup dict.

        Returns {normalized_TAXPINNO → attribute_dict} for the ~5,894 blight-tier
        parcels (Grade_Code 3-5). Always a full pull (static 2023 survey).
        Used to merge severity data into Accela signals and to emit HCS-only signals
        on full rescan.
        """
        hcs_where = "Grade_Code >= 3"
        total = await count_features(_HCS_URL, where=hcs_where)
        logger.info(
            "DaytonCodeViolations: HCS grade>=3 total=%d",
            total,
        )

        hcs_map: dict[str, dict[str, Any]] = {}

        async for batch in query_features(
            _HCS_URL,
            where=hcs_where,
            out_fields=_HCS_FIELDS,
            batch_size=_BATCH_SIZE,
        ):
            for attrs in batch:
                raw_pin: str | None = _str(attrs.get("TAXPINNO"))
                if not raw_pin:
                    continue
                norm = normalize_parcel_number(raw_pin)
                if norm:
                    hcs_map[norm] = attrs

        logger.info(
            "DaytonCodeViolations: HCS map built — %d unique normalized parcels",
            len(hcs_map),
        )
        return hcs_map

    def _parse_accela(
        self,
        attrs: dict[str, Any],
        hcs_map: dict[str, dict[str, Any]],
        *,
        is_full: bool,
    ) -> RawSignal | None:
        """Convert one Accela attribute dict to a RawSignal, or None to skip.

        Skips rows with no TAXPINNO (cannot be spine-joined).
        Delta: skips CLOSED/ABATED rows (only new open complaints are actionable).
        Full rescan: emits ALL statuses so payload.status refreshes in existing DB
        rows — the gate_sql then correctly excludes resolved violations.
        """
        raw_pin: str | None = _str(attrs.get("TAXPINNO"))
        if not raw_pin:
            logger.debug(
                "DaytonCodeViolations: skip Accela OBJECTID=%s — no TAXPINNO",
                attrs.get("OBJECTID"),
            )
            return None

        norm_pin = normalize_parcel_number(raw_pin)
        if not norm_pin:
            return None

        status: str = (_str(attrs.get("STATUS")) or "").upper()

        # Delta: drop CLOSED/ABATED — only new open complaints are worth adding.
        # Full rescan: emit ALL to refresh STATUS in the existing signal payload;
        # the market_config gate_sql filters them at lead-surfacing time.
        if not is_full and status in _CLOSED_STATUSES:
            return None

        record_dt = _ms_to_datetime(attrs.get("RECORD_DATE"))
        observed_at = record_dt if record_dt is not None else datetime.now(tz=timezone.utc)

        # Merge HCS severity for this parcel, if it has a grade>=3 record.
        hcs: dict[str, Any] | None = hcs_map.get(norm_pin)
        hcs_grade_raw = hcs.get("Grade_Code") if hcs else None
        hcs_grade: int | None = int(hcs_grade_raw) if hcs_grade_raw is not None else None

        payload: dict[str, Any] = {
            # ── Accela complaint fields ─────────────────────────────────────
            "complaint_no": _str(attrs.get("COMPLAINT_NO")),
            "complaint_type": _str(attrs.get("COMPLAINT_TYPE")),
            "description": _str(attrs.get("DESCRIPTION")),
            # status is the primary gate_sql key: ILIKE 'open' | ILIKE 'active'
            "status": status or None,
            "action_taken": _str(attrs.get("ACTION_TAKEN")),
            # ── Address ─────────────────────────────────────────────────────
            # address_full is the distress_lead_rules address_key for this market.
            # FullAddress (mixed-case canonical) > ADDRESS (all-caps raw).
            "address": _str(attrs.get("ADDRESS")),
            "address_full": _str(attrs.get("FullAddress")),
            "city": _str(attrs.get("CITY")),
            "zip": _str(attrs.get("ZIP")),
            "neighborhood": _str(attrs.get("NEIGHBORHOOD")),
            # ── Land-use (from Accela address-join layer) ───────────────────
            "luc_code": _safe_float(attrs.get("LUC_Int")),
            "luc_description": _str(attrs.get("LUC_Description")),
            # ── Date ────────────────────────────────────────────────────────
            "open_date": record_dt.date().isoformat() if record_dt else None,
            # ── Provenance ──────────────────────────────────────────────────
            "source_layer": "accela",
            # ── HCS severity (merged; None if parcel not in HCS grade>=3) ──
            # hcs_grade is the numeric gate_sql key: (payload->>'hcs_grade')::numeric >= 3
            "hcs_grade": hcs_grade,
            "hcs_grade_description": _trim_grade_desc(
                hcs.get("Grade_Description") if hcs else None
            ),
            "hcs_occupancy": _str(hcs.get("Status_Description") if hcs else None),
            "hcs_owner_name": _str(hcs.get("OWNER_NAME") if hcs else None),
            "hcs_parloc": _str(hcs.get("PARLOC") if hcs else None),
            "hcs_appraised_building": _safe_float(hcs.get("APPRBLDG") if hcs else None),
        }

        return RawSignal(
            parcel_number=norm_pin,
            signal_type="code_violation",
            source="montgomery_code_violations",
            observed_at=observed_at,
            confidence=1.0,   # official city Accela record
            payload=payload,
        )

    def _parse_hcs_only(
        self,
        norm_pin: str,
        attrs: dict[str, Any],
    ) -> RawSignal:
        """Build a standalone HCS signal for a grade>=3 parcel with no Accela complaint.

        Uses a stable observed_at (2023-01-01 UTC) so the natural idempotency key
        (parcel, type, source, observed_at::date) is constant across full rescans —
        each rescan UPDATE-upserts the same row and refreshes last_seen_at, rather
        than inserting a new row per run (delinquent_tax.py pattern).

        address_full is set to PARLOC (the auditor situs address in the HCS layer),
        satisfying the distress_lead_rules address_key requirement for lead surfacing.
        """
        grade_raw = attrs.get("Grade_Code")
        hcs_grade: int | None = int(grade_raw) if grade_raw is not None else None
        parloc: str | None = _str(attrs.get("PARLOC"))

        payload: dict[str, Any] = {
            # No Accela complaint for this parcel.
            "complaint_no": None,
            "complaint_type": None,
            "description": None,
            # status=None → ILIKE gate fails; hcs_grade >= 3 → numeric gate passes.
            "status": None,
            "action_taken": None,
            # address_full = PARLOC (auditor situs from HCS layer).
            "address": parloc,
            "address_full": parloc,
            "city": "Dayton",
            "zip": None,
            "neighborhood": _str(attrs.get("NEIGHBORHOOD")),
            "luc_code": None,
            "luc_description": _str(attrs.get("LUC_Description")),
            "open_date": None,
            "source_layer": "hcs",
            # hcs_grade is the sole gate_sql gate for this signal.
            "hcs_grade": hcs_grade,
            "hcs_grade_description": _trim_grade_desc(attrs.get("Grade_Description")),
            "hcs_occupancy": _str(attrs.get("Status_Description")),
            "hcs_owner_name": _str(attrs.get("OWNER_NAME")),
            "hcs_parloc": parloc,
            "hcs_appraised_building": _safe_float(attrs.get("APPRBLDG")),
        }

        return RawSignal(
            parcel_number=norm_pin,
            signal_type="code_violation",
            source="montgomery_code_violations",
            observed_at=_HCS_OBSERVED_AT,
            confidence=0.8,   # official 2023 survey; lower than live Accela
            payload=payload,
        )

    async def fetch_signals(self) -> list[RawSignal]:
        """Fetch all code-violation signals for Montgomery County / Dayton.

        Step 1: Fetch HCS grade>=3 lookup dict (always, for merge and HCS-only).
        Step 2: Fetch Accela with delta or full WHERE clause; emit signals.
        Step 3 (full only): Emit standalone HCS-only signals for grade>=3 parcels
                            not covered by any Accela complaint row.
        """
        is_full = self._is_full
        accela_where = self._accela_where()

        logger.info(
            "DaytonCodeViolations: fetch start — full_rescan=%s accela_where=%r",
            is_full, accela_where,
        )

        # ── Step 1: HCS grade>=3 lookup dict ─────────────────────────────────
        hcs_map = await self._fetch_hcs()

        # ── Step 2: Accela complaints ─────────────────────────────────────────
        accela_total = await count_features(_ACCELA_URL, where=accela_where)
        logger.info(
            "DaytonCodeViolations: Accela rows to fetch=%d (where=%r)",
            accela_total, accela_where,
        )

        signals: list[RawSignal] = []
        # Tracks normalized PINs with an Accela row (for HCS-only exclusion below).
        accela_covered: set[str] = set()
        accela_open_count = 0

        async for batch in query_features(
            _ACCELA_URL,
            where=accela_where,
            out_fields=_ACCELA_FIELDS,
            batch_size=_BATCH_SIZE,
        ):
            for attrs in batch:
                sig = self._parse_accela(attrs, hcs_map, is_full=is_full)
                raw_pin = _str(attrs.get("TAXPINNO"))
                if raw_pin:
                    norm = normalize_parcel_number(raw_pin)
                    if norm:
                        accela_covered.add(norm)
                if sig is not None:
                    signals.append(sig)
                    status_upper = (_str(attrs.get("STATUS")) or "").upper()
                    if status_upper in _OPEN_STATUSES:
                        accela_open_count += 1

        logger.info(
            "DaytonCodeViolations: Accela signals=%d (open/active=%d, unique_parcels=%d)",
            len(signals), accela_open_count, len(accela_covered),
        )

        # ── Step 3: HCS-only signals (full rescan only) ───────────────────────
        # Delta runs skip this because accela_covered only reflects the delta batch,
        # not all Accela signals already in the DB — we cannot tell which HCS parcels
        # are truly uncovered without a DB query (scraper has no pool in fetch_signals).
        if is_full:
            hcs_only_count = 0
            for norm_pin, hcs_attrs in hcs_map.items():
                if norm_pin not in accela_covered:
                    signals.append(self._parse_hcs_only(norm_pin, hcs_attrs))
                    hcs_only_count += 1
            logger.info(
                "DaytonCodeViolations: HCS-only grade>=3 signals=%d",
                hcs_only_count,
            )

        logger.info(
            "DaytonCodeViolations: total signals=%d",
            len(signals),
        )
        return signals


# ── Dry-run / verification helpers ────────────────────────────────────────────


async def count_accela_total(where: str = "1=1") -> int:
    """Return Accela total complaint count for the given WHERE clause."""
    return await count_features(_ACCELA_URL, where=where)


async def count_hcs_grade(min_grade: int = 3) -> int:
    """Return HCS parcel count with Grade_Code >= min_grade."""
    return await count_features(_HCS_URL, where=f"Grade_Code >= {min_grade}")

