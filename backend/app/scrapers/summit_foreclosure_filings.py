"""
Summit County (OH / Akron) Foreclosure FILINGS — writes to tranchi.signals.

Source: Akron Legal News /notices/foreclosures (same site as summit_legalnews.py,
different page). Single inline page (~40 KB), no detail-page hop needed — all
fields are present in the inline block via semantic span/id attributes. Each
<div class="format-notice"> is one LEGAL NOTICE.

This is the EARLIEST stage of the Summit distress pipeline — a complaint filed
in Common Pleas (months before any sheriff sale is scheduled). It lands in
tranchi.signals so the signal-stack join can flag a parcel as "has an active
foreclosure complaint" when its distress_lead_types row is enabled. It does NOT
mint a tranchi.listings row.

INVARIANTS (read before editing):
  1. PARCEL VALIDITY: only real-property parcels have a 7-digit Summit number.
     MHLOT (manufactured/mobile-home) parcels and non-real-property entries
     appear with parcel numbers that do NOT normalize to exactly 7 digits after
     normalize_parcel_number(). Skip any row whose normalized parcel does not
     match ^[0-9]{7}$ — these are not real property and should never land in signals.

  2. CASE NUMBER SPACE-STRIPPING: ALN formats case numbers with spaces
     ("CV2026 03 0869"). Strip ALL spaces before storing so the value joins the
     RealAuction / sheriff-sale case number format ("CV2026030869"). Never
     compare the raw ALN text to any other source without stripping first.

  3. STABLE observed_at FROM FILING DATE: observed_at is derived from the
     notice_date1 span (the filing date "February 27, 2026"), NOT from the
     page-scrape timestamp. This makes observed_at stable across re-runs — a
     re-scrape for the same filing UPDATEs in place rather than inserting a
     duplicate row. The signals natural key is
     (parcel_number, signal_type, source, observed_at::date).

  4. PRE-DISTRESS SIGNAL: this is a FILING-stage source, surfaced as a lead
     only when its distress_lead_types row is enabled. Do NOT treat it as a
     sheriff-sale listing; it belongs in tranchi.signals, not tranchi.listings.

HTML structure (verified live 2026-06-13):
  - Posting date header: <h4 class="indent">Foreclosures From June 12, 2026</h4>
  - Each notice:         <div class="format-notice"> ... </div>
  - Case number:         <span class="notice_case_number">CV2026 03 0869</span>
  - Plaintiff name:      <span class="notice_name1">Kristen M. Scalise</span>
                         (plaintiff type derived from surrounding text in the <p>)
  - Filing date:         <span id="notice_date1">February 27, 2026</span>
  - Parcel number:       <span class="notice_parcel_number">51-03109</span>
  - Address:             <span class="notice_address">1743 Ronald Rd.</span>
  - City:                <span class="notice_city">Akron</span>
  - Zip:                 <span class="notice_zip">44312</span>
  - Legal description:   <span class="notice_lot_number">AIRPORT GDNS LOT 4...</span>

Anchor (verified live 2026-06-13):
  CV2026 03 0869 → parcel 51-03109 → normalize → '5103109'
  owner HAWKINS LUTHER M AND HAZEL I, 1743 Ronald Rd, Akron OH 44312
  plaintiff "Kristen M. Scalise" + "Summit County Fiscal Officer" → plaintiff_type='tax'
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup, Tag

from app.scrapers.base import SignalScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawSignal
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

SITE_NAME = "Summit Foreclosure Filings (ALN)"
SIGNAL_SOURCE = "summit_aln_foreclosures"

_BASE_URL = "https://www.akronlegalnews.com"
_FORECLOSURES_URL = f"{_BASE_URL}/notices/foreclosures"

_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0

# Posting date in header: "Foreclosures From June 12, 2026"
_POSTING_DATE_RE = re.compile(
    r"[Ff]oreclosure[s]?\s+[Ff]rom\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})"
)

# Plaintiff type: 'tax' if the paragraph containing the plaintiff name also
# contains these phrases (ALN always spells it out after the plaintiff name span).
_TAX_PLAINTIFF_PHRASES = ("fiscal officer", "treasurer")


# ─────────────────────────────────────────────────────────────────────────────
# Parse helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_case(raw: str | None) -> str | None:
    """Strip all whitespace from a spaced ALN case number.

    'CV2026 03 0869' -> 'CV2026030869'
    Returns None for blank/None input.
    """
    if not raw:
        return None
    return re.sub(r"\s+", "", raw.strip()) or None


def _parse_longdate(raw: str | None) -> datetime | None:
    """'February 27, 2026' or 'February 27 2026' -> UTC midnight datetime.

    Returns None on blank/None/unparseable input — never raises.
    """
    if not raw:
        return None
    raw = raw.strip().rstrip(".")
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _text(tag: Tag | None) -> str | None:
    """Return stripped text from a BS4 tag, or None if tag is None."""
    if tag is None:
        return None
    t = tag.get_text(strip=True)
    return t or None


def _classify_plaintiff(plaintiff_para_text: str | None) -> str:
    """Return 'tax' if paragraph text contains Fiscal Officer/Treasurer, else 'mortgage'."""
    if not plaintiff_para_text:
        return "mortgage"
    lower = plaintiff_para_text.lower()
    for phrase in _TAX_PLAINTIFF_PHRASES:
        if phrase in lower:
            return "tax"
    return "mortgage"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helper
# ─────────────────────────────────────────────────────────────────────────────

async def _get_html(client: httpx.AsyncClient, url: str, label: str) -> str | None:
    """GET url with exponential-backoff retry. Returns HTML text or None on failure."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error(
                    "SummitForeclosureFilings: GET %s (%s) failed after %d attempts: %s",
                    url, label, attempt, exc,
                )
                return None
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Page parsing
# ─────────────────────────────────────────────────────────────────────────────

