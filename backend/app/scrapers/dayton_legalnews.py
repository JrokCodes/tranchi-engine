"""
Dayton / Montgomery County OH — Daily Court Reporter (DCR) scraper.

Site: https://www.dailycourt.com  (BLOX/TownNews TNCMS 1.94.0)
API:  /search/?f=json&t=article&c[]=legal_notices/<section>&l=20&s=start_time&sd=desc
      + &o=<offset> for paging. Response: {total, next, prev, rows:[...]}.
      content[] is the full notice body INLINE — one request = complete notice.

Two TNCMS sections, two scraper classes, one file:
  DaytonPublicAuctionsScraper   — public_auctions  → tranchi.listings
                                   signal_type: mortgage_foreclosure |
                                                tax_delinquent_foreclosure
                                   site_name: "Montgomery Daily Court Reporter"
  DaytonForeclosureFilingsScraper — foreclosures  → tranchi.listings
                                   signal_type: foreclosure_filing (lis-pendens;
                                                no sale data)
                                   site_name: "Montgomery Foreclosure Filings (DCR)"

Both classes must be registered separately in run.py _SCRAPERS by the engine owner.
"Montgomery Foreclosure Filings (DCR)" also needs a "cursor" entry added to the
dayton staleness_policies in market_config.py (currently missing — will default to
FULL_RESCAN and wrongly retire the back-catalog on every run).

INVARIANT — Logan County filter (MANDATORY — must never be removed):
  The DCR feed is shared between Montgomery AND Logan counties. Logan County rows
  MUST NEVER enter tranchi.listings for the dayton market. The filter is applied
  per-row before returning from fetch_and_parse():
    KEEP if: at least one normalized parcel starts with a letter (R72, K46, G27, J44…).
    KEEP if: no parcel found AND body contains the word "MONTGOMERY".
    DROP otherwise (purely numeric parcel = Logan; no parcel + no county indicator).
  Montgomery PRINT_KEY = [LETTER] + 11-or-12 digits (e.g. 'R72 11703 0016').
  Logan County parcels are purely numeric (e.g. 43-031-08-05-010-000) and NEVER
  start with a letter after normalize_parcel_number() collapses delimiters.
  TRAP (2010-era vintage notices): some old filings have swapped fields — "Property
  Address:" contains the parcel number and "Parcel Number:" contains the street
  address (e.g. "1716 Salem Avenue, Dayton..."). The parcel extractor validates
  that each split part contains ONLY letters, digits, and hyphens (no spaces or
  word characters) so that street-name fragments like "Dayton" are rejected before
  they can masquerade as letter-prefix Montgomery parcels.
  _is_montgomery() also requires at least one digit after the letter prefix — pure
  text strings like "D" or "DAYTON" are not valid parcel identifiers.

INVARIANT — signal_type discriminator (per-row, NOT per-section):
  Both mortgage and tax foreclosures use Ohio civil "CV" case prefix — the prefix
  alone does NOT discriminate. Determine per notice from plaintiff + body text:
    tax_delinquent_foreclosure → plaintiff matches /(\\w+ )?COUNTY TREASURER/i, OR
      body contains "delinquent tax" / "in rem" / "r.c. 5721" /
      title contains "tax foreclosure".
    mortgage_foreclosure → all other plaintiffs (bank, servicer, lender).
  Applies to public_auctions only; foreclosures section always → foreclosure_filing.
  NEVER hardcode signal_type as a section-level constant.

Staleness (market_config.py → staleness_policies):
  "Montgomery Daily Court Reporter"      → cursor  (wired)
  "Montgomery Foreclosure Filings (DCR)" → cursor  (wired)

Volume recon (2026-06-26): public_auctions ~938, foreclosures ~2,106.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.scrapers._time import today_et
from app.scrapers.base import ListingScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawListing
from app.scrapers.user_agents import random_ua

logger = logging.getLogger(__name__)

# Wave-0 canonical site names — staleness.py + _SOURCE_META key on these verbatim.
# Scrapers MUST store these exact strings as source_site in every RawListing.
SITE_NAME_AUCTIONS = "Montgomery Daily Court Reporter"
SITE_NAME_FILINGS  = "Montgomery Foreclosure Filings (DCR)"

_BASE_URL        = "https://www.dailycourt.com/search/"
_SEC_AUCTIONS    = "public_auctions"
_SEC_FORECLOSURES = "foreclosures"
_PAGE_SIZE       = 20
# Safety ceiling: foreclosures ~2106/20 = 106 pages; auctions ~938/20 = 47 pages.
_MAX_PAGES       = 200
_TIMEOUT         = 30.0
_RETRY_ATTEMPTS  = 3
_RETRY_BASE_DELAY = 2.0
_INTER_PAGE_DELAY = 0.5


# ── Signal-type discriminator patterns ───────────────────────────────────────
# INVARIANT: never use case prefix to discriminate — see module docstring.

_TAX_PLAINTIFF_RE = re.compile(r'(?:\w+\s+)?COUNTY\s+TREASURER', re.IGNORECASE)
_TAX_BODY_RE = re.compile(
    r'\b(?:delinquent\s+tax|in\s+rem|R\.C\.\s*5721(?:\.18)?)\b',
    re.IGNORECASE,
)
_TAX_TITLE_RE = re.compile(r'\btax\s+foreclosure\b', re.IGNORECASE)


# ── Content-parse patterns ────────────────────────────────────────────────────

# Case number: YYYY CV NNNNN (Ohio Common Pleas CV format)
_CASE_RE = re.compile(r'\b(\d{4}\s*CV\s*\d{4,6})\b', re.IGNORECASE)

# Parcel label + trailing parcel string(s). Stop at common next-label words.
# NOTE: deliberate stop at "with" (comma+space+word) excluded; validation in
# _extract_parcels rejects street-name fragments captured past the real parcel.
_PARCEL_LABEL_RE = re.compile(
    r'Parcel\s+(?:Number|No\.?|#)?\s*:?\s*'
    r'([A-Za-z0-9][A-Za-z0-9\s\-,]+?)(?=\n|\r|$|Plaintiff|Defendant|Property\s|Case\s|Sale\s|Deposit)',
    re.IGNORECASE,
)
# Valid parcel candidate: only letters, digits, spaces, hyphens — no words like "Avenue"
# after trimming. Used by _extract_parcels to reject street-name fragments.
_PARCEL_VALID_RE = re.compile(r'^[A-Za-z0-9][\dA-Za-z\-]*$')

# Full address: "NNN STREET, CITY, [MONTGOMERY[,]] OH[IO][,] NNNNN"
_ADDR_FULL_RE = re.compile(
    r'(\d+\s+[^,\n\r]{2,80}),\s*'           # street (number + name)
    r'([A-Za-z][A-Za-z\s\.\'\-]{1,40}),\s*' # city
    r'(?:MONTGOMERY(?:\s+COUNTY)?,\s*)?'     # optional county segment
    r'OH(?:IO)?\s*,?\s*'                     # state
    r'(\d{5})',                              # zip
    re.IGNORECASE,
)

# Fallback address — "known as / located at / property: NNNN STREET, CITY"
_ADDR_KNOWN_RE = re.compile(
    r'(?:known\s+as|located\s+at|property\s*:?\s*)\s*'
    r'(\d+\s+[^,\n\r]{2,80}),\s*'
    r'([A-Za-z][A-Za-z\s\.\'\-]{1,40})',
    re.IGNORECASE,
)

# Plaintiff / Defendant — two formats:
#   1. DCR auctions inline: "Case# YYYY CV NNNNN. [PLAINTIFF] vs [DEFENDANT], et al."
#   2. DCR/foreclosures labeled: "Plaintiff:" or "NAME, PLAINTIFF" on its own line
_VS_LINE_RE = re.compile(
    r'Case#?\s+\d{4}\s*CV\s*\d{4,6}[.,]\s*([^.]+?)\s+vs\.?\s+([^.]+?)(?:,\s*et\s+al\.?)?\s*\.',
    re.IGNORECASE,
)
_PLAINTIFF_LABEL_RE = re.compile(
    r'(?:^|\n)\s*(.+?),\s*PLAINTIFF\s*$', re.IGNORECASE | re.MULTILINE,
)
_PLAINTIFF_FIELD_RE = re.compile(
    r'Plaintiff\s*:?\s*\n?\s*([^\n\r]+)', re.IGNORECASE,
)
_DEFENDANT_FIELD_RE = re.compile(
    r'Defendant\s*:?\s*\n?\s*([^\n\r]+)', re.IGNORECASE,
)

# Sale date formats.  Priority: "Sale Date:" label → "opening on [DATE]" (Auction.com)
# → "Provisional Sale date:" → "sold at auction on [DATE]"
_SALE_DATE_LABEL_RE = re.compile(
    r'Sale\s+Date\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})',
    re.IGNORECASE,
)
# "opening on July 21, 2026" — Auction.com opening bid date (primary sale date for DCR)
_SALE_DATE_OPENING_RE = re.compile(
    r'opening\s+on\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})',
    re.IGNORECASE,
)
# "Provisional Sale date: August 4, 2026" — fallback date if buyer defaults
_SALE_DATE_PROV_RE = re.compile(
    r'(?:Provisional\s+)?Sale\s+[Dd]ate\s*:?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})',
    re.IGNORECASE,
)
_SALE_DATE_ALT_RE = re.compile(
    r'(?:to\s+be\s+)?(?:sold|offered)\s+(?:at\s+)?(?:public\s+)?auction'
    r'(?:\s+on)?\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})',
    re.IGNORECASE,
)
# "Bidding Open Date: 7/15/2026" — Beth Rose Auction (bethroseauction.com) format.
# Distinct auctioneer from Auction.com; the bidding-open date is the sale date (the
# auction window opens then). ~60/76 of the otherwise-undated public_auctions notices
# use this format (verified live 2026-06-27). Without it the sale_date is NULL, which
# both hides the date from users AND defeats the sale_date-passed expiry guard.
_SALE_DATE_BIDOPEN_RE = re.compile(
    r'Bidding\s+Open\s+Date\s*:?\s*(\d{1,2}/\d{1,2}/\d{4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})',
    re.IGNORECASE,
)

# Sale time
_SALE_TIME_RE = re.compile(
    r'Sale\s+Time\s*:?\s*(\d{1,2}:\d{2}\s*(?:AM|PM|a\.m\.|p\.m\.)?)',
    re.IGNORECASE,
)
_SALE_TIME_AT_RE = re.compile(
    r'at\s+(\d{1,2}:\d{2}\s*(?:AM|PM|a\.m\.|p\.m\.))',
    re.IGNORECASE,
)

# Deposit — "deposit required is $5,000" or "Deposit Required: $5,000"
_DEPOSIT_RE = re.compile(
    r'[Dd]eposit\s*(?:[Rr]equired|[Aa]mount)?\s*(?:is\s+)?:?\s*\$?\s*([\d,]+(?:\.\d{2})?)',
    re.IGNORECASE,
)

# Sale location. Montgomery foreclosure auctions run on two platforms: Auction.com and
# Beth Rose (bethroseauction.com). Capture the full host — a bare 'auction.com' pattern
# wrongly truncates 'bethroseauction.com' to 'auction.com'.
_SALE_LOC_RE = re.compile(r'((?:www\.)?[A-Za-z]*[Aa]uction\.com)', re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# Parse helpers
# ─────────────────────────────────────────────────────────────────────────────

def _content_to_text(content: Any) -> str:
    """Convert DCR content field to plain text.

    Handles: list of HTML strings, list of dicts ({value/html key}),
    single HTML string, or empty/None. Returns '' on failure — never raises.
    """
    if not content:
        return ""
    if isinstance(content, str):
        items = [content]
    elif isinstance(content, list):
        items = content
    else:
        return ""

    parts: list[str] = []
    for item in items:
        if isinstance(item, str):
            raw = item
        elif isinstance(item, dict):
            raw = item.get("value") or item.get("html") or item.get("text") or ""
        else:
            continue
        if not raw:
            continue
        if "<" in raw:
            try:
                soup = BeautifulSoup(raw, "html.parser")
                parts.append(soup.get_text(" ", strip=True))
            except Exception:
                parts.append(raw)
        else:
            parts.append(raw.strip())
    return "\n".join(p for p in parts if p)


def _parse_date_flexible(raw: str | None) -> date | None:
    """Parse date strings in multiple OH notice formats. Never raises."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in (
        "%m/%d/%Y", "%m/%d/%y",
        "%B %d, %Y", "%B %d %Y",
        "%b %d, %Y", "%b %d %Y",
    ):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_money(raw: str | None) -> float | None:
    """'$5,000' / '5000.00' → float; None/'' → None. Never raises."""
    if not raw:
        return None
    cleaned = re.sub(r"[^0-9.]", "", str(raw))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_case_number(title: str, body: str) -> str | None:
    """Extract and normalize case number from title or body.

    Normalizes internal whitespace: '2025 CV 04878' → '2025 CV 04878'.
    """
    for text in (title, body):
        m = _CASE_RE.search(text)
        if m:
            return re.sub(r'\s+', ' ', m.group(1).strip()).upper()
    return None


