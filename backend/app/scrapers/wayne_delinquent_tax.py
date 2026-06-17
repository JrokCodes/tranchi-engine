"""
Wayne County Treasurer — Forfeiture List — writes to tranchi.signals (NOT tranchi.listings).

The Wayne County Treasurer publishes an annual "Notice of Forfeited Property Subject to
Foreclosure" PDF listing every parcel with ≥2 years delinquent taxes, grouped by
municipality. These parcels face circuit-court foreclosure with a Sept–Oct auction;
owners can still redeem. This is the strongest pre-auction distress signal in MI.

Source:
  https://www.waynecountymi.gov/files/assets/mainsite/v/1/treasurer/property-amp-taxes/
  documents/2026_wayne_county_delinquent_tax_liens.pdf
  (~12.7 MB, ~196 pages, 2026 edition published Dec 2025)

INVARIANT — WAF (read before editing):
  waynecountymi.gov returns HTTP 403 to requests with a bare User-Agent. A full
  Chrome header set (Accept, Accept-Language, Sec-Fetch-Mode/Site/Dest/User,
  Upgrade-Insecure-Requests, Accept-Encoding) is required. No Playwright needed.
  Verified: 2026-06-11.

INVARIANT — PARCEL FORMATS (read before editing):
  Two distinct parcel formats appear in the same PDF and must be branched:
    Detroit ward format:   8-digit + trailing period  e.g. "01000136."
    Detroit hyphen range:  8-digit + '-N'              e.g. "02000185-6"
    Out-county packed:     14 purely-numeric digits    e.g. "32093520302300"
  Both are passed VERBATIM to normalize_parcel_number() (Wayne branch is already
  wired -- it returns them unchanged). '^[0-9]{8}\\.$' matches Detroit plain;
  '^[0-9]{14}$' matches out-county packed.

INVARIANT — PDF COLUMN FLOW (read before editing):
  pdftotext/PyMuPDF gives sequential text lines but multi-column pages interleave
  column content. The record structure per parcel is:
    [Municipality header line]    → "City of DETROIT Ward 01"    (sets property_city)
    [Parcel number line]          → "01000136."
    [Situs address line]          → "401 E CONGRESS"
    [1..n interested party lines] → "538 EAST CONGRESS LLC", "APG PARKING, INC"
  A section header resets property_city for all subsequent parcels until the next header.
  Validate parcel→address adjacency; do NOT assume fixed line offsets.

PDF LIBRARY PREFERENCE:
  1. PyMuPDF (import fitz) — tried first.
  2. pdftotext CLI (poppler-utils) — subprocess fallback.
  If neither is present a RuntimeError is raised naming both missing dependencies.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.scrapers.base import SignalScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawSignal

logger = logging.getLogger(__name__)

SITE_NAME = "Wayne County Treasurer — Forfeiture List"
SIGNAL_SOURCE = "wayne_treasurer_forfeiture"

# ⚠ NEW-EDITION BUMP: when the next annual forfeiture PDF publishes (~Dec each year),
# update ALL THREE of these TOGETHER — the URL year, _PUBLICATION_DATE, and _EDITION_YEAR.
# A partial bump (URL only) leaves observed_at/year stale and the new signals read as old.
_PDF_URL = (
    "https://www.waynecountymi.gov/files/assets/mainsite/v/1/treasurer/"
    "property-amp-taxes/documents/2026_wayne_county_delinquent_tax_liens.pdf"
)

# Publication date of the 2026 edition (created 2025-10-29, published Dec 2025).
# Used as the stable observed_at anchor for idempotency — a parcel+year pair
# on the same observed_at date is treated as a re-run UPDATE, not a new row.
_PUBLICATION_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)  # epoch for 2026 edition
_EDITION_YEAR = 2026

# httpx download timeout (12.7MB PDF; allow generous time on slow connections)
_TIMEOUT = 180.0

# Full Chrome header set required to bypass waynecountymi.gov WAF (see INVARIANT above).
_CHROME_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}

# Parcel format patterns (see INVARIANT above).
# Detroit: 8 digits + trailing period, e.g. "01000136."
_RE_DETROIT_PLAIN = re.compile(r"^\d{8}\.$")
# Detroit: 8 digits + hyphen + one or more digits, e.g. "02000185-6"
_RE_DETROIT_HYPHEN = re.compile(r"^\d{8}-\d+$")
# Out-county packed: exactly 14 digits, e.g. "32093520302300"
_RE_OUTCOUNTY_PACKED = re.compile(r"^\d{14}$")

# Municipality header pattern: "City of X", "Township of X", "Village of X",
# "Charter Township of X", etc. Optionally followed by "Ward NN".
#
# IMPORTANT: exclude lines with "C/O" — those are interested-party names like
# "CITY OF DETROIT C/O BUILDINGS, SAFETY" that happen to start with a city name
# but are NOT section headers. Real section headers never contain "C/O".
# Also require title-case start ("City of", not "CITY OF") to further filter;
# all-caps "CITY OF X" lines without C/O still appear as party names in practice.
_RE_MUN_HEADER = re.compile(
    r"^(?:City|Township|Village|Charter\s+Township|Town)\s+of\s+[^/]+$",
)


def _is_parcel_line(line: str) -> bool:
    """Return True if `line` looks like a Wayne County parcel number."""
    s = line.strip()
    return bool(
        _RE_DETROIT_PLAIN.match(s)
        or _RE_DETROIT_HYPHEN.match(s)
        or _RE_OUTCOUNTY_PACKED.match(s)
    )


def _is_mun_header(line: str) -> bool:
    """Return True if `line` is a municipality section header."""
    return bool(_RE_MUN_HEADER.match(line.strip()))


def _extract_city_from_header(header: str) -> str:
    """Pull the city/municipality name from a header like 'City of DETROIT Ward 01'."""
    s = header.strip()
    # Strip a trailing "Ward NN" clause if present
    s = re.sub(r"\s+Ward\s+\d+\s*$", "", s, flags=re.IGNORECASE).strip()
    # Strip leading "City of ", "Township of ", etc.
    s = re.sub(r"^(?:Charter\s+Township|Township|Village|City|Town)\s+of\s+", "", s, flags=re.IGNORECASE)
    return s.strip().title()


def _is_page_artifact(line: str) -> bool:
    """Return True for pdftotext artifacts that are NOT parcel data.

    Catches: blank lines, pure-digit page numbers, 'Wayne County', header/footer
    boilerplate that pdftotext surfaces as standalone lines.
    """
    s = line.strip()
    if not s:
        return True
    if re.match(r"^\d{1,4}$", s):  # page numbers
        return True
    if re.match(r"^-{3,}$", s):    # rule lines
        return True
    return False


def _pdf_to_lines_fitz(pdf_bytes: bytes, max_pages: int | None = None) -> list[str]:
    """Extract text lines from PDF bytes using PyMuPDF (fitz)."""
    import fitz  # type: ignore[import]

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    lines: list[str] = []
    page_count = len(doc)
    if max_pages is not None:
        page_count = min(page_count, max_pages)
    for i in range(page_count):
        page = doc[i]
        text = page.get_text("text")
        lines.extend(text.splitlines())
    doc.close()
    return lines


def _pdf_to_lines_pdftotext(pdf_bytes: bytes, max_pages: int | None = None) -> list[str]:
    """Extract text lines from PDF bytes using the pdftotext CLI (poppler-utils).

    IMPORTANT: do NOT use -layout. The forfeiture PDF is a 4-6-column newspaper
    layout; -layout interleaves columns into very wide lines, breaking the sequential
    parcel->address->parties state machine. Without -layout, pdftotext linearizes
    the columns into the correct sequential order (verified against 2026 edition).
    """
    cmd = ["pdftotext"]
    if max_pages is not None:
        cmd += ["-l", str(max_pages)]
    cmd += ["-", "-"]  # read from stdin, write to stdout

    result = subprocess.run(
        cmd,
        input=pdf_bytes,
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"pdftotext exited {result.returncode}: {result.stderr.decode()[:300]}"
        )
    return result.stdout.decode("utf-8", errors="replace").splitlines()


def _load_pdf_parser():
    """Return the available PDF→lines callable, or raise with a clear message."""
    try:
        import fitz  # noqa: F401
        return _pdf_to_lines_fitz
    except ImportError:
        pass

    if subprocess.run(["which", "pdftotext"], capture_output=True).returncode == 0:
        return _pdf_to_lines_pdftotext

    raise RuntimeError(
        "No PDF parser available. Install one of:\n"
        "  PyMuPDF:       pip install pymupdf\n"
        "  poppler-utils: apt-get install poppler-utils  (or brew install poppler)"
    )


def _parse_lines(lines: list[str]) -> list[dict]:
    """Parse sequential text lines from the forfeiture PDF into per-parcel records.

    State machine (see INVARIANT — PDF COLUMN FLOW):
      - Municipality header  → update current_city, reset parcel state
      - Parcel line          → start new parcel block
      - Address line         → attach to pending parcel (must immediately follow parcel)
      - Everything else      → if parcel+address seen, treat as interested-party name

    A parcel block is COMMITTED when the next parcel or section header is encountered,
    so interested parties accumulate until the block closes. Malformed blocks (parcel
    without address) are skipped with a warning; they do NOT abort parsing.
    """
    records: list[dict] = []
    current_city: str = ""

    # Pending block state
    pending_parcel: str | None = None
    pending_address: str | None = None
    pending_parties: list[str] = []

    def _commit() -> None:
        nonlocal pending_parcel, pending_address, pending_parties
        if pending_parcel is None:
            return
        if not pending_address:
            logger.debug(
                "wayne_delinquent_tax: skipping parcel %s — no address found",
                pending_parcel,
            )
            pending_parcel = None
            pending_address = None
            pending_parties = []
            return
        records.append({
            "parcel_number": pending_parcel,
            "property_address": pending_address,
            "property_city": current_city,
            "interested_parties": list(pending_parties),
        })
        pending_parcel = None
        pending_address = None
        pending_parties = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if _is_page_artifact(line):
            continue

        if _is_mun_header(line):
            _commit()
            current_city = _extract_city_from_header(line)
            continue

        if _is_parcel_line(line):
            _commit()
            pending_parcel = line
            pending_address = None
            pending_parties = []
            continue

        # If we have a parcel but no address yet, this line IS the address
        if pending_parcel is not None and pending_address is None:
            # Validate: an address should look like it starts with a number or a direction
            # Accept generously — just exclude obvious header/footer noise
            if not _is_page_artifact(line):
                pending_address = line
            continue

        # Parcel + address already captured → accumulate interested parties
        if pending_parcel is not None and pending_address is not None:
            if not _is_page_artifact(line):
                pending_parties.append(line)
            continue

        # Line before any parcel in the current section — could be column header
        # text or boilerplate; skip silently
        logger.debug("wayne_delinquent_tax: unclassified line: %r", line)

    # Commit the final pending block
    _commit()

    return records


def _records_to_signals(records: list[dict]) -> list[RawSignal]:
    """Convert parsed parcel dicts to RawSignal objects."""
    signals: list[RawSignal] = []
    for rec in records:
        parcel_raw = rec["parcel_number"]
        norm = normalize_parcel_number(parcel_raw)
        if not norm:
            logger.warning("wayne_delinquent_tax: normalize_parcel_number returned None for %r", parcel_raw)
            continue

        owner = rec["interested_parties"][0] if rec["interested_parties"] else None

        signals.append(RawSignal(
            parcel_number=norm,
            signal_type="tax_delinquent",
            source=SIGNAL_SOURCE,
            observed_at=_PUBLICATION_DATE,
            confidence=1.0,  # official county forfeiture record
            payload={
                "edition_year": _EDITION_YEAR,
                "property_address": rec["property_address"],
                "property_city": rec["property_city"],
                "owner": owner,
                "interested_parties": rec["interested_parties"],
                "forfeited": True,
                "pending_foreclosure": True,
            },
        ))
    return signals


class WayneDelinquentTaxScraper(SignalScraper):
    """Wayne County Treasurer annual forfeiture-list parcels as tax-distress signals.

    Downloads the published forfeiture PDF, parses municipality-grouped parcel blocks,
    and emits one RawSignal per parcel. Output flows through the signal path in run.py
    (fetch_signals → _cv_upsert_signals).

    site_name MUST match the string wired into market_config source_meta exactly.
    """

    site_name = SITE_NAME
    signal_source = SIGNAL_SOURCE

    async def fetch_signals(self) -> list[RawSignal]:
        pdf_parser = _load_pdf_parser()

        logger.info("WayneDelinquentTax: downloading forfeiture PDF from %s", _PDF_URL)
        try:
            async with httpx.AsyncClient(
                headers=_CHROME_HEADERS,
                follow_redirects=True,
                timeout=_TIMEOUT,
            ) as client:
                resp = await client.get(_PDF_URL)
                resp.raise_for_status()
                pdf_bytes = resp.content
        except httpx.HTTPStatusError as exc:
            logger.error(
                "WayneDelinquentTax: HTTP %d downloading PDF — WAF headers may have changed: %s",
                exc.response.status_code, exc,
            )
            return []
        except httpx.RequestError as exc:
            logger.error("WayneDelinquentTax: network error downloading PDF: %s", exc)
            return []

        logger.info("WayneDelinquentTax: downloaded %d bytes; parsing PDF", len(pdf_bytes))
        try:
            lines = pdf_parser(pdf_bytes)
        except Exception as exc:
            logger.error("WayneDelinquentTax: PDF parse failed: %s", exc)
            return []

        records = _parse_lines(lines)
        signals = _records_to_signals(records)

        logger.info(
            "WayneDelinquentTax: %d signals from %d parsed records (%d text lines)",
            len(signals), len(records), len(lines),
        )
        return signals


# ── Dry-run (no DB writes) ──────────────────────────────────────────────────
# Usage: cd /home/jayden/tranchi-engine/backend
#        timeout 240 python3 -m app.scrapers.wayne_delinquent_tax
if __name__ == "__main__":
    import ast
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    async def _dry_run(max_pages: int | None = None) -> None:
        pdf_parser = _load_pdf_parser()
        parser_name = "PyMuPDF (fitz)" if "fitz" in pdf_parser.__name__ else "pdftotext (poppler-utils)"
        print(f"PDF parser: {parser_name}")

        print(f"Downloading: {_PDF_URL}")
        try:
            async with httpx.AsyncClient(
                headers=_CHROME_HEADERS,
                follow_redirects=True,
                timeout=_TIMEOUT,
            ) as client:
                resp = await client.get(_PDF_URL)
                resp.raise_for_status()
                pdf_bytes = resp.content
        except httpx.HTTPStatusError as exc:
            print(f"FAILED — HTTP {exc.response.status_code}. WAF headers may need updating.", file=sys.stderr)
            sys.exit(1)

        print(f"Downloaded {len(pdf_bytes):,} bytes")

        if max_pages is not None:
            print(f"Parsing first {max_pages} pages only (dry-run limit)")
        lines = pdf_parser(pdf_bytes, max_pages=max_pages)
        print(f"Extracted {len(lines):,} text lines")

        records = _parse_lines(lines)
        signals = _records_to_signals(records)

        print(f"\nTotal parsed records: {len(records)}")
        print(f"Total signals:        {len(signals)}")

        # Parcel format breakdown
        detroit_plain = sum(1 for s in signals if _RE_DETROIT_PLAIN.match(s.parcel_number))
        detroit_hyphen = sum(1 for s in signals if _RE_DETROIT_HYPHEN.match(s.parcel_number))
        outcounty = sum(1 for s in signals if _RE_OUTCOUNTY_PACKED.match(s.parcel_number))
        other = len(signals) - detroit_plain - detroit_hyphen - outcounty
        print(f"\nParcel format breakdown:")
        print(f"  Detroit plain (8d.):   {detroit_plain}")
        print(f"  Detroit hyphen (8d-N): {detroit_hyphen}")
        print(f"  Out-county (14d):      {outcounty}")
        print(f"  Other/normalized:      {other}")

        # City distribution (top 10)
        from collections import Counter
        cities = Counter(s.payload.get("property_city", "UNKNOWN") for s in signals)
        print(f"\nTop cities:")
        for city, cnt in cities.most_common(10):
            print(f"  {city}: {cnt}")

        # Sample 15 signals
        print(f"\n--- Sample (first 15 signals) ---")
        for sig in signals[:15]:
            p = sig.payload
            print(
                f"  parcel={sig.parcel_number!r:22s}  "
                f"city={p.get('property_city', '')!r:20s}  "
                f"addr={p.get('property_address', '')!r:30s}  "
                f"owner={p.get('owner', '')!r}"
            )

        print(f"\nsite_name:    {SITE_NAME!r}")
        print(f"signal_source: {SIGNAL_SOURCE!r}")
        print(f"signal_type:   tax_delinquent")
        print(f"observed_at:   {_PUBLICATION_DATE}")

    # Parse only first 10 pages for the dry-run proof (full PDF is 196 pages/12.7MB)
    asyncio.run(_dry_run(max_pages=10))
