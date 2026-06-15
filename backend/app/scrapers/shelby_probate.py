"""
Shelby County, TN (Memphis) Probate Court scraper — estate cases, precision-first.

Site:     https://prdata.shelbycountytn.gov/prweb/  (Neumo CourtConnect, Pega)
Platform: Cloudflare-gated frameset. httpx = 403; a real Playwright browser clears
          the challenge, after which same-origin fetch() of the docket-report content
          frame works (one browser, many fetches). Like shelby_mmlba.py.

────────────────────────────────────────────────────────────────────────────────
INVARIANT — PRECISION-FIRST PARCEL JOIN (do not loosen without the audit + gate).
The Shelby CourtConnect PUBLIC view REDACTS every structured party address
(`Address: unavailable`, verified 100% across case types). The decedent's property
parcel is therefore NOT in the court record. We recover it by cross-referencing the
ReGIS parcel spine we already own (tranchi.parcels), two ways, both precision-first:

  Path B (workhorse) — decedent full name  → parcels.owner_name
    Thesis: an OPEN estate means title has not yet transferred, so the decedent is
    still the registry owner_name. Match via fiscal_officer._name_confidence
    (>= 2 STRONG tokens — given + surname; surname-only REJECTED). Candidate fetch
    requires BOTH surname AND given-name substrings, so a common surname alone never
    explodes. Tier: unique strong -> 'probable' (shown); >AMBIGUITY_CAP matches with
    no anchor -> emit NOTHING; weak single-token -> 'unverified' (hidden / skipped).

  Path A (bonus anchor) — docket free-text address  → parcels.situs_address
    The address appears in docket entries only RARELY ("...real property located at
    <addr>..."), but when present it is a high-confidence anchor. Reuses
    shelby_foreclosure._resolve_parcels (house# + zip + street, unique-match-only).

  Composite (both agree) -> 'confirmed', 0.98.

Loosening this re-creates the Cuyahoga bug where one common surname attached a case
to 775 parcels (68 real). See scraper-playbook/reference/JOIN-PRECISION.md. We emit
NOTHING for: non-estate case types, asset-only estates (no real property), and
ambiguous common-name matches with no anchor. Quality over recall — always.

INVARIANT — staleness is CURSOR (see staleness.py): this scraper only walks NEW
PR-case-numbers forward and never re-visits old ones, so a probate listing can never
be retired by "not seen this cycle". It retires when shelby_probate_recheck.py finds
the court case_status CLOSED/DISPOSED. SITE_NAME must be mapped CURSOR in staleness.py.

INVARIANT — tranchi.shelby_probate_cursor always has exactly one row (id=1). Read/write
only that row; never DELETE it. Migration 010 creates + seeds it.
────────────────────────────────────────────────────────────────────────────────

DATA FLOW PER CASE (PR###### docket report):
1. fetch cp_dktrpt_docket_report?case_id=PR######
2. parse: case_type code, case_status, case_title (decedent), filing_date, docket text
3. skip if not an estate type (guardianship/name-change/etc. — living persons)
4. resolve parcel(s): Path B (name->owner) + Path A (docket address->situs)
5. classify + precision-gate; emit one RawListing + one tranchi.signals row per parcel
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any

from bs4 import BeautifulSoup

try:  # asyncpg only needed for the pool type hint; scraper requires a pool to run
    import asyncpg  # noqa: F401
except Exception:  # pragma: no cover
    asyncpg = None  # type: ignore

from app.scrapers.base import ListingScraper
from app.scrapers._probate_owner import surname_mismatch as _surname_mismatch
from app.scrapers.db import canonical_address, normalize_parcel_number
from app.scrapers.fiscal_officer import _name_confidence, _normalize_name, _levenshtein, _STRONG_TOKEN
from app.scrapers.models import RawListing
from app.scrapers.shelby_foreclosure import _resolve_parcels

logger = logging.getLogger(__name__)

SITE_NAME = "Shelby Probate Court"
SIGNAL_TYPE = "probate"
SIGNAL_SOURCE = "shelby_probate_court"

_BASE_URL = "https://prdata.shelbycountytn.gov"
# Entry page used to clear the Cloudflare challenge + set the session cookie. Any
# successful same-origin navigation works; the docket setup page is the natural one.
_ENTRY_URL = f"{_BASE_URL}/prweb/ck_public_qry_doct.cp_dktrpt_setup_idx"
# Content frame of a single case's docket report (no frameset — fetch() returns it raw).
_DOCKET_URL = (
    f"{_BASE_URL}/prweb/ck_public_qry_doct.cp_dktrpt_docket_report"
    "?backto=P&case_id={case_id}&begin_date=&end_date="
)

# Estate case types — a decedent who could own real property. Everything else
# (guardianship, conservatorship, name change, birth record, mental health) is a
# LIVING person and has no decedent estate, so we never emit a listing for it.
# Verbatim from the search form's case_type select. Tunable.
_ESTATE_TYPES: set[str] = {
    "01",  # PROBATE OF WILL
    "02",  # ADMINISTRATION
    "05",  # ADMINISTRATION, CTA
    "08",  # MUNIMENT OF TITLE & SM ESTATE
    "09",  # MUNIMENT OF TITLE
    "10",  # SMALL ESTATE
    "11",  # YEARS SUPPORT (decedent's family support)
    "17",  # ADMINISTRATOR AD LITEM
    "20",  # SALE OF REAL ESTATE
    "23",  # PET APPT ADM CTA, DBN
}

# Precision thresholds (mirror probate.py / fiscal_officer)
_MIN_CONFIDENCE = 0.75
_HIGH_CONFIDENCE = 0.90
_AMBIGUITY_CAP = 5

# Cursor-walk bounds
_DEFAULT_MAX_IDS = int(os.environ.get("SHELBY_PROBATE_MAX_IDS", "1500"))
_DEFAULT_MAX_CONSECUTIVE_MISS = 30  # PR space is dense; this only trips at the frontier

# Pace + Cloudflare clearance refresh
_REQ_DELAY_SEC = 1.0
_RECLEAR_EVERY = 250  # re-navigate the entry page this often to refresh cf clearance
_PAGE_TIMEOUT_MS = 45_000

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_CLOSED_WORDS = ("closed", "disposed", "terminated", "dismissed")

# Docket free-text property-address recital. Captures the street through a state/zip
# or comma terminator. Rare but high-value when present (Path A anchor).
_DOCKET_ADDR_RE = re.compile(
    r"(?:real property|property|premises)\s+(?:located|situated)\s+at\s+"
    r"(\d{2,6}\s+[A-Za-z0-9 .'#-]+?)(?:,?\s*(?:MEMPHIS|TENNESSEE|TN|SHELBY)\b|,|\.|\s+\d{5}\b)",
    re.IGNORECASE,
)
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


# ─────────────────────────────────────────────────────────────────────────────
# Case-id helpers (PR + 6 digits, monotonic, dense)
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_case_id(n: int) -> str:
    return f"PR{n:06d}"


# ─────────────────────────────────────────────────────────────────────────────
# Cursor helpers (single-row tranchi.shelby_probate_cursor, id=1)
# ─────────────────────────────────────────────────────────────────────────────

async def _read_cursor(pool: Any) -> int:
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT last_id FROM tranchi.shelby_probate_cursor WHERE id = 1"
        )
    if val is None:
        raise RuntimeError(
            "tranchi.shelby_probate_cursor has no row — run migration 010 first"
        )
    return int(val)


async def _write_cursor(pool: Any, last_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE tranchi.shelby_probate_cursor SET last_id = $1, updated_at = NOW() WHERE id = 1",
            last_id,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Docket-report parsing
# ─────────────────────────────────────────────────────────────────────────────

def _clean_text(html: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" "))


def _parse_filing_date(text: str) -> date | None:
    """'Monday , January 05th, 2026' -> date. Tolerates the stray space + ordinal."""
    m = re.search(r"Filing Date:\s*[A-Za-z]+\s*,\s*([A-Za-z]+)\s+(\d{1,2})\w*,\s*(\d{4})", text)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date()
    except ValueError:
        return None


def _parse_docket(html: str) -> dict[str, Any] | None:
    """Parse one docket-report content page.

    Returns dict(case_type, case_status, case_title, decedent_name, filing_date,
    docket_text) for a real case, or None when the id has no case (frontier) or the
    page is a Cloudflare/challenge interstitial (caller re-clears + retries).
    """
    text = _clean_text(html)

    # Challenge / not-yet-cleared interstitial — signal caller to re-navigate.
    if "Case Description" not in text and ("Just a moment" in text or "Attention Required" in text):
        return {"_challenge": True}

    if any(p in text for p in ("No Record Found", "no cases found", "Case Not Found", "Invalid")):
        return None
    if "Case Description" not in text:
        return None  # empty / unassigned id (frontier)

    type_m = re.search(r"Type:\s*(\d{2})\s*-", text)
    case_type = type_m.group(1) if type_m else None

    # 'Status: OPEN - OPEN' / 'OPBD - OPEN' / 'CLOSED - CLOSED'
    status_m = re.search(r"Status:\s*([A-Z]{2,6})\s*-\s*([A-Z]{2,6})", text)
    case_status = f"{status_m.group(1)} - {status_m.group(2)}" if status_m else None

    # 'Case ID: PR034682 - IN RE: NANNIE EVELYN GUY -Bench Trial'
    # Capture the whole title up to 'Filing Date:'; _clean_decedent owns the trailing
    # '-Bench Trial' strip (a regex strip here truncates hyphenated surnames).
    title_m = re.search(
        r"Case ID:\s*PR\d+\s*-\s*(?:IN RE:|IN THE MATTER OF:)\s*(.+?)\s+Filing Date:",
        text,
    )
    raw_title = title_m.group(1).strip() if title_m else None
    decedent_name = _clean_decedent(raw_title) if raw_title else None

    filing_date = _parse_filing_date(text)

    # Docket entries block (for Path A address recital). Everything after the label.
    docket_text = text.split("Docket Entries", 1)[1] if "Docket Entries" in text else ""

    return {
        "case_type": case_type,
        "case_status": case_status,
        "case_title": raw_title,
        "decedent_name": decedent_name,
        "filing_date": filing_date,
        "docket_text": docket_text,
    }


def _clean_decedent(raw: str) -> str | None:
    """Normalize a case-title decedent to a clean 'GIVEN MIDDLE SURNAME' string."""
    s = re.sub(r"\b(ESTATE OF|THE ESTATE OF|DECEASED)\b", "", raw, flags=re.I)
    # Strip a ' -Bench Trial'-style suffix. Require whitespace BEFORE the hyphen so a
    # hyphenated surname ('JONES-SMITH MARY') is preserved (no space before its hyphen).
    s = re.sub(r"\s+-\s*[A-Za-z ]+$", "", s)
    s = re.sub(r"\s+", " ", s).strip(" ,-")
    return s or None


def _extract_docket_address(docket_text: str) -> tuple[str, str | None] | None:
    """Return (street, zip5) from a docket-entry property recital, else None."""
    m = _DOCKET_ADDR_RE.search(docket_text or "")
    if not m:
        return None
    street = canonical_address(m.group(1).strip(" ,."))
    if not street:
        return None
    z = _ZIP_RE.search(docket_text[m.start(): m.end() + 30])
    return street, (z.group(1) if z else None)


# ─────────────────────────────────────────────────────────────────────────────
# Path B — decedent name → spine owner_name (precision-first)
# ─────────────────────────────────────────────────────────────────────────────

def _name_tokens(name: str) -> list[str]:
    return [t for t in _normalize_name(name).split() if len(t) >= 2 and t not in _SUFFIXES]


def _sim(a: str, b: str) -> float:
    """Per-token similarity 0..1 (1 - normalized Levenshtein)."""
    if not a or not b:
        return 0.0
    return 1.0 - _levenshtein(a, b) / max(len(a), len(b), 1)


async def _resolve_by_owner_name(
    pool: Any, decedent_name: str
) -> dict[str, dict[str, Any]]:
    """Return {parcel_number: {owner_name, situs, score}} for precision-safe owner matches.

    SURNAME-ANCHORED (critical — see file header). The spine stores owners SURNAME-FIRST
    ('JAMISON ROOSEVELT', 'KEATHLEY RANDY L & LINDA C'), so a real match requires the
    decedent's SURNAME to strong-match the owner's LEADING token AND the decedent's GIVEN
    name to strong-match SOME owner token. Without the surname anchor, the bare
    >=2-strong-token rule false-matches across MULTI-OWNER strings — e.g. decedent
    'ASHLEY S. DANIEL' wrongly hit 'HARPER DANIEL S & ASHLEY B' (Daniel/Ashley are two
    OTHER people). The anchor kills that while still catching co-owner decedents (the
    shared leading surname covers 'KEATHLEY RANDY & LINDA').
    """
    out: dict[str, dict[str, Any]] = {}
    toks = _name_tokens(decedent_name)
    if pool is None or len(toks) < 2:
        return out  # cannot precision-match on a single token — emit nothing

    surname, given = toks[-1], toks[0]
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT parcel_number, owner_name, situs_address
                FROM tranchi.parcels
                WHERE owner_name ILIKE '%' || $1 || '%'
                  AND owner_name ILIKE '%' || $2 || '%'
                  AND owner_name IS NOT NULL AND owner_name <> ''
                  -- MARKET SCOPE (load-bearing): parcels is a SHARED multi-market table with
                  -- no state column; markets are distinguished ONLY by parcel_number format
                  -- (TN = 14-char alnum; OH = DDD-NN-NNN). Without this guard a Shelby decedent
                  -- name fuzzy-matches a CUYAHOGA owner -> a TN listing gets an OH parcel and
                  -- cross-county dedup wrongly merges it. Scope the candidate fetch to TN parcels.
                  AND parcel_number ~ '^[0-9A-Z]{14}$'
                LIMIT 200
                """,
                surname, given,
            )
    except Exception as exc:  # pragma: no cover
        logger.warning("ShelbyProbate: owner-name query failed for %r: %s", decedent_name, exc)
        return out

    for r in rows:
        owner_toks = _normalize_name(r["owner_name"]).split()
        if not owner_toks:
            continue
        # Surname must be the LEADING token (the shared surname in this dataset).
        if _sim(surname, owner_toks[0]) < _STRONG_TOKEN:
            continue
        # Given name must strong-match some token (covers primary + co-owner givens).
        if max((_sim(given, t) for t in owner_toks), default=0.0) < _STRONG_TOKEN:
            continue
        score = _name_confidence(decedent_name, r["owner_name"])
        if score < _MIN_CONFIDENCE:
            continue
        norm = normalize_parcel_number(r["parcel_number"])
        if not norm:
            continue
        prev = out.get(norm)
        if prev is None or score > prev["score"]:
            out[norm] = {"owner_name": r["owner_name"], "situs": r["situs_address"], "score": score}
    return out


