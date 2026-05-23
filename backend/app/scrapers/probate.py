"""
Cuyahoga County Probate Court scraper — Estate cases only.

Site:     https://probate.cuyahogacounty.gov/pa/
Platform: ProWare ASP.NET WebForms (Build 2.6.0416).

STRATEGY: ID CURSOR ENUMERATION
The probate search form has NO date-range filter — only Case Year. Rather
than sweeping alphabetically by name (expensive and yields full history),
we exploit the fact that the detail URL encodes a monotonically increasing
internal integer:

    CaseSummary.aspx?q=<base64(int_id)>

Every cron run reads the last successfully ingested int ID from
tranchi.probate_cursor, increments forward, and stops after
PROBATE_MAX_CONSECUTIVE_MISS consecutive IDs that return no Estate case.
This gives us a true delta: only cases filed since the last run.

ToS POSTURE (mandatory — this is the only scraper with explicit anti-mining language):
- 1 req/sec floor enforced by ProwareSession._rate_limiter()
- Generic Chrome User-Agent via user_agents.random_ua()
- Estate cases (EST) only — we never pull Guardianship, Trust, or other types
- Delta-only via cursor — never bulk-pull historical records
- No self-identification in headers

DUAL-PATH PARCEL RESOLUTION (address_anchor + name_match):
For each Estate case, parcel resolution uses TWO paths in parallel:

  Path A — address_anchor (high confidence, 0.95+):
    If decedent_address is present on the CaseParties page, call
    fiscal_officer.search_by_address(decedent_address). This finds the
    "home" parcel directly, even when the owner-name search misses it
    (married names, typos, abbreviated names in MyPlace). Tagged in listing
    metadata as join_method="address_anchor".

  Path B — name_match (fuzzy, 0.75+):
    Always call fiscal_officer.search_by_owner(decedent_name). Catches
    additional owned parcels (investment properties, lots) that the address
    path would miss. Tagged join_method="name_match".

Deduplication: if the same parcel_number appears in both paths, keep the
address_anchor version (higher confidence). This is the main hit-rate
improvement: previously, if name_match returned zero, we emitted no listing.
Now, a successful address_anchor still emits the home parcel.

DATA FLOW PER ESTATE CASE:
1. GET CaseSummary.aspx?q=<b64(id)>
   → parse: case_number, case_title, filing_date, case_status, judge
   → check Case Category is EST; skip if not
2. GET CaseParties.aspx?q=<b64(id)>
   → parse: decedent name, DOD (from "(DOD: MM/DD/YYYY)" suffix), decedent address
   → parse: executor/administrator name (role EX or AD)
   → parse: attorney name (role AT)
3. Dual-path resolve:
   a. search_by_address(decedent_address)  → address_anchor matches
   b. search_by_owner(decedent_name)       → name_match matches
   Dedupe by parcel_number (address_anchor wins on conflict).
4. Emit one RawListing + one tranchi.signals row per unique parcel

INVARIANT: The T&C agreement page must be POSTed before any CaseSummary
fetch. The ProwareSession.accept_agreement() call mints the ASP.NET_SessionId
cookie. Session must be reused across all ID fetches in a single run.

INVARIANT: tranchi.probate_cursor always has exactly one row (id=1).
The scraper reads/writes only that row. Never DELETE it.

INVARIANT: A RawListing is emitted whenever at least one parcel is found
via either path. A case with zero matches on both paths produces no listing
(the decedent may have been a renter with no Cuyahoga parcel ownership).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any

from bs4 import BeautifulSoup

from app.scrapers.base import ListingScraper
from app.scrapers.models import RawListing
from app.scrapers.proware_client import ProwareSession
from app.scrapers.user_agents import random_ua
from app.scrapers._time import today_et
from app.scrapers.fiscal_officer import search_by_address, search_by_owner, ParcelMatch

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_BASE_URL = "https://probate.cuyahogacounty.gov"
_PA_ROOT = "/pa"
_TERMS_PATH = "/pa/"
_SEARCH_PATH = "/pa/CaseSearch.aspx"
_SUMMARY_PATH = "/pa/CaseSummary.aspx"
_PARTIES_PATH = "/pa/CaseParties.aspx"

# T&C gate button name (from field-map section A, verbatim Playwright probe)
# The agreement page uses button name "ctl00$mpContentPH$btnYes" not a simple "btnYes"
# Accept button discovered: id="btnYes" in probe screenshot analysis.
_AGREE_BUTTON = "ctl00$mpContentPH$btnYes"

# Case category code for Estate (from field-map section A, verbatim)
_ESTATE_CATEGORY = "EST"

# How many consecutive IDs with no Estate case before we stop enumeration.
# This prevents runaway runs when we reach the frontier of new filings.
_DEFAULT_MAX_CONSECUTIVE_MISS = 25

# Hard cap on IDs explored per run. Overridden by PROBATE_MAX_IDS env var.
_DEFAULT_MAX_IDS = 200

# Minimum fiscal_officer confidence to emit a listing
_MIN_CONFIDENCE = 0.75

# Minimum confidence to flag as "high confidence" (vs. ambiguous)
_HIGH_CONFIDENCE = 0.90

# Party role codes (from field-map section A)
_ROLE_DECEDENT = "DECEDENT"
_ROLE_EXECUTOR = {"EXECUTOR", "ADMINISTRATOR", "FIDUCIARY", "APPLICANT"}
_ROLE_ATTORNEY = {"ATTORNEY"}

# ─────────────────────────────────────────────────────────────────────────────
# URL encoding helpers
# ─────────────────────────────────────────────────────────────────────────────

def _int_to_q(internal_id: int) -> str:
    """Encode a ProWare internal int ID to the base64 `q` parameter.

    The detail URL is CaseSummary.aspx?q=<base64(str(int_id))>.
    Example: 2818155 → b'2818155' → 'MjgxODE1NQ=='
    """
    return base64.b64encode(str(internal_id).encode()).decode()


def _summary_url(internal_id: int) -> str:
    return f"{_SUMMARY_PATH}?q={_int_to_q(internal_id)}"


def _parties_url(internal_id: int) -> str:
    return f"{_PARTIES_PATH}?q={_int_to_q(internal_id)}"


# ─────────────────────────────────────────────────────────────────────────────
# HTML parsers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_case_summary(html: str) -> dict[str, Any] | None:
    """
    Parse CaseSummary.aspx page.

    Returns a dict with keys:
        case_number, case_title, case_type, filing_date (date),
        case_status, status_date (date | None), judge, category_code (str)
    Returns None if the page indicates no case found (404-equivalent in ProWare:
    the case summary area is empty or contains "Case Not Found" language).
    Returns None if the case category is not EST.
    """
    soup = BeautifulSoup(html, "html.parser")

    # ProWare renders a "No Record Found" or blank case area when an ID has
    # no case. Look for the case number label as a proxy for "real case here".
    page_text = soup.get_text(separator="\n", strip=True)

    # No-case indicators
    if any(phrase in page_text for phrase in (
        "No Record Found",
        "no cases found",
        "Case Not Found",
        "Invalid case",
    )):
        return None

    # Extract field values from the label/value pairs in the detail table.
    # ProWare renders these as adjacent <td> or label/span pairs.
    # We use a label-text → next-sibling-text pattern.
    def _find_field(label: str) -> str | None:
        tag = soup.find(string=re.compile(re.escape(label), re.I))
        if tag is None:
            return None
        # Walk to the nearest sibling or parent's next text node
        parent = tag.find_parent()
        if parent is None:
            return None
        # Try next sibling td/span
        nxt = parent.find_next_sibling()
        if nxt:
            val = nxt.get_text(strip=True)
            if val:
                return val
        # Try parent's next sibling
        nxt = parent.find_parent()
        if nxt:
            nxt2 = nxt.find_next_sibling()
            if nxt2:
                val = nxt2.get_text(strip=True)
                if val:
                    return val
        return None

    case_number = _find_field("Case Number")
    if not case_number:
        # Second attempt: look for pattern YYYYEST######
        m = re.search(r"\b(\d{4}[A-Z]{2,5}\d{4,7})\b", page_text)
        if m:
            case_number = m.group(1)
        else:
            # Truly empty page (valid but unassigned ID range)
            return None

    # Determine category code from case_number or "Case Type" field
    # Case number format: YYYYCAT######  e.g. 2026EST305113
    cat_m = re.match(r"^\d{4}([A-Z]+)\d+$", case_number or "")
    category_code = cat_m.group(1) if cat_m else ""

    if category_code != _ESTATE_CATEGORY:
        logger.debug("Non-estate case %s (cat=%s) — skipping", case_number, category_code)
        return None

    case_title = _find_field("Case Title") or ""
    case_type = _find_field("Case Type") or ""
    filing_date_str = _find_field("Filing Date") or ""
    case_status = _find_field("Case Status") or ""
    status_date_str = _find_field("Status Date") or ""
    judge = _find_field("Judge") or ""

    filing_date = _parse_long_date(filing_date_str)
    status_date = _parse_long_date(status_date_str)

    return {
        "case_number": case_number,
        "case_title": case_title,
        "case_type": case_type,
        "filing_date": filing_date,
        "case_status": case_status,
        "status_date": status_date,
        "judge": judge,
        "category_code": category_code,
    }


def _parse_long_date(s: str) -> date | None:
    """Parse ProWare long date format: 'FRIDAY, FEBRUARY 27, 2026'."""
    if not s:
        return None
    # Strip leading weekday if present
    s = re.sub(r"^[A-Z]+,\s*", "", s.strip(), flags=re.I)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_dod(text: str) -> date | None:
    """Extract DOD from '(DOD: MM/DD/YYYY)' suffix in decedent name cell."""
    m = re.search(r"\(DOD:\s*(\d{1,2}/\d{1,2}/\d{4})\)", text, re.I)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%m/%d/%Y").date()
    except ValueError:
        return None


def _parse_parties(html: str) -> dict[str, Any]:
    """
    Parse CaseParties.aspx page.

    Returns:
        decedent_name (str): Name without the DOD suffix
        dod (date | None): Date of death
        decedent_address (str | None): Pre-death situs address
        executor_name (str | None): First executor/administrator/fiduciary found
        attorney_name (str | None): First attorney found

    Parties page format (from field-map section A, verbatim):
        APPLICANT    KAREN SMITH PLICKA
                     338 CRAIN AVENUE
                     KENT OH 44240-0000

        DECEDENT     ANNETTE SMITH (DOD: 10/25/2025)
                     6859 HIDDEN LAKE TRAIL
                     BRECKSVILLE OH 44141

    The role label appears in one text block; the name and address lines
    follow in the next block(s).
    """
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(separator="\n", strip=True)

    result: dict[str, Any] = {
        "decedent_name": None,
        "dod": None,
        "decedent_address": None,
        "executor_name": None,
        "attorney_name": None,
    }

    # Split into lines, walk looking for role labels
    lines = [l.strip() for l in page_text.split("\n") if l.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]
        line_upper = line.upper()

        # Detect DECEDENT role
        if "DECEDENT" in line_upper and result["decedent_name"] is None:
            # The decedent name may be on the same line after "DECEDENT" or the next line
            name_raw = line_upper.replace("DECEDENT", "").strip()
            if not name_raw and (i + 1) < len(lines):
                i += 1
                name_raw = lines[i]

            dod = _parse_dod(name_raw)
            # Strip the (DOD: ...) suffix from the name
            clean_name = re.sub(r"\(DOD:.*?\)", "", name_raw, flags=re.I).strip()
            result["decedent_name"] = clean_name.title() if clean_name else None
            result["dod"] = dod

            # Next lines are the address (until we hit another role label or blank)
            addr_lines: list[str] = []
            j = i + 1
            while j < len(lines):
                next_line = lines[j]
                # Stop if this looks like another party role label
                if re.match(
                    r"^(APPLICANT|EXECUTOR|ADMINISTRATOR|ATTORNEY|FIDUCIARY|HEIR|GUARDIAN|TRUSTEE)\b",
                    next_line.upper(),
                ):
                    break
                # Stop after 3 address lines (street + city/state/zip + optional unit)
                addr_lines.append(next_line)
                if len(addr_lines) >= 3:
                    break
                j += 1

            if addr_lines:
                result["decedent_address"] = ", ".join(addr_lines)
            i = j
            continue

        # Detect executor/administrator roles
        executor_pattern = re.compile(
            r"^(EXECUTOR|ADMINISTRATOR|FIDUCIARY|APPLICANT)\b", re.I
        )
        if executor_pattern.match(line_upper) and result["executor_name"] is None:
            name_raw = executor_pattern.sub("", line).strip()
            if not name_raw and (i + 1) < len(lines):
                i += 1
                name_raw = lines[i]
            result["executor_name"] = name_raw.title() if name_raw else None
            i += 1
            continue

        # Detect attorney role
        if re.match(r"^ATTORNEY\b", line_upper) and result["attorney_name"] is None:
            name_raw = re.sub(r"^ATTORNEY\s*", "", line, flags=re.I).strip()
            if not name_raw and (i + 1) < len(lines):
                i += 1
                name_raw = lines[i]
            result["attorney_name"] = name_raw.title() if name_raw else None
            i += 1
            continue

        i += 1

    return result


# ─────────────────────────────────────────────────────────────────────────────
# DB cursor helpers (asyncpg)
# ─────────────────────────────────────────────────────────────────────────────

async def _read_cursor(pool: Any) -> int:
    """Read the current last_id from tranchi.probate_cursor."""
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT last_id FROM tranchi.probate_cursor WHERE id = 1"
        )
    if val is None:
        raise RuntimeError(
            "tranchi.probate_cursor has no row — run migration 002_probate_cursor.py first"
        )
    return int(val)


async def _write_cursor(pool: Any, last_id: int) -> None:
    """Update tranchi.probate_cursor.last_id to the new high-water mark."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE tranchi.probate_cursor
            SET last_id = $1, updated_at = NOW()
            WHERE id = 1
            """,
            last_id,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Signal upsert helper
# ─────────────────────────────────────────────────────────────────────────────

async def _upsert_probate_signal(
    pool: Any,
    parcel_number: str,
    case_number: str,
    decedent_name: str,
    dod: date | None,
    confidence: float,
    dry_run: bool = False,
) -> None:
    """Write one probate signal row to tranchi.signals.

    INVARIANT: parcel must already exist in tranchi.parcels (FK constraint).
    If the parcel is not found there yet, this call is a no-op (FK violation
    is caught, logged as debug, and skipped — probate signal will be added
    on the next fiscal_officer enrichment pass).
    """
    if dry_run:
        logger.debug(
            "[DRY RUN] Would write probate signal: parcel=%s, case=%s, decedent=%s",
            parcel_number, case_number, decedent_name,
        )
        return

    payload = {
        "case_number": case_number,
        "decedent_name": decedent_name,
        "dod": dod.isoformat() if dod else None,
    }
    import json
    payload_json = json.dumps(payload)
    now = datetime.now(tz=timezone.utc)

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tranchi.signals
                    (parcel_number, signal_type, source, observed_at,
                     confidence, payload, first_seen_at, last_seen_at)
                VALUES ($1, 'probate', 'probate_court', $2, $3, $4::jsonb, $2, $2)
                ON CONFLICT DO NOTHING
                """,
                parcel_number,
                now,
                confidence,
                payload_json,
            )
    except Exception as exc:
        # FK violation means parcel not yet in tranchi.parcels — non-fatal.
        logger.debug(
            "probate signal insert skipped for parcel %s (parcel not in registry yet): %s",
            parcel_number, exc,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main scraper class
# ─────────────────────────────────────────────────────────────────────────────

class ProbateScraper(ListingScraper):
    """
    Cuyahoga Probate Court — Estate cases via ID cursor enumeration.

    Each run:
    1. Reads cursor from tranchi.probate_cursor
    2. Walks forward, fetching CaseSummary + CaseParties for each ID
    3. For Estate cases, calls fiscal_officer.search_by_owner(decedent_name)
    4. Emits one RawListing per matched parcel
    5. Updates cursor to highest ID successfully processed

    Rate limit: 1 req/sec enforced by ProwareSession (ToS requirement).

    Args:
        pool:       asyncpg connection pool (required for cursor reads)
        dry_run:    If True, do not update cursor or write signals to DB.
                    RawListings are still returned for prefilter inspection.
        max_ids:    Hard cap on IDs to explore this run. Env var
                    PROBATE_MAX_IDS overrides this. Default 200.
        max_consecutive_miss: Stop after this many non-Estate IDs in a row.
    """

    site_name = "Cuyahoga Probate Court"

    def __init__(
        self,
        pool: Any | None = None,
        *,
        dry_run: bool = False,
        max_ids: int | None = None,
        max_consecutive_miss: int = _DEFAULT_MAX_CONSECUTIVE_MISS,
    ) -> None:
        self._pool = pool
        self._dry_run = dry_run
        self._max_ids = int(os.environ.get("PROBATE_MAX_IDS", max_ids or _DEFAULT_MAX_IDS))
        self._max_consecutive_miss = max_consecutive_miss

    async def fetch_and_parse(self) -> list[RawListing]:
        """
        Enumerate probate IDs from cursor, return RawListings for Estate cases
        that resolve to at least one parcel via fiscal_officer.

        Side effects (when dry_run=False):
        - Updates tranchi.probate_cursor.last_id
        - Inserts rows into tranchi.signals (one per parcel match)
        """
        if self._pool is None:
            raise RuntimeError(
                "ProbateScraper requires a pool= argument to read/write the cursor. "
                "Pass dry_run=True to run without a pool (cursor is not updated)."
            )

        # Read starting cursor
        start_id = await _read_cursor(self._pool)
        logger.info(
            "ProbateScraper starting: cursor=%d, max_ids=%d, max_miss=%d",
            start_id, self._max_ids, self._max_consecutive_miss,
        )

        all_listings: list[RawListing] = []
        highest_ingested = start_id
        consecutive_miss = 0
        ids_explored = 0

        async with ProwareSession(_BASE_URL, rate_limit_sec=1.0) as session:
            # ── Step 1: Accept T&C to get session cookie ──────────────────────
            try:
                await session.accept_agreement(path=_TERMS_PATH, agree_button_id=_AGREE_BUTTON)
                logger.info("ProbateScraper: T&C accepted, session established")
            except Exception as exc:
                logger.error("ProbateScraper: failed to accept T&C agreement: %s", exc)
                raise

            # ── Step 2: Walk ID space ─────────────────────────────────────────
            current_id = start_id + 1

            while ids_explored < self._max_ids and consecutive_miss < self._max_consecutive_miss:
                ids_explored += 1
                q_param = _int_to_q(current_id)

                # Fetch CaseSummary
                summary_data: dict[str, Any] | None = None
                try:
                    # ProwareSession.fetch_form_state does a GET and enforces 1 req/sec.
                    # We use it here to get the raw HTML; we don't need the form state.
                    html_summary = await _get_page(session, _SUMMARY_PATH, q_param)
                    summary_data = _parse_case_summary(html_summary)
                except Exception as exc:
                    logger.warning("CaseSummary fetch error for id=%d: %s", current_id, exc)
                    consecutive_miss += 1
                    current_id += 1
                    continue

                if summary_data is None:
                    # Not an Estate case (or no case at this ID)
                    logger.debug("ID %d: no Estate case found (miss %d/%d)", current_id, consecutive_miss + 1, self._max_consecutive_miss)
                    consecutive_miss += 1
                    current_id += 1
                    continue

                # Reset miss counter — we found a valid Estate case
                consecutive_miss = 0
                case_number = summary_data["case_number"]
                filing_date = summary_data.get("filing_date")

                logger.info(
                    "ID %d: Estate case %s filed %s status=%s",
                    current_id, case_number, filing_date, summary_data.get("case_status"),
                )

                # ── Step 3: Fetch CaseParties ─────────────────────────────────
                parties: dict[str, Any] = {}
                try:
                    html_parties = await _get_page(session, _PARTIES_PATH, q_param)
                    parties = _parse_parties(html_parties)
                except Exception as exc:
                    logger.warning("CaseParties fetch error for id=%d / %s: %s", current_id, case_number, exc)
                    # Non-fatal: we can still try without parties data

                decedent_name = parties.get("decedent_name") or summary_data.get("case_title", "").replace("THE ESTATE OF ", "").title()
                dod = parties.get("dod")
                decedent_address = parties.get("decedent_address")
                executor_name = parties.get("executor_name")
                attorney_name = parties.get("attorney_name")

                logger.info(
                    "Case %s: decedent=%r, DOD=%s, executor=%r",
                    case_number, decedent_name, dod, executor_name,
                )

                # ── Step 4: Dual-path parcel resolution ──────────────────────
                # Path A (address_anchor): high-confidence home parcel via address.
                # Path B (name_match): additional owned parcels via owner name.
                # Dedupe by parcel_number — address_anchor wins on conflict.

                # parcel_number → (ParcelMatch, join_method)
                resolved: dict[str, tuple[ParcelMatch, str]] = {}

                # Path B first so Path A can overwrite on conflict (address_anchor wins)
                if decedent_name:
                    try:
                        name_matches = await search_by_owner(
                            decedent_name,
                            fuzzy=True,
                            enrich_detail=True,
                            enrich_tax=False,
                            min_confidence=_MIN_CONFIDENCE,
                        )
                        logger.info(
                            "Case %s: search_by_owner returned %d match(es) for %r",
                            case_number, len(name_matches), decedent_name,
                        )
                        for m in name_matches:
                            resolved[m.parcel_number] = (m, "name_match")
                    except Exception as exc:
                        logger.warning(
                            "fiscal_officer.search_by_owner failed for %r (case %s): %s",
                            decedent_name, case_number, exc,
                        )

                # Path A — address_anchor overwrites name_match for the home parcel
                if decedent_address:
                    try:
                        addr_matches = await search_by_address(
                            decedent_address,
                            enrich_detail=True,
                        )
                        logger.info(
                            "Case %s: search_by_address returned %d match(es) for %r",
                            case_number, len(addr_matches), decedent_address,
                        )
                        for m in addr_matches:
                            resolved[m.parcel_number] = (m, "address_anchor")
                    except Exception as exc:
                        logger.warning(
                            "fiscal_officer.search_by_address failed for %r (case %s): %s",
                            decedent_address, case_number, exc,
                        )

                parcel_matches = [(match, method) for match, method in resolved.values()]

                # ── Step 5: Emit RawListings ──────────────────────────────────
                for match, join_method in parcel_matches:
                    listing = _build_listing(
                        match=match,
                        case_number=case_number,
                        filing_date=filing_date,
                        decedent_name=decedent_name,
                        dod=dod,
                        executor_name=executor_name,
                        attorney_name=attorney_name,
                        probate_internal_id=current_id,
                        join_method=join_method,
                    )
                    all_listings.append(listing)

                    # Write probate signal row (non-fatal if parcel not in registry)
                    await _upsert_probate_signal(
                        pool=self._pool,
                        parcel_number=match.parcel_number,
                        case_number=case_number,
                        decedent_name=decedent_name or "",
                        dod=dod,
                        confidence=match.confidence,
                        dry_run=self._dry_run,
                    )

                if not parcel_matches:
                    logger.info(
                        "Case %s: no parcels found for decedent %r via either path "
                        "(likely renter or name/address mismatch)",
                        case_number, decedent_name,
                    )

                highest_ingested = current_id
                current_id += 1

        # ── Step 6: Update cursor ─────────────────────────────────────────────
        if highest_ingested > start_id:
            if self._dry_run:
                logger.info(
                    "[DRY RUN] Would advance cursor from %d to %d (%d IDs explored, %d Estate cases found)",
                    start_id, highest_ingested, ids_explored, len(all_listings),
                )
            else:
                await _write_cursor(self._pool, highest_ingested)
                logger.info(
                    "ProbateScraper complete: cursor %d → %d | %d IDs explored | %d listings",
                    start_id, highest_ingested, ids_explored, len(all_listings),
                )
        else:
            logger.info(
                "ProbateScraper: no new Estate IDs found (cursor stays at %d, %d IDs explored)",
                start_id, ids_explored,
            )

        return all_listings


# ─────────────────────────────────────────────────────────────────────────────
# Internal HTTP helper (GET with q param, reusing ProwareSession rate limiter)
# ─────────────────────────────────────────────────────────────────────────────

async def _get_page(session: ProwareSession, path: str, q_param: str) -> str:
    """
    GET a ProWare page using the session's rate limiter.

    Uses session's internal httpx client for cookie persistence and the
    1 req/sec floor. Raises httpx.HTTPStatusError on 4xx/5xx.
    """
    # ProwareSession.fetch_form_state does a GET + returns FormState, but
    # we only need the raw HTML. We call it and discard the FormState —
    # probate GET pages don't need ViewState for subsequent GETs.
    #
    # Construct full path with query param.
    full_path = f"{path}?q={q_param}"
    # Access the private client — ProwareSession is in the same package and
    # this avoids duplicating the rate-limiter logic.
    await session._rate_limiter()  # noqa: SLF001
    client = session._assert_client()  # noqa: SLF001
    resp = await client.get(full_path)
    resp.raise_for_status()
    return resp.text


# ─────────────────────────────────────────────────────────────────────────────
# RawListing builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_listing(
    *,
    match: ParcelMatch,
    case_number: str,
    filing_date: date | None,
    decedent_name: str | None,
    dod: date | None,
    executor_name: str | None,
    attorney_name: str | None,
    probate_internal_id: int,
    join_method: str = "name_match",
) -> RawListing:
    """
    Build one RawListing from a ParcelMatch for a probate Estate case.

    One listing per matched parcel. The parcel's situs_address becomes
    the property_address; the executor becomes trustee_name.

    join_method is stored in the listing metadata to indicate how the parcel
    was resolved:
      "address_anchor" — matched via decedent_address (high confidence, 0.95+)
      "name_match"     — matched via decedent_name owner search (fuzzy, 0.75+)
    """
    # Property address comes from the fiscal_officer match (most reliable)
    address = match.situs_address or ""
    city = match.property_city
    zipcode = match.property_zip

    return RawListing(
        source_site="probate",
        property_address=address,
        property_city=city,
        property_county="Cuyahoga",
        property_state="OH",
        property_zip=zipcode,
        case_number=case_number,
        trustee_name=executor_name,
        sale_date=None,         # probate has no auction date
        sale_time=None,
        deposit_usd=None,
        sale_location=None,
        status="active",
        signal_type="probate",
        source_listing_id=match.parcel_number,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dry-run CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

async def _dry_run_demo(max_ids: int = 20) -> None:
    """
    Smoke test without a real DB pool.

    Enumerates IDs from the seed cursor value (hardcoded below for standalone
    testing) and reports what it finds. Does not write to DB.
    """
    import json

    # Standalone mock pool so we can test parsing logic without asyncpg
    class _MockPool:
        async def acquire(self) -> Any:
            return _MockConn()

    class _MockConn:
        async def __aenter__(self) -> "_MockConn":
            return self

        async def __aexit__(self, *_: Any) -> None:
            pass

        async def fetchval(self, *_: Any, **__: Any) -> int:
            # Return a cursor close to the probe sample ID
            return 2818100

        async def execute(self, *_: Any, **__: Any) -> None:
            pass

    print(f"\n=== ProbateScraper DRY RUN (max_ids={max_ids}) ===\n")

    scraper = ProbateScraper(
        pool=_MockPool(),
        dry_run=True,
        max_ids=max_ids,
        max_consecutive_miss=10,
    )
    listings = await scraper.fetch_and_parse()

    print(f"\nTotal RawListings produced: {len(listings)}\n")
    for idx, l in enumerate(listings, 1):
        print(
            f"  [{idx}] case={l.case_number} | parcel={l.source_listing_id} | "
            f"addr={l.property_address!r} | city={l.property_city} | "
            f"executor={l.trustee_name!r} | signal={l.signal_type}"
        )


if __name__ == "__main__":
    import logging as _logging
    import sys

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _max = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    asyncio.run(_dry_run_demo(max_ids=_max))