def _extract_parcels(text: str) -> list[str]:
    """Extract and normalize all parcel numbers from notice text.

    Finds 'Parcel Number:' label, splits on comma, normalizes each via the
    global normalize_parcel_number(). Returns [] if no parcel label found.
    Uses the GLOBAL normalizer — no local normalizer, per engine contract.

    Validation: each split part must be all-alphanumeric-and-hyphen BEFORE
    normalization (no spaces inside). This rejects street-address fragments
    like "1716 Salem Avenue" or city names like "Dayton" that appear in
    vintage 2010-era notices where the Parcel Number and Property Address
    fields were swapped. See module INVARIANT comment for full context.
    """
    m = _PARCEL_LABEL_RE.search(text)
    if not m:
        return []
    raw_segment = m.group(1)
    parts = [p.strip() for p in raw_segment.split(",")]
    normalized: list[str] = []
    for part in parts:
        if not part:
            continue
        # Reject anything with interior spaces — parcel strings are alphanumeric+hyphen.
        # This catches "1716 Salem Avenue" while passing "R72 11703 0016" only after
        # spaces are a deliberate delimiter (the normalizer collapses them).
        # Key: actual parcel candidates have ONLY alphanumeric + hyphen chars (no words).
        if not _PARCEL_VALID_RE.match(part.replace(" ", "")):
            # After stripping spaces, must be all-alphanumeric+hyphen
            continue
        # Extra guard: must contain at least one digit (rejects pure-letter city names)
        if not any(c.isdigit() for c in part):
            continue
        norm = normalize_parcel_number(part)
        if norm and norm not in normalized:
            normalized.append(norm)
    return normalized