async def _fetch_parcel_rows(pool: Any, parcels: list[str]) -> dict[str, dict[str, Any]]:
    """{parcel_number: {owner_name, situs}} for a set of normalized parcels."""
    if pool is None or not parcels:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT parcel_number, owner_name, situs_address FROM tranchi.parcels "
            "WHERE parcel_number = ANY($1::text[])",
            parcels,
        )
    return {r["parcel_number"]: {"owner_name": r["owner_name"], "situs": r["situs_address"]} for r in rows}


def _city_zip(situs: str | None) -> tuple[str | None, str | None]:
    """('306 S Island Dr, Memphis, TN 38103') -> ('Memphis', '38103')."""
    if not situs:
        return None, None
    z = _ZIP_RE.search(situs)
    zip5 = z.group(1) if z else None
    cm = re.search(r",\s*([A-Za-z .]+?),\s*(?:TN|TENNESSEE)\b", situs, re.I)
    city = cm.group(1).strip() if cm else None
    return city, zip5


# ─────────────────────────────────────────────────────────────────────────────
# Signal upsert (HOT stacking — same parcel across listing types)
# ─────────────────────────────────────────────────────────────────────────────

async def _upsert_signal(pool: Any, parcel: str, case_id: str, decedent: str, score: float) -> None:
    import json
    now = datetime.now(tz=timezone.utc)
    payload = json.dumps({"case_number": case_id, "decedent_name": decedent})
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tranchi.signals
                    (parcel_number, signal_type, source, observed_at, confidence, payload,
                     first_seen_at, last_seen_at, market)
                VALUES ($1, 'probate', $2, $3, $4, $5::jsonb, $3, $3, 'shelby')
                ON CONFLICT DO NOTHING
                """,
                parcel, SIGNAL_SOURCE, now, score, payload,
            )
    except Exception as exc:
        logger.debug("ShelbyProbate: signal insert skipped for %s: %s", parcel, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Classify + build
# ─────────────────────────────────────────────────────────────────────────────

def _build_listing(
    *, parcel: str, situs: str | None, case_id: str, case_status: str | None,
    decedent: str | None, case_title: str | None, method: str, tier: str, score: float,
    probate_internal_id: int, filing_date: date | None = None,
) -> RawListing:
    city, zip5 = _city_zip(situs)
    return RawListing(
        source_site=SITE_NAME,
        source_listing_id=parcel,
        case_number=case_id,
        signal_type=SIGNAL_TYPE,
        property_address=canonical_address(situs) or (situs or ""),
        property_city=city,
        property_county="Shelby",
        property_state="TN",
        property_zip=zip5,
        sale_date=None,
        status="active",
        case_status=case_status,
        match_method=method,
        match_confidence=tier,
        match_score=score,
        decedent_name=decedent,
        case_title=case_title,
        decedent_dod=None,  # not exposed in the Shelby public view
        probate_internal_id=probate_internal_id,
        filing_date=filing_date,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scraper
# ─────────────────────────────────────────────────────────────────────────────

class ShelbyProbateScraper(ListingScraper):
    """Shelby County (TN) Probate Court — estate cases via PR-number cursor walk.

    Needs `pool` (cursor + spine cross-ref + signal writes). Staleness: CURSOR.
    """

    site_name = SITE_NAME

    def __init__(
        self,
        pool: "asyncpg.Pool | None" = None,
        *,
        dry_run: bool = False,
        max_ids: int | None = None,
        max_consecutive_miss: int = _DEFAULT_MAX_CONSECUTIVE_MISS,
    ) -> None:
        self.pool = pool
        self.dry_run = dry_run
        self._max_ids = int(max_ids or _DEFAULT_MAX_IDS)
        self._max_miss = max_consecutive_miss

    async def fetch_and_parse(self) -> list[RawListing]:
        if self.pool is None:
            raise RuntimeError("ShelbyProbateScraper requires a pool= (cursor + spine cross-ref).")

        from playwright.async_api import async_playwright

        start_id = await _read_cursor(self.pool)
        logger.info(
            "ShelbyProbate starting: cursor=PR%06d, max_ids=%d, max_miss=%d",
            start_id, self._max_ids, self._max_miss,
        )

        listings: list[RawListing] = []
        highest = start_id
        miss = 0
        explored = 0
        emitted = skipped_nonestate = skipped_ambiguous = skipped_weak = nomatch = 0

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )
            page = await ctx.new_page()

            async def _clear() -> None:
                await page.goto(_ENTRY_URL, wait_until="domcontentloaded", timeout=_PAGE_TIMEOUT_MS)
                await asyncio.sleep(2.0)  # let the cf challenge settle

            await _clear()

            async def _fetch(case_id: str) -> str:
                url = _DOCKET_URL.format(case_id=case_id)
                return await page.evaluate(
                    """async (u) => { const r = await fetch(u, {credentials:'include'}); return await r.text(); }""",
                    url,
                )

            current = start_id + 1
            cf_retries = 0
            try:
                while explored < self._max_ids and miss < self._max_miss:
                    case_id = _fmt_case_id(current)
                    try:
                        html = await _fetch(case_id)
                        parsed = _parse_docket(html)
                    except Exception as exc:
                        logger.warning("ShelbyProbate: fetch/parse error for %s: %s", case_id, exc)
                        parsed = None

                    if parsed and parsed.get("_challenge"):
                        # cf clearance lapsed — re-clear and retry the SAME id, but bail
                        # after a few consecutive failures so a cf soft-ban can't burn the
                        # whole run on one id (BLOCKER-1). explored is NOT incremented here.
                        cf_retries += 1
                        if cf_retries >= 3:
                            logger.error(
                                "ShelbyProbate: cf challenge persisted %d× at %s — aborting run",
                                cf_retries, case_id,
                            )
                            break
                        logger.info("ShelbyProbate: cf clearance lapsed at %s — re-clearing", case_id)
                        await asyncio.sleep(_REQ_DELAY_SEC)
                        await _clear()
                        continue
                    cf_retries = 0

                    explored += 1
                    await asyncio.sleep(_REQ_DELAY_SEC)
                    if explored % _RECLEAR_EVERY == 0:
                        await _clear()

                    if parsed is None:
                        miss += 1
                        current += 1
                        continue

                    miss = 0  # a real case — not the frontier
                    case_type = parsed.get("case_type")
                    if case_type not in _ESTATE_TYPES:
                        skipped_nonestate += 1
                        highest = current
                        current += 1
                        continue

                    decedent = parsed.get("decedent_name")
                    case_status = parsed.get("case_status")
                    case_title = parsed.get("case_title")

                    # ── Path B: decedent name → owner_name ────────────────────
                    name_hits = await _resolve_by_owner_name(self.pool, decedent) if decedent else {}

                    # ── Path A: docket free-text address → situs ──────────────
                    addr_hits: dict[str, dict[str, Any]] = {}
                    addr = _extract_docket_address(parsed.get("docket_text", ""))
                    if addr:
                        resolved = await _resolve_parcels(self.pool, [addr])
                        if resolved:
                            anchor_parcels = list(resolved.values())
                            enr = await _fetch_parcel_rows(self.pool, anchor_parcels)
                            for p in anchor_parcels:
                                addr_hits[p] = {"owner_name": enr.get(p, {}).get("owner_name"),
                                                "situs": enr.get(p, {}).get("situs"), "score": 0.95}

                    if not name_hits and not addr_hits:
                        nomatch += 1
                        highest = current
                        current += 1
                        continue

                    # Precision-first tiering. Address-anchored parcels are ALWAYS shown
                    # (confirmed). A name-only parcel is 'probable' (shown) ONLY when it is
                    # the UNIQUE name match for this decedent AND scores high — multiple
                    # name matches mean either a multi-property owner OR (the real risk)
                    # several different people with the same name, indistinguishable
                    # without an anchor, so they are skipped (hidden) rather than shown.
                    name_ambiguous = len(name_hits) > _AMBIGUITY_CAP
                    name_unique = len(name_hits) == 1
                    for parcel in set(name_hits) | set(addr_hits):
                        nh = name_hits.get(parcel)
                        ah = addr_hits.get(parcel)
                        if ah is not None:
                            method = "composite" if nh else "address_anchor"
                            tier, score = "confirmed", (0.98 if nh else 0.95)
                            # Owner-vs-decedent mis-join guard (cross-market, mirrors
                            # summit_probate): address-anchor proves the decedent lived
                            # here, not that the estate owns it. Surname mismatch -> demote
                            # to 'review' (VISIBLE + badged), never hide (heirs/trusts/LLCs
                            # legitimately differ).
                            if _surname_mismatch(decedent, ah.get("owner_name")):
                                tier = "review"
                                logger.info(
                                    "ShelbyProbate: case %s parcel %s owner %r != decedent %r — REVIEW (possible mis-join)",
                                    case_id, parcel, ah.get("owner_name"), decedent,
                                )
                        else:  # name-only — precision gates
                            if name_ambiguous:
                                skipped_ambiguous += 1
                                continue
                            score = nh["score"]
                            if not (name_unique and score >= _HIGH_CONFIDENCE):
                                skipped_weak += 1  # ambiguous-few / low-score → hidden
                                continue
                            method, tier = "name_match", "probable"
                        situs = (ah or nh).get("situs")
                        listings.append(_build_listing(
                            parcel=parcel, situs=situs, case_id=case_id, case_status=case_status,
                            decedent=decedent, case_title=case_title, method=method, tier=tier,
                            score=score, probate_internal_id=current,
                            filing_date=parsed.get("filing_date"),
                        ))
                        emitted += 1
                        if not self.dry_run:
                            await _upsert_signal(self.pool, parcel, case_id, decedent or "", score)

                    highest = current
                    current += 1
            finally:
                await browser.close()

        if highest > start_id and not self.dry_run:
            await _write_cursor(self.pool, highest)

        logger.info(
            "ShelbyProbate complete: cursor PR%06d->PR%06d | %d ids | emitted=%d "
            "(skipped: %d non-estate, %d ambiguous, %d weak, %d no-match)%s",
            start_id, highest, explored, emitted,
            skipped_nonestate, skipped_ambiguous, skipped_weak, nomatch,
            " [DRY RUN — cursor not advanced]" if self.dry_run else "",
        )
        return listings
