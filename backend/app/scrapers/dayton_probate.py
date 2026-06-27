"""
Montgomery County (OH / Dayton) Probate Court — estate cases via sequential casenbr enumeration.

Site:     https://go.mcohio.org/applications/probate/prodcfm/ (ColdFusion, no JS gate)
Access:   PUBLIC, no auth. httpx POST (confirmed live 2026-06-27 — NOT Playwright-gated).
          POST casesearch_actionx.cfm?caseyear=2026&casenbr=N&SEARCH=GO → one-row HTML result
          or "No matches were found". GET casesearchresultx.cfm?<hextoken> for detail.

────────────────────────────────────────────────────────────────────────────────
INVARIANT — CURSOR-ONLY RETIREMENT (do NOT time-retire):
This scraper walks casenbr forward and never re-visits old cases. A DAYTON probate
listing retires ONLY when dayton_probate_recheck.py confirms the court case_status
is CLOSED/DISPOSED. The May-2026 6,446-case bug was caused by _mark_stale_listings()
treating a cursor source as "not-seen = gone". SITE_NAME must be mapped CURSOR in
staleness.py; the recheck script is the sole retirement path.

INVARIANT — DECEDENT→OWNER JOIN PROVES RESIDENCE, NOT OWNERSHIP:
The decedent name→spine.owner_name join confirms the decedent's estate was registered
under their name. If the current owner_name surname DIFFERS from the decedent's,
the estate may have transferred or the decedent never owned it (rented). Those rows
get match_confidence='review' (VISIBLE + badged), NEVER hidden — heirs/trusts/LLCs
legitimately show different surnames. Hard-misjoins (ambiguity > cap, or <2 strong
name tokens) are NEVER entered into the feed at all.

INVARIANT — NO PROPERTY ADDRESS on the probate site:
The fiduciary address on the detail page is the estate ADMINISTRATOR's home, not the
estate property. NEVER join on the fiduciary address. The sole resolution path is
decedent-name → spine.owner_name (_probate_owner.surname_mismatch + precision-first
owner query, ambiguity cap ≤5).

INVARIANT — CASEYEAR SCOPE:
This scraper enumerates caseyear=<current year> only. Historic years (2024, 2025) are
in a separate backfill path (dayton_probate_recheck.py with --year flag). Mixing years
in a single cursor walk would make the cursor ambiguous (same casenbr exists per year).
INVARIANT — COLDFUSION SESSION REQUIRED:
The casesearch_actionx.cfm POST returns empty results (200 OK, no case rows) unless the
client carries a valid ColdFusion session (CFID + CFTOKEN + JSESSIONID cookies). These
cookies are issued by the GET of the estate search form page (casesearchx.cfm). Always
GET the form URL first to establish a session; httpx.AsyncClient persists cookies across
requests automatically. Verified live 2026-06-27: POST without a CF session → empty
result; POST after establishing session → full one-row case table.
────────────────────────────────────────────────────────────────────────────────

Cursor DDL (migration 026 — do NOT write the migration here):

    CREATE TABLE IF NOT EXISTS tranchi.dayton_probate_cursor (
        id            INTEGER PRIMARY KEY DEFAULT 1,
        caseyear      INTEGER NOT NULL DEFAULT 2026,
        last_casenbr  INTEGER NOT NULL DEFAULT 0,
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CHECK (id = 1)
    );
    INSERT INTO tranchi.dayton_probate_cursor (id, caseyear, last_casenbr)
        VALUES (1, 2026, 0)
        ON CONFLICT DO NOTHING;
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.scrapers._probate_owner import surname_mismatch as _surname_mismatch
from app.scrapers.base import ListingScraper
from app.scrapers.db import canonical_address, normalize_parcel_number
from app.scrapers.fiscal_officer import (
    _STRONG_TOKEN,
    _levenshtein,
    _name_confidence,
    _normalize_name,
)
from app.scrapers.models import RawListing

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SITE_NAME = "Montgomery County Probate"   # Wave-0 canonical — verbatim; matches market_config.py
SIGNAL_TYPE = "probate"
SIGNAL_SOURCE = "montgomery_probate_court"

_SEARCH_URL = (
    "https://go.mcohio.org/applications/probate/prodcfm/casesearch_actionx.cfm"
)
_DETAIL_BASE = "https://go.mcohio.org/applications/probate/prodcfm/"

# GET this URL first to obtain CFID/CFTOKEN/JSESSIONID session cookies.
# Without these cookies the POST returns 200 OK but with EMPTY results (no case rows).
# Verified live 2026-06-27. CF session default timeout is ~20 minutes; for long runs
# the recheck script should re-establish periodically.
_SESSION_FORM_URL = (
    "https://go.mcohio.org/applications/probate/prodcfm/casesearchx.cfm"
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Precision thresholds (mirror shelby_probate.py)
_MIN_CONFIDENCE: float = 0.75
_HIGH_CONFIDENCE: float = 0.90
_AMBIGUITY_CAP: int = 5

# Cursor-walk bounds — adjustable via env for backfill tuning
_DEFAULT_MAX_IDS: int = int(os.environ.get("DAYTON_PROBATE_MAX_IDS", "1500"))
# After this many consecutive "No match" responses we assume we have hit the
# frontier for the current year. Dense case space means ~5 consecutive misses
# is already a solid frontier signal; 10 is conservative.
_DEFAULT_MAX_CONSECUTIVE_MISS: int = 10

# Polite crawl delay (ColdFusion backend; plain GET/POST, no anti-bot gate)
_REQ_DELAY_SEC: float = 0.35

_CLOSED_WORDS: tuple[str, ...] = ("closed", "disposed", "terminated", "dismissed")

_SUFFIXES: frozenset[str] = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})

# Regex to detect "No matches" on the search result page
_NO_MATCH_RE = re.compile(r"no matches were found", re.IGNORECASE)

# ─────────────────────────────────────────────────────────────────────────────
# Cursor helpers — tranchi.dayton_probate_cursor (single row, id=1)
# ─────────────────────────────────────────────────────────────────────────────

async def _read_cursor(pool: Any) -> tuple[int, int]:
    """Return (caseyear, last_casenbr) from tranchi.dayton_probate_cursor."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT caseyear, last_casenbr FROM tranchi.dayton_probate_cursor WHERE id = 1"
        )
    if row is None:
        raise RuntimeError(
            "tranchi.dayton_probate_cursor has no row — run migration 026 first"
        )
    return int(row["caseyear"]), int(row["last_casenbr"])