def _is_montgomery(parcels: list[str], body: str) -> bool:
    """Return True if the notice belongs to Montgomery County.

    INVARIANT (Logan filter): Montgomery PRINT_KEY always starts with a letter
    AND has digits (R72..., K46..., G27..., J44...). Logan County parcels are
    purely numeric after normalize_parcel_number(). Pure text strings like "D"
    or "DAYTON" that might slip through from badly-parsed vintage notices are
    rejected by the digit requirement.
    If no parcels are found, fall back to body text 'MONTGOMERY' check.
    """
    if parcels:
        return any(
            p and p[0].isalpha() and any(c.isdigit() for c in p)
            for p in parcels
        )
    # No parcel found — check body for county indicator
    return bool(re.search(r'\bMONTGOMERY\b', body, re.IGNORECASE))


def _get_signal_type(title: str, body: str, plaintiff: str | None) -> str:
    """Return signal_type for a public_auctions notice.

    INVARIANT: discriminate by PLAINTIFF + body, NOT by case prefix ('CV').
    Both tax and mortgage foreclosures use the same CV prefix in Ohio.
    """
    p = plaintiff or ""
    if _TAX_PLAINTIFF_RE.search(p):
        return "tax_delinquent_foreclosure"
    if _TAX_BODY_RE.search(body):
        return "tax_delinquent_foreclosure"
    if _TAX_TITLE_RE.search(title):
        return "tax_delinquent_foreclosure"
    return "mortgage_foreclosure"


def _extract_address(text: str) -> tuple[str | None, str | None, str | None]:
    """Extract (street, city, zip) from notice text.

    Tries the full 'street, city, OH, zip' pattern, then a 'known as / located at'
    fallback. Returns (None, None, None) if nothing parseable.
    """
    m = _ADDR_FULL_RE.search(text)
    if m:
        street = m.group(1).strip()
        city   = m.group(2).strip().rstrip(",").strip()
        zip_   = m.group(3).strip()
        return street, city, zip_
    m2 = _ADDR_KNOWN_RE.search(text)
    if m2:
        street = m2.group(1).strip()
        city   = m2.group(2).strip().rstrip(",").strip()
        return street, city, None
    return None, None, None


