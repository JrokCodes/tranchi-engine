"""
Summit County (OH / Akron) Certified-Delinquent Tax Roll — writes to tranchi.signals.

The Summit County Fiscal Office publishes a monthly iasWorld bulk extract of every
parcel on the certified-delinquent roll (SC720_DELQ). This is the Stage-1 distress
signal for the Summit market — DLQ_AMOUNT is the cumulative balance including
penalties; old CERTIFIED_YR + out-of-state TAXBILL_ADDRESS = motivated/absentee.

Source: public, no auth, direct ZIP download (Joomla jdownloads). Verified live
2026-06-11 (tape 28-MAY-2026). ~28,363 rows; whole-file monthly replacement.

INVARIANTS (read before editing):
  - PARCEL is a STRING, always. Leading zeros are load-bearing ('0101379' is 7 chars).
    Int-casting ('0101379' → 101379) silently breaks the GIS spine join. normalize_parcel_number
    handles Summit's 7-digit zero-padded form — always call it, never skip it.
  - File format is CSV-inside-ZIP, not XLSX. Use stdlib zipfile + csv. Do NOT use openpyxl.
  - observed_at is derived from TAPE_CREATE_DATE (one value per file). This is the natural
    monthly dedup key: re-runs UPDATE in place rather than inserting 28K duplicate rows.
    A new monthly tape mints a new observed_at and replaces the prior month's signals.
  - ~26% of rows have blank PROPERTY_ADDRESS (vacant land / mineral / condo subs).
    Do NOT drop these rows — address resolves later via the GIS spine join.
  - Removal signal = parcel absent from the new monthly tape (cured / foreclosed / sold).
    The orchestrator diffs old vs new tape for cure events; this scraper only emits the
    current live roll.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import datetime, timezone
from typing import Any

import httpx

from app.scrapers.base import SignalScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawSignal
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

SITE_NAME = "Summit Delinquent Tax"
SIGNAL_SOURCE = "summit_fiscal_office"

_DOWNLOAD_URL = (
    "https://fiscaloffice.summitoh.net/index.php/documents-a-forms/finish/10-cama/282-sc720delq"
)
_ZIP_MEMBER = "SC720_DELQ.csv"
_TIMEOUT = 120.0  # ZIP is ~3–5 MB; generous for county server


def _clean_amount(raw: str | None) -> str:
    """Strip currency formatting from DLQ_AMOUNT/TAX_VAL and return a bare numeric string.

    Input examples: '$7,789.69', '7789.69', '  12,047.77 '
    Output: '7789.69'   (regex-safe; castable with ::numeric in SQL)
    Returns '' for blank/None so the gate_sql regex '^[0-9.]+$' skips blanks cleanly.
    """
    if not raw:
        return ""
    cleaned = raw.strip().lstrip("$").replace(",", "")
    return cleaned if cleaned else ""


def _parse_tape_date(raw: str | None) -> datetime:
    """Parse TAPE_CREATE_DATE ('28-MAY-2026') into a stable UTC midnight datetime.

    This is the idempotency anchor: all 28K rows in one tape share the same
    observed_at, so a monthly re-run updates in place rather than duplicating.
    Falls back to a sentinel date (2000-01-01) only when the field is unparseable
    so that a malformed tape still ingests rather than crashing.
    """
    if not raw:
        return datetime(2000, 1, 1, tzinfo=timezone.utc)
    try:
        return datetime.strptime(raw.strip(), "%d-%b-%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning("SummitDelinquentTax: unexpected TAPE_CREATE_DATE format %r — using sentinel", raw)
        return datetime(2000, 1, 1, tzinfo=timezone.utc)


class SummitDelinquentTaxScraper(SignalScraper):
    """Summit County certified-delinquent-tax parcels as pre-distress signals.

    No DB pool needed — signals tag by parcel; address comes from the GIS spine later.
    Output flows through the signal path in run.py (fetch_signals → upsert_signals,
    which normalizes the parcel number and stub-upserts tranchi.parcels for the FK).
    """

    site_name = SITE_NAME
    signal_source = SIGNAL_SOURCE  # run.py reads this for the active-count + dashboard

    async def fetch_signals(self) -> list[RawSignal]:
        headers = default_headers()
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            logger.info("SummitDelinquentTax: downloading %s", _DOWNLOAD_URL)
            try:
                resp = await client.get(_DOWNLOAD_URL, timeout=_TIMEOUT)
                resp.raise_for_status()
                zip_bytes = resp.content
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                logger.error("SummitDelinquentTax: download failed: %s", exc)
                return []

        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile as exc:
            logger.error("SummitDelinquentTax: response is not a valid ZIP: %s", exc)
            return []

        # Locate the CSV inside the ZIP — try the canonical name first, then any .csv member.
        names = zf.namelist()
        if _ZIP_MEMBER in names:
            csv_name = _ZIP_MEMBER
        else:
            csv_candidates = [n for n in names if n.upper().endswith(".CSV")]
            if not csv_candidates:
                logger.error("SummitDelinquentTax: no CSV found in ZIP (members: %s)", names)
                return []
            csv_name = csv_candidates[0]
            logger.warning(
                "SummitDelinquentTax: expected %r, found %r — using %r",
                _ZIP_MEMBER, names, csv_name,
            )

        with zf.open(csv_name) as raw_fh:
            csv_text = raw_fh.read().decode("utf-8", errors="replace")

        reader = csv.DictReader(io.StringIO(csv_text))

        # One stable observed_at anchors the entire tape (TAPE_CREATE_DATE is constant
        # across all rows in a given file). We read it from the first data row and reuse.
        tape_observed_at: datetime | None = None

        rows_seen = 0
        signals: list[RawSignal] = []

        for row in reader:
            parcel_raw = (row.get("PARCEL") or "").strip()
            if not parcel_raw:
                continue

            rows_seen += 1

            # Determine the stable observed_at once from the first row's TAPE_CREATE_DATE.
            if tape_observed_at is None:
                tape_observed_at = _parse_tape_date(row.get("TAPE_CREATE_DATE"))

            norm = normalize_parcel_number(parcel_raw)
            if not norm:
                continue

            # Build mailing address for skip-trace / absentee detection.
            tb_addr = (row.get("TAXBILL_ADDRESS") or "").strip()
            tb_city = (row.get("TAXBILL_CITY") or "").strip()
            tb_state = (row.get("TAXBILL_STATE") or "").strip()
            taxbill_address = ", ".join(p for p in [tb_addr, tb_city, tb_state] if p)

            district_name = (row.get("DISTRICT_NAME") or "").strip()
            district_no = (row.get("DISTRICT_NO") or "").strip()
            district = f"{district_name} ({district_no})" if district_no else district_name

            payload: dict[str, Any] = {
                "delq_amount": _clean_amount(row.get("DLQ_AMOUNT")),
                "luc": (row.get("LUC") or "").strip(),
                "certified_yr": (row.get("CERTIFIED_YR") or "").strip(),
                "owner": (row.get("OWNER") or "").strip(),
                "taxbill_address": taxbill_address,
                "property_address": (row.get("PROPERTY_ADDRESS") or "").strip(),
                "district": district,
            }

            signals.append(RawSignal(
                parcel_number=parcel_raw,   # raw 7-digit string; upsert_signals normalizes
                signal_type="tax_delinquent",
                source=SIGNAL_SOURCE,
                observed_at=tape_observed_at,
                confidence=1.0,
                payload=payload,
            ))

        logger.info(
            "SummitDelinquentTax: %d signals from %d rows (tape observed_at=%s)",
            len(signals), rows_seen,
            tape_observed_at.date() if tape_observed_at else "unknown",
        )
        return signals


if __name__ == "__main__":
    import asyncio

    ANCHOR_PARCEL = "0101379"  # EVERGREEN HOMES LLC, Joseph Ave, Barberton, ~$7,789.69

    async def _dry_run() -> None:
        scraper = SummitDelinquentTaxScraper()
        print("SummitDelinquentTaxScraper dry-run — downloading live tape...")
        signals = await scraper.fetch_signals()

        print(f"\nTotal signals: {len(signals)}  (expect ~28,000+)")

        # Anchor check: 0101379 must survive with leading zero intact.
        anchor_hits = [s for s in signals if s.parcel_number == ANCHOR_PARCEL]
        if anchor_hits:
            print(f"\nAnchor parcel {ANCHOR_PARCEL} FOUND (leading zero intact).")
            print(f"  payload: {anchor_hits[0].payload}")
        else:
            print(f"\nWARNING: anchor parcel {ANCHOR_PARCEL} NOT FOUND — check leading-zero handling.")

        # Print a few sample signals.
        print("\nSample RawSignals (first 3):")
        for sig in signals[:3]:
            print(f"  parcel_number : {sig.parcel_number!r}")
            print(f"  signal_type   : {sig.signal_type}")
            print(f"  source        : {sig.source}")
            print(f"  observed_at   : {sig.observed_at}")
            print(f"  confidence    : {sig.confidence}")
            print(f"  payload       : {sig.payload}")
            print()

        # Verify payload keys are complete.
        expected_keys = {"delq_amount", "luc", "certified_yr", "owner", "taxbill_address", "property_address", "district"}
        if signals:
            present = set(signals[0].payload.keys())
            missing = expected_keys - present
            extra = present - expected_keys
            if missing:
                print(f"WARNING: missing payload keys: {missing}")
            if extra:
                print(f"INFO: extra payload keys: {extra}")
            if not missing:
                print(f"Payload keys OK: {sorted(present)}")

    asyncio.run(_dry_run())