def _parse_foreclosures_page(html: str) -> list[dict]:
    """Parse the ALN /notices/foreclosures page.

    Returns a list of raw notice dicts, one per <div class="format-notice"> block.
    Uses semantic span id/class attributes rather than regex on raw text.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Posting date from <h4 class="indent">Foreclosures From June 12, 2026</h4>
    posting_date: str | None = None
    h4 = soup.find("h4", class_="indent")
    if h4:
        h4_text = h4.get_text(strip=True)
        pd_m = _POSTING_DATE_RE.search(h4_text)
        if pd_m:
            posting_date = pd_m.group(1).strip()

    notices: list[dict] = []
    for div in soup.find_all("div", class_="format-notice"):
        notice = _parse_notice_div(div, posting_date)
        if notice:
            notices.append(notice)

    return notices


def _parse_notice_div(div: Tag, posting_date: str | None) -> dict | None:
    """Parse one <div class="format-notice"> block into a raw notice dict.

    Returns None if the block has no case number (skips malformed/empty blocks).
    """
    # Case number — may appear more than once; take first
    case_tag = div.find("span", class_="notice_case_number")
    case_raw = _text(case_tag)
    if not case_raw:
        return None
    case_number = _normalize_case(case_raw)

    # Filing date — <span id="notice_date1"> (unique per notice)
    date_tag = div.find(id="notice_date1")
    filing_dt = _parse_longdate(_text(date_tag))

    # Plaintiff name — <span class="notice_name1">
    plaintiff_name_tag = div.find("span", class_="notice_name1")
    plaintiff_name = _text(plaintiff_name_tag)

    # Plaintiff type — determined from the full paragraph text that contains
    # notice_name1. ALN always appends ", Summit County Fiscal Officer" after the
    # name span in the same <p> when it's a tax-lien foreclosure.
    plaintiff_type = "mortgage"
    if plaintiff_name_tag:
        parent_p = plaintiff_name_tag.find_parent("p")
        if parent_p:
            plaintiff_type = _classify_plaintiff(parent_p.get_text())
        else:
            # Fallback: search the whole div text
            plaintiff_type = _classify_plaintiff(div.get_text())

    # Build plaintiff string: name only (the class captures the name; the
    # surrounding text in the <p> contains role/address that we don't need to store)
    plaintiff = plaintiff_name

    # Parcel numbers — class="notice_parcel_number" may appear twice (once inline
    # in the body paragraph, once in the "Permanent Parcel Number:" line).
    # Some mortgage-foreclosure blocks embed routing numbers in the span text
    # ("6815043, Routing No.07-00267-02-023.000; 6814331, ..."). We extract
    # clean 2-7 digit numeric tokens (with optional single dash) and deduplicate.
    parcel_tags = div.find_all("span", class_="notice_parcel_number")
    parcel_raws_seen: set[str] = set()
    parcel_raws: list[str] = []
    for pt in parcel_tags:
        raw = _text(pt)
        if not raw:
            continue
        # Try treating the whole span text as a single parcel (clean 2-7 digit form
        # with optional dash, e.g. '51-03109'). If it contains letters or is long,
        # fall back to extracting numeric-dash tokens.
        clean_candidates: list[str]
        if re.match(r"^[\d\-]+$", raw) and len(re.sub(r"\D", "", raw)) <= 7:
            # Simple clean parcel (normal case: '51-03109' or '5103109')
            clean_candidates = [raw]
        else:
            # Compound span — extract Summit display form (NN-NNNNN) or bare 7-digit
            # tokens only. Routing-number fragments like '07-00267' share the same
            # form; they will be gated out later IF the filing date is present.
            # NN-NNNNN: exactly 2 digits, dash, exactly 5 digits (Summit display form)
            # NNNNNNN:  exactly 7 consecutive digits (Summit compact form)
            clean_candidates = re.findall(r"\b\d{2}-\d{5}\b|\b\d{7}\b", raw)
        for cand in clean_candidates:
            if cand and cand not in parcel_raws_seen:
                parcel_raws_seen.add(cand)
                parcel_raws.append(cand)

    # Address components — each has its own span class
    property_address = _text(div.find("span", class_="notice_address"))
    property_city = _text(div.find("span", class_="notice_city"))
    # notice_state is present but we don't need to store it (always OH)
    property_zip = _text(div.find("span", class_="notice_zip"))

    # Legal description — <span class="notice_lot_number">
    legal_description = _text(div.find("span", class_="notice_lot_number"))

    return {
        "case_raw": case_raw,
        "case_number": case_number,
        "filing_dt": filing_dt,
        "plaintiff": plaintiff,
        "plaintiff_type": plaintiff_type,
        "parcel_raws": parcel_raws,
        "property_address": property_address,
        "property_city": property_city,
        "property_zip": property_zip,
        "legal_description": legal_description,
        "posting_date": posting_date,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SignalScraper
# ─────────────────────────────────────────────────────────────────────────────

class SummitForeclosureFilingsScraper(SignalScraper):
    """Summit County foreclosure complaints (ALN /notices/foreclosures) -> tranchi.signals.

    Pre-distress pipeline stage — complaint filed in Common Pleas, months before
    any sheriff sale. No DB pool needed; signals flow through the signal path in
    run.py (fetch_signals -> upsert_signals, which normalizes the parcel and
    stub-upserts tranchi.parcels for the FK).

    Constructor:
        No required arguments.
    """

    site_name = SITE_NAME
    signal_source = SIGNAL_SOURCE  # run.py reads this for the active-count + dashboard

    async def fetch_signals(self) -> list[RawSignal]:
        headers = default_headers()
        headers["Referer"] = _BASE_URL + "/"

        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            logger.info("SummitForeclosureFilings: fetching %s", _FORECLOSURES_URL)
            html = await _get_html(client, _FORECLOSURES_URL, "foreclosures")

        if not html:
            logger.error("SummitForeclosureFilings: failed to fetch page — returning empty")
            return []

        raw_notices = _parse_foreclosures_page(html)
        logger.info("SummitForeclosureFilings: parsed %d raw notice blocks", len(raw_notices))

        signals: list[RawSignal] = []
        skipped_no_parcel = 0
        skipped_non_7digit = 0
        skipped_no_date = 0

        for notice in raw_notices:
            parcel_raws: list[str] = notice.get("parcel_raws") or []

            if not parcel_raws:
                skipped_no_parcel += 1
                logger.debug(
                    "SummitForeclosureFilings: skipping case %s — no parcel found",
                    notice.get("case_number"),
                )
                continue

            filing_dt: datetime | None = notice.get("filing_dt")
            if filing_dt is None:
                skipped_no_date += 1
                logger.debug(
                    "SummitForeclosureFilings: skipping case %s — no filing date parsed",
                    notice.get("case_number"),
                )
                continue

            for parcel_raw in parcel_raws:
                norm = normalize_parcel_number(parcel_raw)

                # INVARIANT: only emit signals for real property (7-digit Summit parcels).
                # MHLOT and non-real-property parcel numbers do not normalize to 7 digits.
                if not norm or not re.match(r"^\d{7}$", norm):
                    skipped_non_7digit += 1
                    logger.debug(
                        "SummitForeclosureFilings: skipping parcel %r (case %s) — "
                        "normalized to %r, not a 7-digit Summit real-property parcel",
                        parcel_raw, notice.get("case_number"), norm,
                    )
                    continue

                payload = {
                    "case_number": notice.get("case_number"),
                    "filing_date": filing_dt.date().isoformat() if filing_dt else None,
                    "plaintiff": notice.get("plaintiff"),
                    "plaintiff_type": notice.get("plaintiff_type"),
                    "property_address": notice.get("property_address"),
                    "property_city": notice.get("property_city"),
                    "property_zip": notice.get("property_zip"),
                    "legal_description": notice.get("legal_description"),
                    "posting_date": notice.get("posting_date"),
                }

                signals.append(RawSignal(
                    parcel_number=norm,       # normalized 7-digit; upsert_signals FK-stubs parcels
                    signal_type="foreclosure_filing",
                    source=SIGNAL_SOURCE,
                    observed_at=filing_dt,    # stable from filing date — UPDATE-in-place on re-run
                    confidence=1.0,
                    payload=payload,
                ))

        logger.info(
            "SummitForeclosureFilings: %d signals from %d notice blocks "
            "(skipped: %d no-parcel, %d non-7-digit, %d no-date)",
            len(signals), len(raw_notices),
            skipped_no_parcel, skipped_non_7digit, skipped_no_date,
        )
        return signals


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging as _logging
    import re as _re

    _logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Anchor: CV2026 03 0869 -> parcel 51-03109 -> norm '5103109' -> 1743 Ronald Rd
    ANCHOR_CASE_NORM = "CV2026030869"
    ANCHOR_PARCEL_RAW = "51-03109"
    ANCHOR_PARCEL_NORM = "5103109"
    ANCHOR_ADDRESS = "1743 Ronald Rd"
    ANCHOR_PLAINTIFF_TYPE = "tax"

    async def _dry_run() -> None:
        print(f"\n=== SummitForeclosureFilings Dry Run — {SITE_NAME} ===\n")

        scraper = SummitForeclosureFilingsScraper()
        signals = await scraper.fetch_signals()

        print(f"Total signals emitted : {len(signals)}")
        print()

        # ── Anchor verification ───────────────────────────────────────────────
        print("--- Anchor case verification ---")
        anchor_hits = [s for s in signals if s.payload.get("case_number") == ANCHOR_CASE_NORM]
        if anchor_hits:
            sig = anchor_hits[0]
            print(f"  case_number     : {sig.payload['case_number']!r}  (expected {ANCHOR_CASE_NORM!r})")
            print(f"  parcel_number   : {sig.parcel_number!r}  (expected {ANCHOR_PARCEL_NORM!r})")
            addr = sig.payload.get("property_address") or ""
            print(f"  property_address: {addr!r}  (expect contains {ANCHOR_ADDRESS!r})")
            print(f"  plaintiff_type  : {sig.payload['plaintiff_type']!r}  (expected {ANCHOR_PLAINTIFF_TYPE!r})")
            parcel_ok = sig.parcel_number == ANCHOR_PARCEL_NORM
            addr_ok = ANCHOR_ADDRESS.lower().rstrip(".") in addr.lower()
            ptype_ok = sig.payload["plaintiff_type"] == ANCHOR_PLAINTIFF_TYPE
            print(f"  parcel PASS     : {parcel_ok}")
            print(f"  address PASS    : {addr_ok}")
            print(f"  ptype PASS      : {ptype_ok}")
            if parcel_ok and addr_ok and ptype_ok:
                print("  ANCHOR: ALL CHECKS PASSED")
            else:
                print("  ANCHOR: ONE OR MORE CHECKS FAILED — inspect output above")
        else:
            print(f"  WARNING: anchor case {ANCHOR_CASE_NORM!r} not found in signals.")
            print("  The notice may have aged off the page — verify live.")
            # Direct normalize check even if anchor is gone
            from app.scrapers.db import normalize_parcel_number as _npn
            norm = _npn(ANCHOR_PARCEL_RAW)
            print(f"  Direct normalize_parcel_number({ANCHOR_PARCEL_RAW!r}) = {norm!r}  (expected {ANCHOR_PARCEL_NORM!r})")
            print(f"  Normalize check PASS: {norm == ANCHOR_PARCEL_NORM}")
        print()

        # ── 7-digit gate spot-check ───────────────────────────────────────────
        print("--- 7-digit parcel gate check ---")
        all_7digit = all(_re.match(r"^\d{7}$", s.parcel_number) for s in signals)
        print(f"  All emitted parcel_numbers are 7-digit: {all_7digit}")
        if not all_7digit:
            bad = [s.parcel_number for s in signals if not _re.match(r"^\d{7}$", s.parcel_number)]
            print(f"  BAD parcels (should not exist): {bad}")
        print()

        # ── Payload key check ─────────────────────────────────────────────────
        expected_keys = {
            "case_number", "filing_date", "plaintiff", "plaintiff_type",
            "property_address", "property_city", "property_zip",
            "legal_description", "posting_date",
        }
        print("--- Payload key check ---")
        if signals:
            present = set(signals[0].payload.keys())
            missing = expected_keys - present
            extra = present - expected_keys
            if missing:
                print(f"  MISSING keys: {missing}")
            if extra:
                print(f"  EXTRA keys  : {extra}")
            if not missing:
                print(f"  Payload keys OK: {sorted(present)}")
        print()

        # ── plaintiff_type distribution ───────────────────────────────────────
        print("--- Plaintiff type distribution ---")
        from collections import Counter
        dist = Counter(s.payload.get("plaintiff_type") for s in signals)
        for ptype, count in dist.items():
            print(f"  {ptype}: {count}")
        print()

        # ── Sample RawSignals (first 3) ───────────────────────────────────────
        print("--- Sample RawSignals (first 3) ---")
        for sig in signals[:3]:
            print(f"  parcel_number : {sig.parcel_number!r}")
            print(f"  signal_type   : {sig.signal_type!r}")
            print(f"  source        : {sig.source!r}")
            print(f"  observed_at   : {sig.observed_at}")
            print(f"  confidence    : {sig.confidence}")
            print(f"  payload       : {sig.payload}")
            print()

    asyncio.run(_dry_run())