def _extract_plaintiff(text: str) -> str | None:
    """Extract plaintiff from DCR notice.

    Priority:
      1. Inline "Case# YYYY CV NNNNN. [PLAINTIFF] vs [DEFENDANT]" (public_auctions)
      2. "NAME, PLAINTIFF" on its own line (some foreclosure formats)
      3. "Plaintiff:" labeled field
    """
    m = _VS_LINE_RE.search(text)
    if m:
        return m.group(1).strip() or None
    m2 = _PLAINTIFF_LABEL_RE.search(text)
    if m2:
        return m2.group(1).strip() or None
    m3 = _PLAINTIFF_FIELD_RE.search(text)
    return m3.group(1).strip() or None if m3 else None


def _extract_defendant(text: str) -> str | None:
    """Extract defendant from DCR notice.

    Priority:
      1. Inline "Case# YYYY CV NNNNN. [PLAINTIFF] vs [DEFENDANT], et al." (public_auctions)
      2. "Defendant:" labeled field
    """
    m = _VS_LINE_RE.search(text)
    if m:
        return m.group(2).strip() or None
    m2 = _DEFENDANT_FIELD_RE.search(text)
    return m2.group(1).strip() or None if m2 else None


def _extract_sale_date(text: str) -> date | None:
    """Extract sale/auction date from DCR notice text.

    Priority: "Sale Date:" label → "opening on [DATE]" (Auction.com primary date)
    → "Bidding Open Date:" (Beth Rose / bethroseauction.com) → "Provisional Sale date:"
    → "sold at auction on [DATE]". Auction.com and Beth Rose are the two Montgomery
    auction platforms; each labels its bidding-open (sale) date differently.
    """
    m = _SALE_DATE_LABEL_RE.search(text)
    if m:
        return _parse_date_flexible(m.group(1))
    m2 = _SALE_DATE_OPENING_RE.search(text)
    if m2:
        return _parse_date_flexible(m2.group(1))
    mbo = _SALE_DATE_BIDOPEN_RE.search(text)
    if mbo:
        return _parse_date_flexible(mbo.group(1))
    m3 = _SALE_DATE_PROV_RE.search(text)
    if m3:
        return _parse_date_flexible(m3.group(1))
    m4 = _SALE_DATE_ALT_RE.search(text)
    if m4:
        return _parse_date_flexible(m4.group(1))
    return None


def _extract_sale_time(text: str) -> str | None:
    m = _SALE_TIME_RE.search(text)
    if m:
        return m.group(1).strip() or None
    m2 = _SALE_TIME_AT_RE.search(text)
    if m2:
        return m2.group(1).strip() or None
    return None


def _extract_sale_location(text: str) -> str | None:
    m = _SALE_LOC_RE.search(text)
    return m.group(1).strip() if m else None