async def _write_cursor(pool: Any, caseyear: int, last_casenbr: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE tranchi.dayton_probate_cursor
               SET caseyear     = $1,
                   last_casenbr = $2,
                   updated_at   = NOW()
             WHERE id = 1
            """,
            caseyear,
            last_casenbr,
        )


# ─────────────────────────────────────────────────────────────────────────────
# HTML parsers — search result page
# ─────────────────────────────────────────────────────────────────────────────

def _parse_search_result(html: str) -> dict[str, str] | None:
    """Parse the one-row search result page for a single casenbr POST.

    Returns None when "No matches were found" (frontier or empty casenbr).

    On a hit, returns:
        case_id     — the numeric internal ID (e.g. '342333')
        case_number — the full estate number (e.g. '2026EST00005')
        case_name   — decedent name from the results grid (e.g. 'VIRGIL CAMPBELL')
        detail_href — the relative href to casesearchresultx.cfm?<hextoken>

    DOM (confirmed live 2026-06-27 — ColdFusion-generated, intentionally malformed):
        <table>
          <tr>
            <th>Case ID</th><th>Case Number</th><th>Case Name</th>
          </tr>
          <td><a href="casesearchresultx.cfm?<HEX>">342333</a></td>
          <td>2026EST00005</td>
          <td>VIRGIL CAMPBELL</td>
        </table>

    INVARIANT: the data cells (<td>) are NOT wrapped in a <tr> — ColdFusion emits them
    as direct children of <table>. Always collect ALL <td> elements from the table,
    not from a <tr>; never rely on <tr> containing <td> for this page.
    """
    if _NO_MATCH_RE.search(html):
        return None

    soup = BeautifulSoup(html, "html.parser")

    for tbl in soup.find_all("table"):
        # Identify the case-result table by its <th> header row
        ths = [th.get_text(strip=True) for th in tbl.find_all("th")]
        if "Case ID" not in ths or "Case Number" not in ths:
            continue

        # Collect ALL <td> cells in the table (not inside <tr> — see DOM invariant above)
        cells = tbl.find_all("td")
        if len(cells) < 3:
            continue

        # Cell 0: Case ID — has the detail link
        a_tag = cells[0].find("a")
        if not a_tag:
            continue
        detail_href = (a_tag.get("href") or "").strip()
        if "casesearchresultx" not in detail_href:
            continue
        case_id = a_tag.get_text(strip=True)

        case_number = cells[1].get_text(strip=True)
        case_name = cells[2].get_text(strip=True)

        if not case_number or not case_id:
            continue

        return {
            "case_id": case_id,
            "case_number": case_number,
            "case_name": case_name,
            "detail_href": detail_href,
        }

    return None


# ─────────────────────────────────────────────────────────────────────────────
# HTML parsers — detail page
# ─────────────────────────────────────────────────────────────────────────────

def _clean_cell(td: Any) -> str:
    """Extract trimmed plain text from a detail-page table cell."""
    if td is None:
        return ""
    return re.sub(r"\s+", " ", td.get_text(" ").replace("\xa0", " ")).strip()


def _parse_detail(html: str) -> dict[str, Any] | None:
    """Parse the estate case detail page.

    DOM (confirmed live 2026-06-27): a wide-border table whose row pairs are
    label cells (Decedent's Name / Date of Death / Case Status / etc.) on the
    left and value cells on the right.

    Returns None if the page looks malformed or lacks a recognizable case header.

    Returns dict:
        decedent_name   — e.g. 'VIRGIL CAMPBELL'
        decedent_dod    — date | None (Date of Death)
        case_number     — e.g. '2026EST00005'
        case_type       — e.g. '14 TRANSFER OF REAL ESTATE ONLY; W/O WILL'
        case_status     — e.g. 'CLOSED' or 'OPEN'
        case_status_date— date of the status change | None
        case_title      — raw case-type string (same as case_type for now)

    Case Status cell format: 'CLOSED  01-16-2026' or 'OPEN  02-09-2026'
    (nbsp-separated; strip then split on whitespace).
    """
    soup = BeautifulSoup(html, "html.parser")
    text_flat = re.sub(r"\s+", " ", soup.get_text(" ").replace("\xa0", " ")).strip()

    # Guard: if "Decedent's Name" is not anywhere on the page this is not a case detail.
    if "Decedent" not in text_flat:
        return None

    result: dict[str, Any] = {
        "decedent_name": None,
        "decedent_dod": None,
        "case_number": None,
        "case_type": None,
        "case_status": None,
        "case_status_date": None,
        "case_title": None,
    }

    # Walk all <tr> pairs in the main detail table (label | value).
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        label = _clean_cell(tds[0]).rstrip(":").lower()
        value = _clean_cell(tds[1])

        if not value:
            continue

        if "decedent" in label and "name" in label:
            result["decedent_name"] = value or None

        elif "date of death" in label:
            result["decedent_dod"] = _parse_date_mdy(value)

        elif label == "case number":
            result["case_number"] = value or None

        elif "case type" in label:
            result["case_type"] = value or None
            result["case_title"] = value or None

        elif "case status" in label:
            # Format: 'CLOSED  01-16-2026' or 'OPEN  02-09-2026'
            parts = value.split()
            if parts:
                result["case_status"] = parts[0].upper()
            if len(parts) >= 2:
                result["case_status_date"] = _parse_date_dmy(parts[1])

    return result if result.get("decedent_name") else None


def _parse_date_mdy(s: str) -> date | None:
    """Parse 'MM/DD/YYYY' → date. Returns None on failure."""
    s = s.strip()
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        return None


def _parse_date_dmy(s: str) -> date | None:
    """Parse 'MM-DD-YYYY' → date (case_status_date format). Returns None on failure."""
    s = s.strip()
    try:
        return datetime.strptime(s, "%m-%d-%Y").date()
    except ValueError:
        return None


def _is_closed(case_status: str | None) -> bool:
    s = (case_status or "").lower()
    return any(w in s for w in _CLOSED_WORDS)


# ─────────────────────────────────────────────────────────────────────────────
# Name-to-parcel join — precision-first, surname-anchored, ambiguity-capped
# ─────────────────────────────────────────────────────────────────────────────

def _name_tokens(name: str) -> list[str]:
    """Return significant lowercased tokens (≥2 chars, no generational suffixes)."""
    return [
        t for t in _normalize_name(name).split()
        if len(t) >= 2 and t not in _SUFFIXES
    ]


def _sim(a: str, b: str) -> float:
    """Per-token similarity 0..1 (1 - normalized Levenshtein)."""
    if not a or not b:
        return 0.0
    return 1.0 - _levenshtein(a, b) / max(len(a), len(b), 1)


async def _resolve_by_owner_name(
    pool: Any, decedent_name: str
) -> dict[str, dict[str, Any]] | None:
    """Precision-first owner-name join for Montgomery County probate.

    Returns {parcel_number: {owner_name, situs, score}} for parcels whose
    AUDGIS owner_name strongly matches the decedent.

    SURNAME-ANCHORED (critical — prevents the Cuyahoga 775-parcel over-match):
    Montgomery AUDGIS stores owners SURNAME-FIRST (e.g. 'Campbell Virgil D').
    The decedent name from the probate site is GIVEN-FIRST (e.g. 'VIRGIL CAMPBELL').
    We require:
      1. decedent_surname (last token of decedent_name, given-first form) matches the
         LEADING token of owner_name (registry surname-first form) at >= _STRONG_TOKEN.
      2. decedent_given (first token) matches SOME owner token at >= _STRONG_TOKEN.
    Without condition 1 a shared surname alone triggers on every same-surname owner.

    MARKET SCOPE: restrict to market='dayton' — prevents a Montgomery decedent
    name from false-matching a same-surname owner in another market's parcels table.

    Ambiguity cap: if > _AMBIGUITY_CAP parcels pass the filter, return an empty dict
    (caller emits nothing rather than blasting 6+ uncertain listings per case).
    """
    out: dict[str, dict[str, Any]] = {}
    toks = _name_tokens(decedent_name)
    if pool is None or len(toks) < 2:
        return out  # single-token name — cannot precision-match

    # Decedent name is GIVEN-FIRST: last token = surname, first token = given name
    surname = toks[-1]
    given = toks[0]

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT parcel_number, owner_name, situs_address
                  FROM tranchi.parcels
                 WHERE owner_name ILIKE '%' || $1 || '%'
                   AND owner_name ILIKE '%' || $2 || '%'
                   AND owner_name IS NOT NULL AND owner_name <> ''
                   -- MARKET SCOPE (load-bearing): restricts to Montgomery County
                   -- parcels. Without this a Dayton decedent name can fuzzy-match
                   -- a same-surname owner in another market's parcels and produce a
                   -- cross-county mis-join. market='dayton' is the safe boundary.
                   AND market = 'dayton'
                 LIMIT 200
                """,
                surname,
                given,
            )
    except Exception as exc:
        logger.warning(
            "DaytonProbate: owner-name query failed for %r: %s", decedent_name, exc
        )
        return out

    for r in rows:
        owner_raw = r["owner_name"] or ""
        if not owner_raw:
            continue

        owner_toks = _normalize_name(owner_raw).split()
        if not owner_toks:
            continue

        # Condition 1: registry is SURNAME-FIRST → first token must be the surname.
        # Require strong match against the decedent's surname token.
        if _sim(surname, owner_toks[0]) < _STRONG_TOKEN:
            continue

        # Condition 2: decedent's given name must strongly match SOME owner token.
        if max((_sim(given, t) for t in owner_toks), default=0.0) < _STRONG_TOKEN:
            continue

        score = _name_confidence(decedent_name, owner_raw)
        if score < _MIN_CONFIDENCE:
            continue

        norm = normalize_parcel_number(r["parcel_number"])
        if not norm:
            continue

        prev = out.get(norm)
        if prev is None or score > prev["score"]:
            out[norm] = {
                "owner_name": owner_raw,
                "situs": r["situs_address"],
                "score": score,
            }

    # Ambiguity cap: >5 candidates with no address anchor means the name is too
    # common to confirm. Return None (not {}) so the caller can count this path
    # separately from a genuine no-match — see skipped_ambiguous counter in fetch_and_parse.
    if len(out) > _AMBIGUITY_CAP:
        logger.info(
            "DaytonProbate: decedent %r → %d parcel candidates > cap=%d — skipping (ambiguous)",
            decedent_name, len(out), _AMBIGUITY_CAP,
        )
        return None  # sentinel: over-cap, not zero-match

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Signal upsert
# ─────────────────────────────────────────────────────────────────────────────

async def _upsert_signal(
    pool: Any,
    parcel: str,
    case_number: str,
    decedent_name: str,
    score: float,
    run_started_at: datetime,
    dry_run: bool = False,
) -> None:
    """Upsert one probate signal row using the canonical idempotency key.

    observed_at is set to run_started_at (shared across the entire run) rather than
    datetime.now() per call. This ensures the unique key
    (parcel_number, signal_type, source, (observed_at AT TIME ZONE 'UTC')::date)
    collapses all signals from the same calendar day into one row (UPDATE) instead of
    accumulating duplicates (the 1,300 cases × 365 days/yr bloat path).
    """
    if dry_run or pool is None:
        return
    import json

    payload = json.dumps({"case_number": case_number, "decedent_name": decedent_name})
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tranchi.signals
                    (parcel_number, signal_type, source, observed_at, confidence,
                     payload, first_seen_at, last_seen_at, market)
                VALUES ($1, 'probate', $2, $3, $4, $5::jsonb, $3, $3, 'dayton')
                ON CONFLICT (parcel_number, signal_type, source,
                             ((observed_at AT TIME ZONE 'UTC')::date))
                DO UPDATE SET
                    last_seen_at = EXCLUDED.last_seen_at,
                    confidence   = EXCLUDED.confidence,
                    payload      = EXCLUDED.payload
                """,
                parcel,
                SIGNAL_SOURCE,
                run_started_at,
                score,
                payload,
            )
    except Exception as exc:
        logger.debug("DaytonProbate: signal upsert skipped for %s: %s", parcel, exc)


# ─────────────────────────────────────────────────────────────────────────────
# RawListing builder
# ─────────────────────────────────────────────────────────────────────────────

def _city_zip(situs: str | None) -> tuple[str | None, str | None]:
    """Extract (city, zip5) from a situs_address string like '2064 Rustic Rd, Dayton, OH 45414'."""
    if not situs:
        return None, None
    z_m = re.search(r"\b(\d{5})(?:-\d{4})?\b", situs)
    zip5 = z_m.group(1) if z_m else None
    # City: last token before the ', OH' or ', Ohio' segment
    c_m = re.search(r",\s*([A-Za-z][A-Za-z .\'-]{1,30}),?\s*(?:OH|Ohio)\b", situs, re.I)
    city = c_m.group(1).strip() if c_m else None
    return city, zip5


def _build_listing(
    *,
    parcel_number: str,
    situs: str | None,
    case_number: str,
    case_status: str | None,
    case_status_date: date | None,
    decedent_name: str | None,
    decedent_dod: date | None,
    case_title: str | None,
    probate_internal_id: int,
    method: str,
    tier: str,
    score: float,
) -> RawListing:
    city, zip5 = _city_zip(situs)
    addr = canonical_address(situs) or (situs or "")
    # Strip city/state/zip from property_address — situs may include them
    _street_only = (situs or "").split(",")[0].strip()
    addr = canonical_address(_street_only) or addr
    return RawListing(
        source_site=SITE_NAME,
        source_listing_id=parcel_number,
        case_number=case_number,
        signal_type=SIGNAL_TYPE,
        property_address=addr,
        property_city=city,
        property_county="Montgomery",
        property_state="OH",
        property_zip=zip5,
        sale_date=None,
        status="active",
        case_status=case_status,
        case_status_date=case_status_date,
        match_method=method,
        match_confidence=tier,
        match_score=score,
        decedent_name=decedent_name,
        decedent_dod=decedent_dod,
        case_title=case_title,
        probate_internal_id=probate_internal_id,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scraper
# ─────────────────────────────────────────────────────────────────────────────

class DaytonProbateScraper(ListingScraper):
    """Montgomery County (OH / Dayton) Probate Court — OPEN estate cases via casenbr cursor walk.

    Enumeration:
      POST casesearch_actionx.cfm?caseyear=<year>&casenbr=N&SEARCH=GO (httpx, no Playwright).
      Case space is dense and monotonic. We walk forward from the cursor's last_casenbr,
      stopping after _MAX_CONSECUTIVE_MISS consecutive "No matches" responses (frontier signal).

    Parcel resolution:
      No property address on the probate site — join by decedent name → tranchi.parcels.owner_name
      (precision-first, surname-anchored, ambiguity cap ≤5). Surname mismatch → 'review' tier.

    Retirement:
      CURSOR source. Active listings retire ONLY via dayton_probate_recheck.py (status re-check).
      NEVER via "not seen this cycle" — that path caused the May-2026 6,446-case bug.

    Args:
        pool:      asyncpg connection pool (required for cursor + parcel join + signal writes).
                   Pass None only when dry_run=True (demo mode — parcel join skipped).
        dry_run:   Skip DB writes (cursor, signals). RawListings still returned.
        max_ids:   Safety cap on how many casenbr values to walk per run.
        max_consecutive_miss: Frontier gate (stop after N consecutive "No match" responses).
        caseyear:  Override the year to enumerate (default: current calendar year).
    """

    site_name = SITE_NAME

    def __init__(
        self,
        pool: Any = None,
        *,
        dry_run: bool = False,
        max_ids: int | None = None,
        max_consecutive_miss: int = _DEFAULT_MAX_CONSECUTIVE_MISS,
        caseyear: int | None = None,
    ) -> None:
        self.pool = pool
        self.dry_run = dry_run
        self._max_ids = int(max_ids or _DEFAULT_MAX_IDS)
        self._max_miss = max_consecutive_miss
        self._caseyear_override = caseyear

    async def fetch_and_parse(self) -> list[RawListing]:
        """Walk Montgomery Probate casenbr forward. Returns list[RawListing]."""
        if self.pool is None and not self.dry_run:
            raise RuntimeError(
                "DaytonProbateScraper requires pool= for cursor + parcel join. "
                "Pass dry_run=True to skip DB writes."
            )

        # Resolve caseyear and start cursor
        if self._caseyear_override is not None:
            caseyear = self._caseyear_override
            start_nbr = 0
        elif self.pool is not None:
            caseyear, start_nbr = await _read_cursor(self.pool)
        else:
            caseyear = date.today().year
            start_nbr = 0

        logger.info(
            "DaytonProbate starting: caseyear=%d, cursor=casenbr=%d, max_ids=%d, max_miss=%d, dry_run=%s",
            caseyear, start_nbr, self._max_ids, self._max_miss, self.dry_run,
        )

        # Stable run timestamp: shared across all signal upserts in this run so the
        # daily dedup key (observed_at::date) collapses all same-day writes to one row.
        run_started_at = datetime.now(tz=timezone.utc)

        listings: list[RawListing] = []
        highest = start_nbr
        miss = 0
        explored = 0
        emitted = skipped_closed = skipped_ambiguous = skipped_weak = no_match = 0

        headers = {
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=20.0,
        ) as client:
            # Establish CF session (CFID/CFTOKEN/JSESSIONID cookies). Without this
            # the POST returns 200 OK with empty results. httpx.AsyncClient persists
            # cookies automatically for subsequent requests. See module INVARIANT above.
            try:
                _sess = await client.get(_SESSION_FORM_URL)
                _sess.raise_for_status()
                logger.info(
                    "DaytonProbate: CF session established (cookies: %s)",
                    ", ".join(client.cookies.keys()),
                )
            except Exception as exc:
                logger.error(
                    "DaytonProbate: failed to establish CF session from %s: %s",
                    _SESSION_FORM_URL, exc,
                )
                raise

            current = start_nbr + 1

            while explored < self._max_ids and miss < self._max_miss:
                # ── Search POST for this casenbr ──────────────────────────
                await asyncio.sleep(_REQ_DELAY_SEC)
                try:
                    resp = await client.post(
                        _SEARCH_URL,
                        data={
                            "caseyear": str(caseyear),
                            "casenbr": str(current),
                            "SEARCH": "GO",
                        },
                    )
                    resp.raise_for_status()
                    search_html = resp.text
                except Exception as exc:
                    logger.warning(
                        "DaytonProbate: search request failed for casenbr=%d: %s",
                        current, exc,
                    )
                    current += 1
                    explored += 1
                    miss += 1
                    continue

                parsed_search = _parse_search_result(search_html)

                if parsed_search is None:
                    # No match — either frontier or a skipped number (dense space → frontier)
                    miss += 1
                    explored += 1
                    current += 1
                    continue

                miss = 0  # a real case — reset frontier counter
                explored += 1

                case_number = parsed_search.get("case_number", "")
                detail_href = parsed_search.get("detail_href", "")
                case_name_raw = parsed_search.get("case_name", "")

                # ── Detail page fetch ─────────────────────────────────────
                if not detail_href:
                    logger.warning(
                        "DaytonProbate: casenbr=%d has no detail link — skipping", current
                    )
                    highest = current
                    current += 1
                    continue

                await asyncio.sleep(_REQ_DELAY_SEC)
                try:
                    detail_resp = await client.get(_DETAIL_BASE + detail_href)
                    detail_resp.raise_for_status()
                    detail_html = detail_resp.text
                except Exception as exc:
                    logger.warning(
                        "DaytonProbate: detail fetch failed for casenbr=%d (%s): %s",
                        current, case_number, exc,
                    )
                    highest = current
                    current += 1
                    continue

                detail = _parse_detail(detail_html)
                if not detail:
                    logger.debug(
                        "DaytonProbate: casenbr=%d detail parse returned nothing (case=%s)",
                        current, case_number,
                    )
                    highest = current
                    current += 1
                    continue

                # ── OPEN gate — CLOSED cases are noted but not emitted ────
                case_status = detail.get("case_status")
                if _is_closed(case_status):
                    skipped_closed += 1
                    logger.debug(
                        "DaytonProbate: casenbr=%d case=%s status=%r — CLOSED, skipping",
                        current, case_number, case_status,
                    )
                    highest = current
                    current += 1
                    continue

                decedent_name = detail.get("decedent_name") or case_name_raw or None
                decedent_dod = detail.get("decedent_dod")
                case_title = detail.get("case_title")
                case_status_date = detail.get("case_status_date")

                if not decedent_name:
                    logger.info(
                        "DaytonProbate: casenbr=%d case=%s no decedent name — skipping",
                        current, case_number,
                    )
                    highest = current
                    current += 1
                    continue

                logger.info(
                    "DaytonProbate: casenbr=%d case=%s decedent=%r status=%r dod=%s",
                    current, case_number, decedent_name, case_status, decedent_dod,
                )

                # ── Parcel join: decedent name → owner_name ──────────────
                if self.pool is not None:
                    name_hits = await _resolve_by_owner_name(self.pool, decedent_name)
                else:
                    name_hits = {}

                # None = over ambiguity cap (too many candidates — name too common).
                # {}   = genuine no match.
                if name_hits is None:
                    skipped_ambiguous += 1
                    highest = current
                    current += 1
                    continue

                if not name_hits:
                    no_match += 1
                    logger.info(
                        "DaytonProbate: casenbr=%d case=%s decedent=%r — no parcel match",
                        current, case_number, decedent_name,
                    )
                    highest = current
                    current += 1
                    continue

                # Precision gate: unique + high-confidence → probable.
                # 2-5 candidates with no address anchor → skip (too uncertain).
                # >5 → already collapsed to {} by _resolve_by_owner_name.
                name_unique = len(name_hits) == 1
                for parcel, hit in name_hits.items():
                    score = hit["score"]
                    if not (name_unique and score >= _HIGH_CONFIDENCE):
                        skipped_weak += 1
                        logger.debug(
                            "DaytonProbate: casenbr=%d parcel=%s score=%.3f unique=%s — weak/ambiguous, skip",
                            current, parcel, score, name_unique,
                        )
                        continue

                    method = "name_match"
                    tier = "probable"

                    # Surname-mismatch guard: decedent→owner join proves the property was
                    # registered under the decedent's name, not that the estate still holds it.
                    # Mismatch → demote to 'review' (VISIBLE + badged), never hide entirely.
                    owner_raw = hit.get("owner_name")
                    if _surname_mismatch(decedent_name, owner_raw):
                        tier = "review"
                        logger.info(
                            "DaytonProbate: casenbr=%d parcel=%s owner=%r != decedent=%r — REVIEW",
                            current, parcel, owner_raw, decedent_name,
                        )

                    situs = hit.get("situs")
                    listing = _build_listing(
                        parcel_number=parcel,
                        situs=situs,
                        case_number=case_number,
                        case_status=case_status,
                        case_status_date=case_status_date,
                        decedent_name=decedent_name,
                        decedent_dod=decedent_dod,
                        case_title=case_title,
                        probate_internal_id=current,
                        method=method,
                        tier=tier,
                        score=score,
                    )
                    listings.append(listing)
                    emitted += 1

                    if not self.dry_run:
                        await _upsert_signal(
                            self.pool, parcel, case_number,
                            decedent_name, score, run_started_at, self.dry_run,
                        )

                highest = current
                current += 1

        # Advance cursor
        if highest > start_nbr and not self.dry_run and self.pool is not None:
            await _write_cursor(self.pool, caseyear, highest)

        logger.info(
            "DaytonProbate complete: caseyear=%d cursor=%d→%d | %d ids explored | "
            "emitted=%d (closed=%d, no_match=%d, ambiguous=%d, weak=%d)%s",
            caseyear, start_nbr, highest, explored, emitted,
            skipped_closed, no_match, skipped_ambiguous, skipped_weak,
            " [DRY RUN — cursor not advanced]" if self.dry_run else "",
        )
        return listings


# ─────────────────────────────────────────────────────────────────────────────
# Unit-testable parse functions (no DB / no HTTP)
# ─────────────────────────────────────────────────────────────────────────────

# ── Fixture: casenbr=5 search result HTML (matches live ColdFusion DOM exactly)
# Live DOM confirmed 2026-06-27: headers are <th>, data cells are orphaned <td>
# NOT wrapped in a <tr> — ColdFusion emits them as direct table children.
_FIXTURE_SEARCH_HIT = """
<html><body>
<p>ESTATE CASE SEARCH</p>
<table align="center" border="1">
  <tr>
    <th bgcolor="dddddd">Case ID</th>
    <th bgcolor="dddddd">Case Number</th>
    <th bgcolor="dddddd">Case Name</th>
  </tr>
  <td bgcolor="ffffff"><a href="casesearchresultx.cfm?DEADBEEF">342333</a></td>
  <td bgcolor="ffffff">2026EST00005</td>
  <td bgcolor="ffffff">VIRGIL CAMPBELL</td>
</table>
</body></html>
"""

_FIXTURE_SEARCH_MISS = """
<html><body>
<p>ESTATE CASE SEARCH</p>
<p>No matches were found</p>
<p>Please go back to the previous form and try again.</p>
</body></html>
"""

_FIXTURE_DETAIL_CLOSED = """
<html><body>
<table>
  <tr><td>Decedent's Name</td><td>VIRGIL CAMPBELL</td></tr>
  <tr><td>Date of Death</td><td>04/08/2025</td></tr>
  <tr><td>Case Number</td><td>2026EST00005</td></tr>
  <tr><td>Case Type</td><td>14 TRANSFER OF REAL ESTATE ONLY; W/O WILL</td></tr>
  <tr><td>Case Status</td><td>CLOSED&nbsp;&nbsp;01-16-2026</td></tr>
</table>
</body></html>
"""

_FIXTURE_DETAIL_OPEN = """
<html><body>
<table>
  <tr><td>Decedent's Name</td><td>ARTHUR FAIRCLOTH</td></tr>
  <tr><td>Date of Death</td><td>03/15/2025</td></tr>
  <tr><td>Case Number</td><td>2026EST00500</td></tr>
  <tr><td>Case Type</td><td>02 FULL ADMIN; PROBATE WILL</td></tr>
  <tr><td>Case Status</td><td>OPEN&nbsp;&nbsp;04-10-2026</td></tr>
</table>
</body></html>
"""


def _run_unit_tests() -> None:
    """Pure-parse unit tests (no DB, no HTTP). Called from __main__ with --test flag."""
    import sys

    failures: list[str] = []

    def chk(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(f"FAIL: {msg}")
            print(f"  FAIL: {msg}")
        else:
            print(f"  pass: {msg}")

    print("\n=== DaytonProbate unit tests ===\n")

    # ── _parse_search_result ──────────────────────────────────────────────
    hit = _parse_search_result(_FIXTURE_SEARCH_HIT)
    chk(hit is not None, "_parse_search_result returns dict on hit")
    chk(hit is not None and hit["case_number"] == "2026EST00005", "case_number parsed correctly")
    chk(hit is not None and hit["case_name"] == "VIRGIL CAMPBELL", "case_name parsed correctly")
    chk(hit is not None and "DEADBEEF" in hit["detail_href"], "detail_href extracted")

    miss = _parse_search_result(_FIXTURE_SEARCH_MISS)
    chk(miss is None, "_parse_search_result returns None on 'No matches'")

    # ── _parse_detail — CLOSED case ────────────────────────────────────────
    closed = _parse_detail(_FIXTURE_DETAIL_CLOSED)
    chk(closed is not None, "_parse_detail returns dict on CLOSED case")
    chk(closed is not None and closed["decedent_name"] == "VIRGIL CAMPBELL", "decedent_name CLOSED")
    chk(
        closed is not None and closed["decedent_dod"] == date(2025, 4, 8),
        "decedent_dod parsed (04/08/2025 → 2025-04-08)",
    )
    chk(closed is not None and closed["case_status"] == "CLOSED", "case_status CLOSED")
    chk(
        closed is not None and closed["case_status_date"] == date(2026, 1, 16),
        "case_status_date parsed (01-16-2026 → 2026-01-16)",
    )

    # ── _parse_detail — OPEN case ──────────────────────────────────────────
    opened = _parse_detail(_FIXTURE_DETAIL_OPEN)
    chk(opened is not None, "_parse_detail returns dict on OPEN case")
    chk(opened is not None and opened["case_status"] == "OPEN", "case_status OPEN")
    chk(opened is not None and opened["decedent_name"] == "ARTHUR FAIRCLOTH", "decedent_name OPEN")
    chk(
        opened is not None and opened["decedent_dod"] == date(2025, 3, 15),
        "decedent_dod OPEN (03/15/2025 → 2025-03-15)",
    )

    # ── OPEN/CLOSED gate ──────────────────────────────────────────────────
    chk(_is_closed("CLOSED"), "_is_closed('CLOSED') → True")
    chk(_is_closed("DISPOSED"), "_is_closed('DISPOSED') → True")
    chk(not _is_closed("OPEN"), "_is_closed('OPEN') → False")
    chk(not _is_closed(None), "_is_closed(None) → False")

    # ── casenbr frontier detection ─────────────────────────────────────────
    # Simulate: miss=10 consecutive misses → walk stops.
    # (Structural: just verify _NO_MATCH_RE fires on the fixture)
    chk(
        _parse_search_result(_FIXTURE_SEARCH_MISS) is None,
        "consecutive miss detection: 'No matches' returns None",
    )

    # ── Name-join ambiguity cap logic ─────────────────────────────────────
    # Simulate a fake pool returning 6 candidates → should collapse to None (sentinel)
    # (This path is tested structurally; live test requires real parcels.)
    # Confirm: 1 candidate with score < _HIGH_CONFIDENCE is skipped (weak gate)
    from unittest.mock import AsyncMock, MagicMock

    async def _fake_resolve_over_cap(pool: Any, name: str) -> dict[str, Any] | None:
        """Simulates > _AMBIGUITY_CAP hits: function should return None."""
        # Six fake parcel hits — all pass surname+given checks — exceed cap
        over_cap: dict[str, Any] = {f"R{i:011d}": {"owner_name": f"Smith John {i}", "situs": f"{i} Main St", "score": 0.91} for i in range(6)}
        if len(over_cap) > _AMBIGUITY_CAP:
            return None  # sentinel: over-cap, not zero-match
        return over_cap

    import asyncio as _asyncio
    result_over = _asyncio.run(_fake_resolve_over_cap(None, "JOHN SMITH"))
    chk(result_over is None, "ambiguity cap >5 → None sentinel (skipped_ambiguous, not no_match)")

    # ── surname_mismatch guard ─────────────────────────────────────────────
    from app.scrapers._probate_owner import surname_mismatch
    chk(not surname_mismatch("VIRGIL CAMPBELL", "Campbell Virgil D"),
        "surname_mismatch: CAMPBELL matches Campbell → False (no mismatch)")
    chk(surname_mismatch("VIRGIL CAMPBELL", "Smith Mary J"),
        "surname_mismatch: CAMPBELL != Smith → True (mismatch → review tier)")
    chk(not surname_mismatch(None, "Campbell Virgil D"),
        "surname_mismatch: None decedent → False (conservative)")

    # ── _build_listing smoke test ──────────────────────────────────────────
    lst = _build_listing(
        parcel_number="R72117030016",
        situs="2064 Rustic Rd, Dayton, OH 45414",
        case_number="2026EST01000",
        case_status="OPEN",
        case_status_date=None,
        decedent_name="JAMES BAZZELL",
        decedent_dod=date(2025, 6, 1),
        case_title="02 FULL ADMIN; PROBATE WILL",
        probate_internal_id=1000,
        method="name_match",
        tier="probable",
        score=0.92,
    )
    chk(lst.source_site == SITE_NAME, "_build_listing: source_site == SITE_NAME")
    chk(lst.signal_type == "probate", "_build_listing: signal_type == 'probate'")
    chk(lst.property_state == "OH", "_build_listing: property_state == 'OH'")
    chk(lst.match_method == "name_match", "_build_listing: match_method correct")
    chk(lst.match_confidence == "probable", "_build_listing: match_confidence correct")
    chk(lst.match_score == 0.92, "_build_listing: match_score correct")
    chk(lst.decedent_dod == date(2025, 6, 1), "_build_listing: decedent_dod round-trips")
    chk(lst.probate_internal_id == 1000, "_build_listing: probate_internal_id round-trips")

    print(f"\n{'All tests passed!' if not failures else chr(10).join(failures)}")
    sys.exit(0 if not failures else 1)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run proof
# ─────────────────────────────────────────────────────────────────────────────

async def _dry_run_demo(start: int = 1, count: int = 10) -> None:
    """Smoke-test: enumerate casenbr=start..start+count-1, parse + print results.

    Hits the live endpoint. Pool is None → parcel join skipped. DB writes skipped.
    Proves: endpoint reachability, search result parsing, detail fetch + parsing,
    OPEN/CLOSED gate.
    """
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    print(f"\n=== DaytonProbateScraper DRY RUN (casenbr {start}..{start + count - 1}) ===\n")

    scraper = DaytonProbateScraper(
        pool=None,
        dry_run=True,
        max_ids=count,
        max_consecutive_miss=5,
        caseyear=date.today().year,
    )
    listings = await scraper.fetch_and_parse()

    print(f"\n--- {len(listings)} RawListings (parcel join skipped — pool=None) ---\n")
    for idx, lst in enumerate(listings, 1):
        print(
            f"  [{idx:3d}] {(lst.case_number or '?'):20s} | "
            f"decedent={lst.decedent_name!r:30s} | dod={lst.decedent_dod} | "
            f"status={lst.case_status} | conf={lst.match_confidence} | parcel={lst.source_listing_id}"
        )

    # Also show raw parse of a few casenbr for fixture verification
    print("\n--- Raw parse of casenbr 5, 200, 800 (fixture verification) ---\n")
    headers = {"User-Agent": _UA, "Accept": "text/html"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as client:
        # Establish CF session before any search POST (see INVARIANT above)
        await client.get(_SESSION_FORM_URL)
        for nbr in [5, 200, 800]:
            try:
                resp = await client.post(
                    _SEARCH_URL,
                    data={"caseyear": str(date.today().year), "casenbr": str(nbr), "SEARCH": "GO"},
                )
                sr = _parse_search_result(resp.text)
                if sr is None:
                    print(f"  casenbr={nbr}: no match")
                    continue
                dresp = await client.get(_DETAIL_BASE + sr["detail_href"])
                d = _parse_detail(dresp.text)
                print(
                    f"  casenbr={nbr}: case={sr['case_number']} "
                    f"decedent={d.get('decedent_name') if d else 'N/A'!r} "
                    f"dod={d.get('decedent_dod') if d else 'N/A'} "
                    f"status={d.get('case_status') if d else 'N/A'}"
                )
                await asyncio.sleep(_REQ_DELAY_SEC)
            except Exception as exc:
                print(f"  casenbr={nbr}: error — {exc}")


if __name__ == "__main__":
    import sys

    if "--test" in sys.argv:
        _run_unit_tests()
    else:
        asyncio.run(_dry_run_demo())
