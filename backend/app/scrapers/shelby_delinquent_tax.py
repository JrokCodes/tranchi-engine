"""
Shelby County (TN) Delinquent Realty Lawsuit List — writes to tranchi.signals.

The Trustee is required by TN law to file suit + publish the list of parcels with
delinquent real-estate taxes. That published list ("Exhibit A") is a free, structured
XLSX of every parcel in the current delinquent-tax lawsuit — a strong per-parcel
tax-distress SIGNAL (owner is in active tax-foreclosure litigation). Like the Cuyahoga
DelinquentTaxScraper this is a SIGNAL source, not a listing source: it tags parcels with
signal_type='tax_delinquent' so the read-time HOT engine stacks "Tax Distress" onto
foreclosure / tax-sale / land-bank listings (e.g. "in trustee-sale foreclosure AND in a
tax-delinquency lawsuit" → HOT). ~26K parcels — far too many (and too low-signal alone)
to mint as standalone listings.

Source (free, no auth):
  Landing: https://www.shelbycountytrustee.com/259/Delinquent-Realty-Lawsuit-List
  File:    .../DocumentCenter/View/<id>/ExhibitA   (XLSX)

INVARIANT (read before editing):
  - The DocumentCenter View id (currently 1504) is EDITION-SPECIFIC — it changes when
    the Trustee publishes a new lawsuit list. We DISCOVER the current link from the /259
    landing page each run and only fall back to the known id if discovery fails. Do NOT
    rely on the hardcoded id long-term.
  - observed_at = Jan 1 of the row's tax Year (idempotency). The signals natural key is
    (parcel_number, signal_type, source, observed_at::date), so a stable per-year
    observed_at means re-runs UPDATE in place (no 26K-row daily bloat); a new edition for
    a new tax year mints a fresh row. Multiple tax_delinquent signals still collapse to
    ONE "Tax Distress" dimension at read time, so no HOT inflation.
  - A parcel can appear on MULTIPLE rows (City of Memphis + Shelby County, multiple years).
    We AGGREGATE per parcel: sum TaxUnpaid, collect authorities/years, keep one signal.
  - parcel_number is passed RAW (spaced "088030  00095"); upsert_signals normalizes it via
    normalize_parcel_number (TN branch -> 14-char canonical) and stub-upserts tranchi.parcels
    to satisfy the signals FK. Most parcels resolve to the 351K ReGIS spine.

XLSX columns (verified live 2026-06-02): Name, ParcelID, Year, Taxing Autority [sic],
Property Location, Mailing Address, City/St/Zip, TaxUnpaid. ~26,386 data rows.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
import openpyxl
from bs4 import BeautifulSoup

from app.scrapers.base import SignalScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawSignal
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

SITE_NAME = "Shelby Delinquent Tax"
SIGNAL_SOURCE = "shelby_county_trustee"

_LANDING_URL = "https://www.shelbycountytrustee.com/259/Delinquent-Realty-Lawsuit-List"
_FALLBACK_FILE_URL = "https://www.shelbycountytrustee.com/DocumentCenter/View/1504/ExhibitA"
_BASE = "https://www.shelbycountytrustee.com"

_TIMEOUT = 60.0  # 1.8MB XLSX
_FALLBACK_YEAR = 2024


def _to_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _discover_file_url(html: str) -> str:
    """Find the current ExhibitA DocumentCenter link on the landing page."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "DocumentCenter/View/" in href and "exhibita" in href.lower():
            return href if href.startswith("http") else _BASE + href
    # any DocumentCenter link as a looser fallback
    for a in soup.find_all("a", href=True):
        if "DocumentCenter/View/" in a["href"]:
            href = a["href"]
            return href if href.startswith("http") else _BASE + href
    return _FALLBACK_FILE_URL


class ShelbyDelinquentTaxScraper(SignalScraper):
    """Shelby delinquent-tax-lawsuit parcels as tax-distress signals.

    Plain SignalScraper — output flows through the signal path in run.py
    (fetch_signals -> _cv_upsert_signals, which normalizes the parcel + stub-upserts
    tranchi.parcels for the signals FK).
    """

    site_name = SITE_NAME
    signal_source = SIGNAL_SOURCE  # run.py reads this for the active-count + dashboard

    async def fetch_signals(self) -> list[RawSignal]:
        headers = default_headers()
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            file_url = _FALLBACK_FILE_URL
            try:
                landing = await client.get(_LANDING_URL, timeout=_TIMEOUT)
                if landing.status_code == 200:
                    file_url = _discover_file_url(landing.text)
            except httpx.RequestError as exc:
                logger.warning("ShelbyDelinquentTax: landing fetch failed (%s) — using fallback url", exc)

            logger.info("ShelbyDelinquentTax: downloading lawsuit list %s", file_url)
            try:
                resp = await client.get(file_url, timeout=_TIMEOUT)
                resp.raise_for_status()
                content = resp.content
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                logger.error("ShelbyDelinquentTax: download failed: %s", exc)
                return []

        try:
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
        except Exception as exc:
            logger.error("ShelbyDelinquentTax: XLSX parse failed: %s", exc)
            return []

        # Aggregate per parcel: sum unpaid, collect authorities/years, keep owner/location.
        agg: dict[str, dict[str, Any]] = {}
        rows_seen = 0
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                continue  # header
            if not row or len(row) < 8:
                continue
            name, parcel_raw, year, authority, prop_loc, _mail, _csz, unpaid = row[:8]
            if not parcel_raw or not str(parcel_raw).strip():
                continue
            rows_seen += 1
            norm = normalize_parcel_number(str(parcel_raw).strip())
            if not norm:
                continue
            try:
                yr = int(year) if year else _FALLBACK_YEAR
            except (TypeError, ValueError):
                yr = _FALLBACK_YEAR
            rec = agg.setdefault(norm, {
                "parcel_raw": str(parcel_raw).strip(),
                "owner": (str(name).strip() if name else None),
                "property_location": (str(prop_loc).strip() if prop_loc else None),
                "authorities": set(),
                "years": set(),
                "tax_unpaid": 0.0,
            })
            if authority:
                rec["authorities"].add(str(authority).strip())
            rec["years"].add(yr)
            amt = _to_float(unpaid)
            if amt:
                rec["tax_unpaid"] += amt

        signals: list[RawSignal] = []
        for norm, rec in agg.items():
            max_year = max(rec["years"]) if rec["years"] else _FALLBACK_YEAR
            observed_at = datetime(max_year, 1, 1, tzinfo=timezone.utc)
            signals.append(RawSignal(
                parcel_number=rec["parcel_raw"],   # raw; upsert_signals normalizes
                signal_type="tax_delinquent",
                source=SIGNAL_SOURCE,
                observed_at=observed_at,
                confidence=1.0,                    # official county lawsuit record
                payload={
                    "tax_unpaid": round(rec["tax_unpaid"], 2),
                    "years": sorted(rec["years"]),
                    "taxing_authorities": sorted(rec["authorities"]),
                    "owner": rec["owner"],
                    "property_location": rec["property_location"],
                    "in_lawsuit": True,
                },
            ))

        logger.info(
            "ShelbyDelinquentTax: %d signals from %d rows (%d distinct parcels)",
            len(signals), rows_seen, len(agg),
        )
        return signals