def _extract_deposit(text: str) -> float | None:
    m = _DEPOSIT_RE.search(text)
    return _parse_money(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────────────────────────
# HTTP fetch helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_headers() -> dict[str, str]:
    return {
        "User-Agent": random_ua(),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.dailycourt.com/",
    }


async def _get_json(
    client: httpx.AsyncClient,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """GET the DCR JSON search API with exponential-backoff retry."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(_BASE_URL, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error(
                    "DCR: GET %s failed after %d attempts: %s",
                    params, attempt, exc,
                )
                return None
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return None


async def _fetch_section(
    client: httpx.AsyncClient,
    section: str,
) -> list[dict[str, Any]]:
    """Paginate one DCR TNCMS section and return all raw article rows.

    Uses the 'next' cursor from each response to advance. Stops when 'next'
    is null/False, when no rows are returned, or when _MAX_PAGES is reached.
    """
    params_base: dict[str, Any] = {
        "f": "json",
        "t": "article",
        "c[]": f"legal_notices/{section}",
        "l": _PAGE_SIZE,
        "s": "start_time",
        "sd": "desc",
    }

    all_rows: list[dict[str, Any]] = []
    offset    = 0
    total_seen = 0
    page      = 0

    while page < _MAX_PAGES:
        params = dict(params_base)
        if offset > 0:
            params["o"] = offset

        data = await _get_json(client, params)
        if not data:
            logger.warning("DCR %s: fetch failed at offset %d — stopping", section, offset)
            break

        total     = int(data.get("total") or 0)
        rows: list[dict[str, Any]] = data.get("rows") or []
        if not rows:
            break

        all_rows.extend(rows)
        total_seen += len(rows)
        page += 1

        # Advance cursor.  TNCMS 'next' is the integer offset for the next page,
        # or null when we've hit the end.  Also handle boolean-false from some
        # TNCMS versions.
        next_val = data.get("next")
        if next_val is None or next_val is False or next_val == "":
            break  # end of feed
        if isinstance(next_val, bool):
            # True shouldn't appear; treat as advance by page size
            offset += _PAGE_SIZE
        elif isinstance(next_val, int):
            offset = next_val
        elif isinstance(next_val, str):
            m = re.search(r'[?&]o=(\d+)', next_val)
            if m:
                offset = int(m.group(1))
            else:
                break  # unrecognised string — stop
        else:
            offset += _PAGE_SIZE

        if total and total_seen >= total:
            break

        await asyncio.sleep(_INTER_PAGE_DELAY)

    if page >= _MAX_PAGES:
        logger.warning(
            "DCR %s: hit _MAX_PAGES ceiling (%d) — feed may be undercounted",
            section, _MAX_PAGES,
        )

    logger.info("DCR %s: fetched %d rows in %d pages", section, len(all_rows), page)
    return all_rows


# ─────────────────────────────────────────────────────────────────────────────
# Row → RawListing converters
# ─────────────────────────────────────────────────────────────────────────────

def _row_to_auction_listing(row: dict[str, Any], today: date) -> RawListing | None:
    """Convert one public_auctions row to a RawListing, or None if filtered.

    Filters applied (in order):
      1. Logan / non-Montgomery filter  (INVARIANT — mandatory)
      2. sale_date < today              (only upcoming auctions)
      3. No address AND no parcel       (not enough to anchor a listing)
    """
    title   = (row.get("title") or "").strip()
    content = row.get("content") or []
    body    = _content_to_text(content)
    full    = f"{title}\n{body}" if body else title

    if not full.strip():
        return None

    # Parse parcel(s) first — used by Montgomery filter
    parcels = _extract_parcels(full)

    # ── INVARIANT: Logan filter ──────────────────────────────────────────────
    if not _is_montgomery(parcels, full):
        logger.debug("DCR auctions: DROP non-Montgomery row title=%r", title[:80])
        return None

    # Parse remaining fields
    plaintiff   = _extract_plaintiff(full)
    defendant   = _extract_defendant(full)
    signal_type = _get_signal_type(title, full, plaintiff)
    street, city, zip_ = _extract_address(full)
    sale_date   = _extract_sale_date(full)
    sale_time   = _extract_sale_time(full)
    sale_loc    = _extract_sale_location(full)
    deposit     = _extract_deposit(full)
    case_number = _extract_case_number(title, full)

    # Filter: past auctions (skip rows with no sale_date so we don't lose valid leads)
    if sale_date is not None and sale_date < today:
        return None

    # Address gate: require either a parsed street OR a parcel anchor.
    # Select primary parcel as the FIRST Montgomery (letter-prefix) parcel so that
    # a cross-county notice ordered [logan_numeric, montgomery_alpha] doesn't assign
    # the Logan numeric as the spine-join key (which would silently fail all joins).
    primary_parcel = (
        next((p for p in parcels if p and p[0].isalpha()), parcels[0])
        if parcels else None
    )
    if not street:
        if not primary_parcel:
            logger.debug(
                "DCR auctions: DROP no address + no parcel (case=%r)", case_number
            )
            return None
        # Parcel-anchored placeholder so prefilter passes; enrichment updates address
        street = f"Parcel {primary_parcel}"

    return RawListing(
        source_site=SITE_NAME_AUCTIONS,
        source_listing_id=primary_parcel,
        case_number=case_number,
        signal_type=signal_type,
        property_address=street,
        property_city=city,
        property_county="Montgomery",
        property_state="OH",
        property_zip=zip_,
        sale_date=sale_date,
        sale_time=sale_time,
        sale_location=sale_loc,
        deposit_usd=deposit,
        opening_bid_usd=None,       # RealForeclose enriches by case# (JOIN KEY)
        appraised_value_usd=None,   # RealForeclose enriches by case# (JOIN KEY)
        trustee_name=defendant,
        status="active",
        auction_status="scheduled",
    )


def _row_to_filing_listing(row: dict[str, Any]) -> RawListing | None:
    """Convert one foreclosures row to a foreclosure_filing RawListing.

    No sale_date (lis-pendens stage; auction not yet scheduled).
    Montgomery filter enforced identically to the auctions section.
    """
    title   = (row.get("title") or "").strip()
    content = row.get("content") or []
    body    = _content_to_text(content)
    full    = f"{title}\n{body}" if body else title

    if not full.strip():
        return None

    parcels = _extract_parcels(full)

    # ── INVARIANT: Logan filter ──────────────────────────────────────────────
    if not _is_montgomery(parcels, full):
        logger.debug("DCR filings: DROP non-Montgomery row title=%r", title[:80])
        return None

    street, city, zip_ = _extract_address(full)
    case_number = _extract_case_number(title, full)
    defendant   = _extract_defendant(full)
    # Select primary parcel as the FIRST Montgomery (letter-prefix) parcel — same
    # cross-county ordering guard as _row_to_auction_listing (see comment there).
    primary_parcel = (
        next((p for p in parcels if p and p[0].isalpha()), parcels[0])
        if parcels else None
    )

    if not street:
        if not primary_parcel:
            logger.debug(
                "DCR filings: DROP no address + no parcel (case=%r)", case_number
            )
            return None
        street = f"Parcel {primary_parcel}"

    return RawListing(
        source_site=SITE_NAME_FILINGS,
        source_listing_id=primary_parcel,
        case_number=case_number,
        signal_type="foreclosure_filing",
        property_address=street,
        property_city=city,
        property_county="Montgomery",
        property_state="OH",
        property_zip=zip_,
        sale_date=None,         # lis-pendens filing — auction not yet scheduled
        sale_time=None,
        sale_location=None,
        deposit_usd=None,
        opening_bid_usd=None,
        appraised_value_usd=None,
        trustee_name=defendant,
        status="active",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scraper classes
# ─────────────────────────────────────────────────────────────────────────────

class DaytonPublicAuctionsScraper(ListingScraper):
    """Montgomery County upcoming foreclosure auctions from Daily Court Reporter.

    Fetches the 'public_auctions' TNCMS section and emits RawListing rows with
    signal_type determined per-row from plaintiff/body (NEVER hardcoded):
      mortgage_foreclosure        — bank/servicer/lender plaintiff
      tax_delinquent_foreclosure  — COUNTY TREASURER plaintiff or tax-indicator body

    Staleness: CURSOR (forward-only auction feed + sale_date expiry guard).
    Registration key for run.py _SCRAPERS: 'dayton_legalnews_auctions'  (or as
    the engine owner registers it — must match the site_name for staleness lookup).
    """

    site_name = SITE_NAME_AUCTIONS

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    async def fetch_and_parse(self) -> list[RawListing]:
        today = today_et()
        headers = _build_headers()

        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            rows = await _fetch_section(client, _SEC_AUCTIONS)

        listings: list[RawListing] = []
        for row in rows:
            rl = _row_to_auction_listing(row, today)
            if rl is not None:
                listings.append(rl)

        tax_count  = sum(1 for l in listings if l.signal_type == "tax_delinquent_foreclosure")
        mort_count = sum(1 for l in listings if l.signal_type == "mortgage_foreclosure")
        logger.info(
            "DCR public_auctions: %d raw → %d listings "
            "(mortgage=%d, tax=%d)",
            len(rows), len(listings), mort_count, tax_count,
        )
        return listings


class DaytonForeclosureFilingsScraper(ListingScraper):
    """Montgomery County lis-pendens / foreclosure complaints from Daily Court Reporter.

    Fetches the 'foreclosures' TNCMS section and emits RawListing rows with
    signal_type='foreclosure_filing'. No sale_date (complaint stage; auction
    not yet scheduled). Earliest distress stage in the Montgomery pipeline.

    Staleness: CURSOR (forward-only court-filing feed; no sale_date to expire).
    REQUIRED: add to market_config.py dayton staleness_policies:
      "Montgomery Foreclosure Filings (DCR)": "cursor"
    Registration key for run.py _SCRAPERS: 'dayton_legalnews_filings'
    """

    site_name = SITE_NAME_FILINGS

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    async def fetch_and_parse(self) -> list[RawListing]:
        headers = _build_headers()

        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            rows = await _fetch_section(client, _SEC_FORECLOSURES)

        listings: list[RawListing] = []
        for row in rows:
            rl = _row_to_filing_listing(row)
            if rl is not None:
                listings.append(rl)

        logger.info(
            "DCR foreclosures: %d raw → %d foreclosure_filing listings",
            len(rows), len(listings),
        )
        return listings


# ─────────────────────────────────────────────────────────────────────────────
# Standalone probe / unit-test (no DB)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    import json
    import logging as _logging
    import sys

    _logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Fixture dir for saving raw API samples
    _FIXTURE_DIR = (
        "/tmp/claude-1000/-home-jayden-Intelleq-Library-Of-Babel"
        "/f7083ec4-0be4-4f7e-9e76-8adbbab5f07e/scratchpad"
    )

    async def _probe() -> None:  # noqa: C901
        print(f"\n{'='*60}")
        print("DCR Dayton scraper — live probe + unit tests")
        print(f"{'='*60}\n")

        headers = _build_headers()
        passes: list[str] = []
        failures: list[str] = []

        def _check(name: str, cond: bool, note: str = "") -> None:
            if cond:
                passes.append(name)
                print(f"  PASS  {name}" + (f" — {note}" if note else ""))
            else:
                failures.append(name)
                print(f"  FAIL  {name}" + (f" — {note}" if note else ""))

        # ── Phase 1: probe API structure (3 rows per section) ─────────────────
        print("── Phase 1: API structure probe ──────────────────────────────\n")

        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            pa_data = await _get_json(client, {
                "f": "json", "t": "article",
                "c[]": f"legal_notices/{_SEC_AUCTIONS}",
                "l": 3, "s": "start_time", "sd": "desc",
            })
            fc_data = await _get_json(client, {
                "f": "json", "t": "article",
                "c[]": f"legal_notices/{_SEC_FORECLOSURES}",
                "l": 3, "s": "start_time", "sd": "desc",
            })

        for label, data, fname in (
            ("public_auctions",   pa_data, "dcr_auctions_p1.json"),
            ("foreclosures",      fc_data, "dcr_foreclosures_p1.json"),
        ):
            if not data:
                print(f"  ERROR: could not fetch {label}")
                continue

            total    = data.get("total")
            next_val = data.get("next")
            rows     = data.get("rows") or []
            print(f"{label}:")
            print(f"  total={total}, next={next_val!r}, rows_in_page={len(rows)}")

            if rows:
                r0    = rows[0]
                keys  = list(r0.keys())
                ctent = r0.get("content") or []
                print(f"  row[0] keys:   {keys}")
                print(f"  title:         {r0.get('title')!r}")
                print(f"  starttime:     {r0.get('starttime')!r}")
                print(f"  content type:  {type(ctent).__name__}, len={len(ctent)}")
                if ctent:
                    c0 = ctent[0]
                    if isinstance(c0, str):
                        print(f"  content[0][:120]: {c0[:120]!r}")
                    elif isinstance(c0, dict):
                        print(f"  content[0] keys: {list(c0.keys())}")

                body = _content_to_text(ctent)
                print(f"\n  body[:400]:\n{body[:400]}")

                parcels = _extract_parcels(f"{r0.get('title','')}\n{body}")
                print(f"\n  Parsed fields:")
                print(f"    case_number: {_extract_case_number(r0.get('title',''), body)!r}")
                print(f"    parcels:     {parcels!r}")
                print(f"    plaintiff:   {_extract_plaintiff(body)!r}")
                print(f"    defendant:   {_extract_defendant(body)!r}")
                addr, city, zip_ = _extract_address(body)
                print(f"    address:     {addr!r}")
                print(f"    city:        {city!r}")
                print(f"    zip:         {zip_!r}")
                print(f"    is_mont:     {_is_montgomery(parcels, body)}")
                if label == "public_auctions":
                    print(f"    sale_date:   {_extract_sale_date(body)!r}")
                    print(f"    sale_time:   {_extract_sale_time(body)!r}")
                    print(f"    sale_loc:    {_extract_sale_location(body)!r}")
                    print(f"    deposit:     {_extract_deposit(body)!r}")

            # Save fixture
            try:
                fpath = f"{_FIXTURE_DIR}/{fname}"
                with open(fpath, "w") as f:
                    json.dump(data, f, indent=2)
                print(f"\n  Fixture saved → {fpath}")
            except Exception as e:
                print(f"\n  Fixture save failed: {e}")
            print()

        # ── Phase 2: Unit tests on live-parsed fixtures ────────────────────────
        print("\n── Phase 2: Unit tests on parsed data ────────────────────────\n")

        # Test: normalize_parcel_number (Montgomery branch)
        test_parcels = [
            ("R72 11703 0016",  "R72117030016"),   # spaced — same parcel, spaced form
            ("R72-16003-0038",  "R72160030038"),   # hyphenated — different parcel
            ("R72119130016",    "R72119130016"),   # no-delimiter — different parcel
            ("43-031-08-05-010-000", None),        # Logan: purely numeric after strip
        ]
        print("  normalize_parcel_number (Montgomery branch):")
        for raw, expected in test_parcels:
            norm = normalize_parcel_number(raw)
            if expected is None:
                # Logan: should NOT start with a letter
                ok = (norm is None) or (norm and not norm[0].isalpha())
                _check(
                    f"normalize({raw!r})",
                    ok,
                    f"got {norm!r} — expect non-letter-prefix (Logan)"
                )
            else:
                _check(
                    f"normalize({raw!r})",
                    norm == expected,
                    f"got {norm!r}, expected {expected!r}"
                )
        print()

        # Test: Logan filter
        print("  Logan filter:")
        # A purely numeric parcel (Logan style) should be dropped
        logan_parcel = normalize_parcel_number("43-031-08-05-010-000")
        logan_parcels = [logan_parcel] if logan_parcel else []
        _check(
            "Logan parcels dropped by _is_montgomery",
            not _is_montgomery(logan_parcels, "LOGAN COUNTY, OHIO"),
            f"logan normalized={logan_parcel!r}"
        )
        # Montgomery parcel (letter prefix) should be kept
        mont_parcel = normalize_parcel_number("R72 11703 0016")
        mont_parcels = [mont_parcel] if mont_parcel else []
        _check(
            "Montgomery parcels pass _is_montgomery",
            _is_montgomery(mont_parcels, ""),
            f"montgomery normalized={mont_parcel!r}"
        )
        # No parcel, but body has MONTGOMERY → keep
        _check(
            "No-parcel row with MONTGOMERY body → kept",
            _is_montgomery([], "MONTGOMERY COUNTY, OHIO 45405"),
        )
        # No parcel, no MONTGOMERY → drop
        _check(
            "No-parcel row with no MONTGOMERY body → dropped",
            not _is_montgomery([], "LOGAN COUNTY, OHIO 43050"),
        )
        print()

        # Test: signal_type discriminator
        print("  signal_type discriminator:")
        _check(
            "COUNTY TREASURER plaintiff → tax_delinquent_foreclosure",
            _get_signal_type("", "", "MONTGOMERY COUNTY TREASURER") == "tax_delinquent_foreclosure",
        )
        _check(
            "RHONDA STAFFORD, LOGAN COUNTY TREASURER → tax_delinquent_foreclosure",
            _get_signal_type("", "", "RHONDA STAFFORD, LOGAN COUNTY TREASURER") == "tax_delinquent_foreclosure",
        )
        _check(
            "bank plaintiff → mortgage_foreclosure",
            _get_signal_type("", "", "ROCKET MORTGAGE LLC") == "mortgage_foreclosure",
        )
        _check(
            "body has 'in rem' → tax_delinquent_foreclosure",
            _get_signal_type("", "This action is brought in rem pursuant to R.C. 5721.18", None)
            == "tax_delinquent_foreclosure",
        )
        _check(
            "title 'Tax Foreclosure' → tax_delinquent_foreclosure",
            _get_signal_type("Tax Foreclosure Notice 2026 CV 00123", "", None) == "tax_delinquent_foreclosure",
        )
        _check(
            "body has 'R.C. 5721.18' → tax_delinquent_foreclosure",
            _get_signal_type("", "Pursuant to R.C. 5721.18, the property is", None)
            == "tax_delinquent_foreclosure",
        )
        _check(
            "body has 'delinquent tax liens' → tax_delinquent_foreclosure",
            _get_signal_type("", "to collect delinquent tax liens on the property", None)
            == "tax_delinquent_foreclosure",
        )
        print()

        # Test: multi-parcel split
        print("  Multi-parcel split:")
        synthetic_body = (
            "Plaintiff: HUNTINGTON NATIONAL BANK\n"
            "Defendant: JOHN DOE\n"
            "Parcel Number: R72 11703 0016, K46 22801 0004\n"
            "2064 RUSTIC ROAD, DAYTON, MONTGOMERY, OH, 45405"
        )
        multi_parcels = _extract_parcels(synthetic_body)
        _check(
            "multi-parcel notice splits into 2 parcels",
            len(multi_parcels) == 2,
            f"got {multi_parcels!r}"
        )
        _check(
            "first parcel normalized correctly",
            len(multi_parcels) > 0 and multi_parcels[0] == "R72117030016",
            f"first={multi_parcels[0] if multi_parcels else 'MISSING'!r}"
        )
        _check(
            "second parcel normalized correctly",
            len(multi_parcels) > 1 and multi_parcels[1] == "K46228010004",
            f"second={multi_parcels[1] if len(multi_parcels) > 1 else 'MISSING'!r}"
        )
        _check(
            "both multi-parcel parcels are letter-prefix (Montgomery)",
            _is_montgomery(multi_parcels, ""),
        )
        print()

        # Test: cross-county ordering — Logan parcel first, Montgomery parcel second
        print("  Cross-county [logan, montgomery] ordering:")
        logan_norm  = normalize_parcel_number("43-031-08-05-010-000")   # purely numeric
        mont_norm   = normalize_parcel_number("K46 22801 0004")         # letter-prefix
        cross_parcels = [p for p in [logan_norm, mont_norm] if p]
        # _is_montgomery must still pass (has at least one letter-prefix parcel)
        _check(
            "cross-county [logan, montgomery] passes _is_montgomery",
            _is_montgomery(cross_parcels, ""),
            f"parcels={cross_parcels!r}",
        )
        # primary_parcel must be the Montgomery one, not the Logan one
        primary = (
            next((p for p in cross_parcels if p and p[0].isalpha()), cross_parcels[0])
            if cross_parcels else None
        )
        _check(
            "cross-county primary_parcel is the Montgomery (letter-prefix) parcel",
            primary is not None and primary[0].isalpha(),
            f"primary={primary!r} (expected letter-prefix, not {cross_parcels[0]!r})",
        )
        _check(
            "cross-county primary_parcel is not the Logan numeric",
            primary != cross_parcels[0] if cross_parcels and not cross_parcels[0][0].isalpha() else True,
            f"primary={primary!r}, first_raw={cross_parcels[0]!r}",
        )
        print()

        # ── Phase 3: full fetch + row counts ──────────────────────────────────
        print("\n── Phase 3: full fetch (live row counts) ─────────────────────\n")

        scraper_a = DaytonPublicAuctionsScraper(dry_run=True)
        listings_a = await scraper_a.fetch_and_parse()
        tax_count  = sum(1 for l in listings_a if l.signal_type == "tax_delinquent_foreclosure")
        mort_count = sum(1 for l in listings_a if l.signal_type == "mortgage_foreclosure")
        with_parcel = sum(1 for l in listings_a if l.source_listing_id)
        with_date   = sum(1 for l in listings_a if l.sale_date)
        with_deposit = sum(1 for l in listings_a if l.deposit_usd)
        with_loc    = sum(1 for l in listings_a if l.sale_location)

        print(f"public_auctions → {len(listings_a)} listings (recon: ~938 raw, expect < raw after date filter)")
        print(f"  mortgage_foreclosure:       {mort_count}")
        print(f"  tax_delinquent_foreclosure: {tax_count}")
        print(f"  with source_listing_id:     {with_parcel}")
        print(f"  with sale_date:             {with_date}")
        print(f"  with deposit_usd:           {with_deposit}")
        print(f"  with sale_location:         {with_loc}")

        if listings_a:
            s = listings_a[0]
            print(f"\n  Sample listing:")
            print(f"    source_site:       {s.source_site!r}")
            print(f"    signal_type:       {s.signal_type!r}")
            print(f"    case_number:       {s.case_number!r}")
            print(f"    source_listing_id: {s.source_listing_id!r}")
            print(f"    property_address:  {s.property_address!r}")
            print(f"    property_city:     {s.property_city!r}")
            print(f"    property_zip:      {s.property_zip!r}")
            print(f"    sale_date:         {s.sale_date!r}")
            print(f"    sale_time:         {s.sale_time!r}")
            print(f"    sale_location:     {s.sale_location!r}")
            print(f"    deposit_usd:       {s.deposit_usd!r}")
            print(f"    trustee_name:      {s.trustee_name!r}")

        # Spot-check for Logan leakage in auctions
        logan_leak_a = [
            l for l in listings_a
            if l.source_listing_id and not l.source_listing_id[0].isalpha()
        ]
        _check(
            "No Logan rows in public_auctions output",
            len(logan_leak_a) == 0,
            f"{len(logan_leak_a)} purely-numeric parcel rows leaked"
        )

        # site_name consistency
        wrong_site_a = [l for l in listings_a if l.source_site != SITE_NAME_AUCTIONS]
        _check(
            "All auctions listings have correct source_site",
            len(wrong_site_a) == 0,
            f"{len(wrong_site_a)} wrong site rows"
        )
        print()

        scraper_f = DaytonForeclosureFilingsScraper(dry_run=True)
        listings_f = await scraper_f.fetch_and_parse()
        with_parcel_f = sum(1 for l in listings_f if l.source_listing_id)

        print(f"foreclosures → {len(listings_f)} listings (recon: ~2106 raw; Logan-filtered)")
        print(f"  with source_listing_id: {with_parcel_f}")
        if listings_f:
            sf = listings_f[0]
            print(f"\n  Sample filing:")
            print(f"    source_site:       {sf.source_site!r}")
            print(f"    signal_type:       {sf.signal_type!r}")
            print(f"    case_number:       {sf.case_number!r}")
            print(f"    source_listing_id: {sf.source_listing_id!r}")
            print(f"    property_address:  {sf.property_address!r}")
            print(f"    sale_date:         {sf.sale_date!r}  (expect None)")

        # Spot-check for Logan leakage in filings
        logan_leak_f = [
            l for l in listings_f
            if l.source_listing_id and not l.source_listing_id[0].isalpha()
        ]
        _check(
            "No Logan rows in foreclosure_filing output",
            len(logan_leak_f) == 0,
            f"{len(logan_leak_f)} purely-numeric parcel rows leaked"
        )

        wrong_sig_f = [l for l in listings_f if l.signal_type != "foreclosure_filing"]
        _check(
            "All filing listings have signal_type='foreclosure_filing'",
            len(wrong_sig_f) == 0,
            f"{len(wrong_sig_f)} wrong signal_type rows"
        )

        no_sale_date_f = all(l.sale_date is None for l in listings_f)
        _check(
            "All filing listings have sale_date=None",
            no_sale_date_f,
        )

        wrong_site_f = [l for l in listings_f if l.source_site != SITE_NAME_FILINGS]
        _check(
            "All filing listings have correct source_site",
            len(wrong_site_f) == 0,
        )

        # ── Summary ────────────────────────────────────────────────────────────
        print(f"\n{'='*60}")
        print(f"Tests: {len(passes)} passed, {len(failures)} failed")
        if failures:
            print(f"FAILURES: {failures}")
        else:
            print("ALL CHECKS PASSED")
        print(f"{'='*60}\n")

        if failures:
            sys.exit(1)

    asyncio.run(_probe())
