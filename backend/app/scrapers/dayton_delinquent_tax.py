"""
Montgomery County (OH / Dayton) Treasurer Delinquent Tax Roll — writes to tranchi.signals.

The Treasurer publishes a monthly full-snapshot ZIP of every parcel on the certified-
delinquent roll (Delq_YYYYMMDD.zip). This is the Stage-1 pre-distress signal lever for
the Dayton market — NETDELQ is the cumulative net balance; old CERTDLQYR + off-site mailing
address = motivated/absentee. Verified live: Delq_20260531.zip (2026-06-26).

Source: public, no auth, direct ZIP download. Index at fdpopup.cfm?dtype=DQ resolves the
latest month-end filename. ~25,739 rows; whole-file monthly replacement.

INVARIANTS (read before editing):
  - CSV is latin-1 encoded. DO NOT use UTF-8 — decode with encoding='latin-1'.
  - Field names in the header have trailing spaces ('PARCELID      ', 'NETDELQ         ').
    Always strip before use: reader.fieldnames = [f.strip() for f in reader.fieldnames].
    A bare row.get('NETDELQ') returns None because the real key is 'NETDELQ         '.
  - Use csv.DictReader (not naive line.split(',')). The LEGAL1/LEGAL2/LEGAL3 fields
    contain quoted commas; csv.DictReader handles them correctly — naive splitting would
    misalign all columns after the first bad comma (verified live: 5 rows have quoted
    commas in LEGAL fields; 0 unhandled overflow rows in practice).
  - NETDELQ is zero-padded ('000000001243.27') — no $ or commas. Strip and float-cast.
  - FRCLSR carries a foreclosure-filing DATE ('31-JUL-24') when set, not a boolean.
    Empty string = no foreclosure. Pass verbatim; the downstream gate uses its presence.
  - Payload-key CONTRACT with distress_lead_rules (market_config.py dayton market):
      delq_amount = NETDELQ (numeric string, 2dp, no leading zeros)
      cls         = CLS field verbatim (R/C/E/I/A/U)
    The gate_sql reads: s.payload->>'delq_amount' ::numeric >= 2000 AND cls ~ '^R'.
    RENAME EITHER KEY HERE → lead surfacing silently breaks. Do NOT rename them.
  - Monthly cadence. This scraper emits the full current roll. Cured/sold parcels
    are absent from the next tape; the orchestrator diffs old vs new for cure events.
  - observed_at is derived from the ZIP filename date (month-end). All ~25k rows in
    one tape share the same observed_at — re-runs UPDATE in place (not duplicates).

_SOURCE_META (verbatim from market_config.py — do not deviate):
  signal source_meta: "Montgomery Delinquent Tax" / "https://www.mcohio.org/..." / "signal"
  lead   source_meta: "Montgomery Tax Delinquent (Lead)" / ... / "lead"

distress_lead_types need:
  INSERT INTO tranchi.distress_lead_types
    (market, signal_type, enabled) VALUES ('dayton', 'tax_delinquent', false);
  Flip enabled=true only after buy-now is verified (G3). Ships DISABLED per G1 discipline.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from datetime import datetime, timezone
from typing import Any

import httpx

from app.scrapers.base import SignalScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawSignal
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

# ─── SOURCE META (Wave-0 canonical — verbatim) ───────────────────────────────
# site_name → scrape_runs.source_site + dashboard label
# signal_source → tranchi.signals.source (internal); also used by run.py active-count query
SITE_NAME = "Montgomery Delinquent Tax"
SIGNAL_SOURCE = "montgomery_treasurer"

# ─── ENDPOINTS ───────────────────────────────────────────────────────────────
_INDEX_URL = (
    "https://go.mcohio.org/applications/treasurer/search/fdpopup.cfm?dtype=DQ"
)
# Hrefs in the index page use Windows backslashes: data\Delq\Delq_YYYYMMDD.zip
_BASE_DOWNLOAD = "https://go.mcohio.org/applications/treasurer/search/"
_HREF_RE = re.compile(
    r"href=([^\"\'\s>]*Delq_(\d{8})\.zip)", re.IGNORECASE
)
_TIMEOUT = 180.0  # 38 MB CSV inside ~5 MB ZIP; generous for a county server


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _clean_amount(raw: str | None) -> str:
    """Strip currency formatting and zero-padding; return bare numeric string.

    Input examples: '000000001243.27', '000000000000.00', '$1,234.56'
    Output: '1243.27', '0.00', '1234.56'
    Returns '' for blank/None/unparseable — gate_sql '^[0-9.]+$' skips blanks.
    """
    if not raw:
        return ""
    cleaned = raw.strip().lstrip("$").replace(",", "")
    if not cleaned:
        return ""
    try:
        val = float(cleaned)
        return f"{val:.2f}"
    except ValueError:
        return ""


def _observed_at_from_filename(zip_name: str) -> datetime:
    """Extract the month-end date from the ZIP filename 'Delq_YYYYMMDD.zip'.

    This is the idempotency anchor: all ~25k rows in one tape share the same
    observed_at so a monthly re-run UPDATEs in place rather than inserting
    duplicates. Falls back to 2000-01-01 UTC sentinel if the filename is malformed.
    """
    m = re.search(r"Delq_(\d{8})\.zip", zip_name, re.IGNORECASE)
    if not m:
        logger.warning(
            "DaytonDelinquentTax: could not parse date from ZIP name %r — using sentinel",
            zip_name,
        )
        return datetime(2000, 1, 1, tzinfo=timezone.utc)
    raw_date = m.group(1)  # YYYYMMDD
    try:
        return datetime.strptime(raw_date, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning(
            "DaytonDelinquentTax: invalid date %r in ZIP name — using sentinel", raw_date
        )
        return datetime(2000, 1, 1, tzinfo=timezone.utc)


def _resolve_latest_zip_url(html: str) -> str | None:
    """Parse the DQ popup page and return the absolute URL of the latest ZIP.

    The popup page carries unquoted hrefs with Windows backslashes:
      <a href=data\\Delq\\Delq_20260531.zip>Delq as of 05/31/2026</a>
    We pick the highest YYYYMMDD found — that is the most current tape.
    """
    hits = _HREF_RE.findall(html)
    if not hits:
        return None
    # hits is list of (href_value, YYYYMMDD); sort descending by date
    hits.sort(key=lambda t: t[1], reverse=True)
    href, _date = hits[0]
    # Normalise backslashes to forward slashes for URL construction
    href_clean = href.replace("\\", "/")
    return _BASE_DOWNLOAD + href_clean


class DaytonDelinquentTaxScraper(SignalScraper):
    """Montgomery County certified-delinquent-tax parcels as pre-distress signals.

    No DB pool needed — signals tag by parcel; address comes from the AUDGIS spine
    later (address_source='spine' in distress_lead_rules). Output flows through the
    signal path in run.py (fetch_signals → upsert_signals, which normalises the parcel
    number and stub-upserts tranchi.parcels for the FK).

    Cadence: monthly (Treasurer publishes one month-end snapshot). The scraper is
    self-scheduled (SELF_SCHEDULED_SOURCES in market_config.py) — it runs via the
    dedicated monthly cron `30 7 1 * *`, NOT the every-3h full run.
    """

    site_name = SITE_NAME
    signal_source = SIGNAL_SOURCE  # run.py reads this for the active-count query

    async def fetch_signals(self) -> list[RawSignal]:
        headers = default_headers()
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:

            # ── Step 1: resolve the latest ZIP URL from the index page ────────
            zip_url: str | None = None
            try:
                logger.info("DaytonDelinquentTax: resolving latest ZIP from %s", _INDEX_URL)
                idx_resp = await client.get(_INDEX_URL, timeout=30.0)
                idx_resp.raise_for_status()
                zip_url = _resolve_latest_zip_url(idx_resp.text)
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                logger.warning(
                    "DaytonDelinquentTax: index page fetch failed (%s) — falling back to "
                    "known-good URL Delq_20260531.zip",
                    exc,
                )

            if zip_url is None:
                # Fallback: use the last verified URL from the field map (2026-06-26).
                zip_url = f"{_BASE_DOWNLOAD}data/Delq/Delq_20260531.zip"
                logger.warning(
                    "DaytonDelinquentTax: using fallback ZIP URL %s", zip_url
                )

            # ── Step 2: download the ZIP ───────────────────────────────────────
            logger.info("DaytonDelinquentTax: downloading %s", zip_url)
            try:
                resp = await client.get(zip_url, timeout=_TIMEOUT)
                resp.raise_for_status()
                zip_bytes = resp.content
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                logger.error("DaytonDelinquentTax: ZIP download failed: %s", exc)
                return []

        # ── Step 3: open the ZIP ───────────────────────────────────────────────
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile as exc:
            logger.error("DaytonDelinquentTax: response is not a valid ZIP: %s", exc)
            return []

        names = zf.namelist()
        csv_candidates = [n for n in names if n.upper().endswith(".CSV")]
        if not csv_candidates:
            logger.error(
                "DaytonDelinquentTax: no CSV found in ZIP (members: %s)", names
            )
            return []

        # Pick the canonical name if present, otherwise the first CSV found.
        zip_filename = zip_url.rsplit("/", 1)[-1]  # e.g. 'Delq_20260531.zip'
        expected_csv = zip_filename.replace(".zip", ".csv")  # 'Delq_20260531.csv'
        csv_name = expected_csv if expected_csv in names else csv_candidates[0]
        if csv_name != expected_csv:
            logger.warning(
                "DaytonDelinquentTax: expected %r, found %r — using %r",
                expected_csv, names, csv_name,
            )

        # ── Step 4: derive observed_at from the ZIP filename ──────────────────
        observed_at = _observed_at_from_filename(zip_filename)
        logger.info(
            "DaytonDelinquentTax: tape observed_at=%s (from %s)",
            observed_at.date(), zip_filename,
        )

        # Current year for years_delinquent computation
        current_year = observed_at.year

        # ── Step 5: decode and parse the CSV ─────────────────────────────────
        # INVARIANT: latin-1 encoding is required. The CSV header fields carry
        # trailing spaces ('NETDELQ         ') — strip via reader.fieldnames setter.
        # Use csv.DictReader (not line.split) — LEGAL fields contain quoted commas.
        with zf.open(csv_name) as raw_fh:
            csv_text = raw_fh.read().decode("latin-1")

        reader = csv.DictReader(io.StringIO(csv_text))
        # Strip trailing spaces from all header names (e.g. 'NETDELQ         ' → 'NETDELQ')
        reader.fieldnames = [f.strip() for f in (reader.fieldnames or [])]

        if not reader.fieldnames:
            logger.error("DaytonDelinquentTax: CSV has no header row")
            return []

        # ── Step 6: build RawSignal list ──────────────────────────────────────
        rows_seen = 0
        rows_skipped_no_parcel = 0
        rows_skipped_overflow = 0
        signals: list[RawSignal] = []

        for row in reader:
            rows_seen += 1

            # Skip rows with extra fields (unescaped commas misaligned all columns;
            # numeric fields after the bad column are corrupted and unusable).
            if row.get(None) is not None:
                rows_skipped_overflow += 1
                logger.debug(
                    "DaytonDelinquentTax: skipping overflow row %d (extra fields: %r)",
                    rows_seen, row.get(None),
                )
                continue

            parcel_raw = (row.get("PARCELID") or "").strip()
            if not parcel_raw:
                rows_skipped_no_parcel += 1
                continue

            # Owner: combine primary + secondary name lines
            owner1 = (row.get("OWNERNAME1") or "").strip()
            owner2 = (row.get("OWNERNAME2") or "").strip()
            owner = " ".join(p for p in [owner1, owner2] if p)

            # Property address (situs) — can be a township name for non-site parcels
            address = (row.get("PARCELLOCATION") or "").strip()

            # Mailing address (absentee-owner signal)
            paddr1 = (row.get("PADDR1") or "").strip()
            paddr2 = (row.get("PADDR2") or "").strip()
            paddr3 = (row.get("PADDR3") or "").strip()
            mailing = ", ".join(p for p in [paddr1, paddr2, paddr3] if p)

            # GATE KEYS — must match market_config distress_lead_rules EXACTLY.
            # delq_amount: bare numeric string (2dp, no leading zeros)
            delq_amount = _clean_amount(row.get("NETDELQ"))
            # cls: verbatim class code (R=residential, C=commercial, E=exempt, …)
            cls = (row.get("CLS") or "").strip()

            # Supplemental fields
            certdlqyr_raw = (row.get("CERTDLQYR") or "").strip()
            try:
                years_delinquent = (
                    current_year - int(certdlqyr_raw)
                    if certdlqyr_raw.isdigit()
                    else None
                )
            except (ValueError, TypeError):
                years_delinquent = None

            foreclosure = (row.get("FRCLSR") or "").strip()  # date string or ''
            appraised = _clean_amount(row.get("ASMTTOTAL"))
            district = (row.get("TXDST") or "").strip()
            luc = (row.get("LUC") or "").strip()
            nbhd = (row.get("NBHD") or "").strip()

            payload: dict[str, Any] = {
                # ── Gate keys (distress_lead_rules) — do NOT rename ──────────
                "delq_amount": delq_amount,
                "cls": cls,
                # ── Identification + skip-trace ──────────────────────────────
                "owner": owner,
                "address": address,
                "mailing": mailing,
                # ── Distress depth indicators ─────────────────────────────────
                "certdlqyr": certdlqyr_raw,
                "years_delinquent": years_delinquent,
                "foreclosure": foreclosure,    # date-of-foreclosure-filing if set
                # ── Valuation + classification ────────────────────────────────
                "appraised": appraised,
                "district": district,
                "luc": luc,
                "nbhd": nbhd,
            }

            signals.append(RawSignal(
                parcel_number=parcel_raw,   # raw PRINT_KEY; upsert_signals normalises
                signal_type="tax_delinquent",
                source=SIGNAL_SOURCE,
                observed_at=observed_at,
                confidence=1.0,
                payload=payload,
            ))

        logger.info(
            "DaytonDelinquentTax: %d signals from %d rows "
            "(skipped: %d overflow, %d no-parcel; tape=%s)",
            len(signals), rows_seen,
            rows_skipped_overflow, rows_skipped_no_parcel,
            observed_at.date() if observed_at else "unknown",
        )
        return signals


if __name__ == "__main__":
    import asyncio

    # Reference parcel from the field map (spaced PRINT_KEY → 'R72088080055')
    ANCHOR_PARCEL_RAW = "R72 08808 0055"
    ANCHOR_PARCEL_NORM = "R72088080055"

    async def _dry_run() -> None:
        scraper = DaytonDelinquentTaxScraper()
        print("DaytonDelinquentTaxScraper dry-run — downloading live tape...")
        signals = await scraper.fetch_signals()

        total = len(signals)
        print(f"\nTotal signals: {total}  (expect ~25,000+)")

        netdelq_pos = sum(
            1 for s in signals
            if s.payload.get("delq_amount") and float(s.payload["delq_amount"]) > 0
        )
        cls_r = sum(1 for s in signals if s.payload.get("cls") == "R")
        print(f"NETDELQ > 0: {netdelq_pos}  (expect ~20,115)")
        print(f"CLS = 'R':   {cls_r}  (expect ~21,883)")

        # Anchor parcel check — R72 08808 0055 should normalize to R72088080055.
        norm = normalize_parcel_number(ANCHOR_PARCEL_RAW)
        anchor_hits = [
            s for s in signals
            if normalize_parcel_number(s.parcel_number) == ANCHOR_PARCEL_NORM
        ]
        print(f"\nAnchor parcel {ANCHOR_PARCEL_RAW!r} → {norm!r}: ", end="")
        if anchor_hits:
            print("FOUND")
            print(f"  payload: {anchor_hits[0].payload}")
        else:
            print("NOT FOUND (may not be delinquent — check against tape directly)")

        # Payload key check
        expected_gate_keys = {"delq_amount", "cls"}
        expected_all_keys = {
            "delq_amount", "cls", "owner", "address", "mailing",
            "certdlqyr", "years_delinquent", "foreclosure",
            "appraised", "district", "luc", "nbhd",
        }
        if signals:
            present = set(signals[0].payload.keys())
            missing_gate = expected_gate_keys - present
            missing_all = expected_all_keys - present
            extra = present - expected_all_keys
            if missing_gate:
                print(f"\nWARNING: GATE KEYS MISSING from payload: {missing_gate}")
            else:
                print(f"\nGate keys OK (delq_amount + cls present): {sorted(expected_gate_keys)}")
            if missing_all:
                print(f"Missing supplemental keys: {missing_all}")
            if extra:
                print(f"Extra keys: {extra}")

        # Sample signals
        print("\nSample RawSignals (first 3 with cls='R' and delq_amount > 0):")
        shown = 0
        for sig in signals:
            if shown >= 3:
                break
            try:
                if sig.payload.get("cls") == "R" and float(sig.payload.get("delq_amount", "0")) > 0:
                    print(f"  parcel_number : {sig.parcel_number!r}  → norm: {normalize_parcel_number(sig.parcel_number)!r}")
                    print(f"  signal_type   : {sig.signal_type}")
                    print(f"  source        : {sig.source}")
                    print(f"  observed_at   : {sig.observed_at}")
                    print(f"  confidence    : {sig.confidence}")
                    print(f"  payload       : {sig.payload}")
                    print()
                    shown += 1
            except (ValueError, TypeError):
                pass

    asyncio.run(_dry_run())
