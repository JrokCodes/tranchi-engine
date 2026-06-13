"""
Summit County (OH / Akron) Probate Court — Estate cases via CourtView eServices v1.52.

Site:     https://search.summitohioprobate.com/eservices/
Vendor:   equivant CourtView Justice Solutions — eServices v1.52 (Apache Wicket on IIS 8.5)
Access:   PUBLIC, no auth, NO bot gate. Stateful httpx session (JSESSIONID cookie).

──────────────────────────────────────────────────────────────────────────────
INVARIANT — WICKET SINGLE-USE ?x= TOKENS (do NOT cache or reuse):
Every ?x= continuation token in a response page is consumed on the next request.
Re-using a stale token silently resets to home.page (no error, just a redirect back
to start). Re-parse the returned HTML at each hop to obtain the next fresh token.
Keep ONE JSESSIONID cookie for the entire session walk.

INVARIANT — DISCLAIMER POST IS MANDATORY (step 3):
Skipping the Begin/disclaimer POST bounces every subsequent request back to home.page.
The disclaimer uses Wicket-Ajax headers and the ?x= token extracted from the
wicketSubmitFormById() onclick attribute of the Begin anchor (NOT the form action token).

INVARIANT — SELECT VALUES ARE SPACE-PADDED TO FIXED WIDTH:
caseCd must be 'ES        ' (10 chars), statCd 'O         ' (10 chars). Sending
unpadded values silently no-ops the filter — you get all case types / all statuses.

INVARIANT — BIND DECEDENT, NOT FIDUCIARY:
Each detail page shows two Party blocks (Decedent + Fiduciary), both with addresses.
Always bind to the block whose role text contains 'Decedent'. The Fiduciary address
is the estate administrator's home, not the estate property.

INVARIANT — PRECISION-FIRST JOIN + AMBIGUITY CAP:
Parcel resolution against tranchi.parcels uses house-number + street substring match
(address_anchor). Surname-only joins are REJECTED. If an address resolves to more than
_AMBIGUITY_CAP parcels (multi-unit building), emit nothing. Quality over recall.

INVARIANT — case_number KEEPS ITS SPACES:
Store case_number as '2026 ES 00449' (with the spaces). The orchestrator's
probate_transfer_rule does substring(1,4) for year and regex ^[0-9]{4} ES to detect
Summit probate cases. Stripping the spaces breaks the year-parse.

INVARIANT — DATE RANGE MAX 12 MONTHS:
CourtView rejects 'Begin and End must be within 12 months.' Chunk into ≤12-month
windows; the cursor's last_window_end is the carry-forward anchor.

INVARIANT — fileDateRange returns ancillary filings of OLD cases:
A 2005 ES case that had new docket activity in the window appears in results with its
1990s case number. Filter on case_number year == current year (or ≤3 years back) to
avoid ingesting ancient cases that have merely had docket activity recently. Ancillary
cases are identified by the ' A' or ' B' suffix (e.g. '2005 ES 00884 A') — skip them.

INVARIANT — pageSize=3 (500) is a result-set cap, not a display-page size:
CourtView always renders 50 rows per page regardless of the pageSize select value.
'500' (option value 3) caps the total result set to 500 matching cases. Pagination
via 'Next' links is still required — each page link is a single-use ?x= token.
Max theoretical pages = 500/50 = 10 per search window. Always follow all Next links.
──────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.scrapers.base import ListingScraper
from app.scrapers.db import canonical_address, normalize_parcel_number
from app.scrapers.models import RawListing

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SITE_NAME = "Summit Probate Court"
SIGNAL_TYPE = "probate"

_BASE_URL = "https://search.summitohioprobate.com/eservices/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# fileDateRange max per CourtView constraint. We chunk into 12-month windows.
_MAX_WINDOW_DAYS = 365

# Rolling lookback for fresh installs (no cursor row yet).
# First run fetches the last 12 months; subsequent runs use the cursor window.
_FRESH_LOOKBACK_DAYS = 365

# Oldest case year we will ingest (skip ancillary filings of ancient cases).
# A case filed before this year is treated as ancillary noise.
_MIN_CASE_YEAR = date.today().year - 2

# Precision thresholds for parcel join
_AMBIGUITY_CAP = 5  # if address resolves to more parcels than this, skip

# Request pacing (polite crawl — no ToS restriction, but be a good citizen)
_REQ_DELAY_SEC = 0.5

# Minimum OPEN-case year filter: cases older than _MIN_CASE_YEAR are ancillary
# (they show up because of docket activity in the window, not new filings).
_ANCILLARY_SUFFIX_RE = re.compile(r"\s+[AB]$")  # ' A' and ' B' suffixes = ancillary filings

# Parcel join — address parsing regexes
_ADDR_BLOB_RE = re.compile(
    r"^(\d+\s+.+?)\s*,?\s*([A-Za-z][A-Za-z\s]{1,30}),?\s*"
    r"(OH|Ohio),?\s*(\d{5})(?:-\d{4})?$",
    re.IGNORECASE,
)

# ─────────────────────────────────────────────────────────────────────────────
# Cursor DDL (declare for migration 018 — do NOT write the migration here)
# ─────────────────────────────────────────────────────────────────────────────
#
# Migration 018 must create:
#
# CREATE TABLE IF NOT EXISTS tranchi.summit_probate_cursor (
#     id            INTEGER PRIMARY KEY DEFAULT 1,
#     last_window_end DATE NOT NULL,   -- end of the last successfully fetched window
#     updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
#     CHECK (id = 1)                   -- single-row invariant
# );
# INSERT INTO tranchi.summit_probate_cursor (id, last_window_end)
#     VALUES (1, (CURRENT_DATE - INTERVAL '365 days')::date)
#     ON CONFLICT DO NOTHING;
#
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Cursor helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _read_cursor(pool: Any) -> date:
    """Return last_window_end from tranchi.summit_probate_cursor.

    If the row doesn't exist yet (pre-migration), returns today minus
    _FRESH_LOOKBACK_DAYS as the bootstrap start.
    """
    try:
        async with pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT last_window_end FROM tranchi.summit_probate_cursor WHERE id = 1"
            )
        if val is not None:
            return val
    except Exception as exc:
        logger.warning("SummitProbate: cursor read failed (%s) — using bootstrap date", exc)
    return date.today() - timedelta(days=_FRESH_LOOKBACK_DAYS)


async def _write_cursor(pool: Any, window_end: date) -> None:
    """Advance tranchi.summit_probate_cursor.last_window_end."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE tranchi.summit_probate_cursor
                   SET last_window_end = $1,
                       updated_at      = NOW()
                 WHERE id = 1
                """,
                window_end,
            )
    except Exception as exc:
        logger.warning("SummitProbate: cursor write failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Wicket session helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_x_token(html: str) -> str:
    """Extract the first ?x= token from a page's form action."""
    m = re.search(r'action="[^"]*\?x=([^"&\s]+)"', html)
    return m.group(1) if m else ""


def _extract_form_action(html: str, form_id: str) -> str:
    """Return the ?x= token from a specific form's action attribute."""
    m = re.search(
        rf'<form[^>]+id="{re.escape(form_id)}"[^>]+action="\?x=([^"]+)"',
        html,
    )
    if m:
        return m.group(1)
    # alt: action comes before id
    m2 = re.search(
        rf'<form[^>]+action="\?x=([^"]+)"[^>]+id="{re.escape(form_id)}"',
        html,
    )
    return m2.group(1) if m2 else ""


def _extract_onclick_x(html: str, element_id: str = "id21") -> str:
    """Return the ?x= token embedded in the Begin button's wicketSubmitFormById onclick."""
    # The anchor id21 carries: wicketSubmitFormById('id27', '?x=<TOKEN>', 'linkFrag:beginButton', ...)
    m = re.search(
        rf'id="{re.escape(element_id)}"[^>]*onclick="[^"]*wicketSubmitFormById\(\'id27\',\s*\'\\?x=([^\']+)\'',
        html,
    )
    if m:
        return m.group(1)
    # Fallback: scan globally for the pattern (element id may shift across sessions)
    m2 = re.search(
        r"wicketSubmitFormById\('id27',\s*'\?x=([^']+)'",
        html,
    )
    return m2.group(1) if m2 else ""


class _CourtViewSession:
    """Manages a stateful httpx session through the CourtView Wicket walk.

    Lifecycle:
      1. establish() — steps 1-3 (BrowserInfo POST + disclaimer POST)
      2. goto_case_type_tab() — step 4 (GET search.page, click Case Type tab)
      3. The returned form action token is used for each search POST.

    The session maintains ONE JSESSIONID across all requests.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "_CourtViewSession":
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers={"User-Agent": _UA},
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        # NOT an assert — python -O (some systemd units use it) strips asserts, which
        # would turn this into a confusing AttributeError deep in a .get()/.post() call.
        if self._client is None:
            raise RuntimeError("CourtViewSession not entered as a context manager")
        return self._client

    async def establish(self) -> None:
        """Steps 1-3: init JSESSIONID, browser fingerprint POST, disclaimer POST."""
        # Step 1: GET BrowserInfoPage — sets JSESSIONID, mints form action ?x=
        r1 = await self.client.get(_BASE_URL + "home.page.2")
        r1.raise_for_status()
        action = _extract_x_token(r1.text)
        if not action:
            raise RuntimeError("SummitProbate: failed to extract BrowserInfoPage action token")

        # Step 2: POST browser fingerprint — emulates JS-collected navigator fields
        post_url = _BASE_URL + "home.page.2" + f";jsessionid={self._jsessionid()}?x={action}"
        # The URL with ;jsessionid is already handled by cookie jar; use simpler form:
        post_url = _BASE_URL + "home.page.2" + f"?x={action}"
        r2 = await self.client.post(
            post_url,
            data={
                "id1_hf_0": "",
                "navigatorAppName": "Netscape",
                "navigatorAppVersion": "5.0 (Windows NT 10.0; Win64; x64)",
                "navigatorAppCodeName": "Mozilla",
                "navigatorCookieEnabled": "true",
                "navigatorJavaEnabled": "false",
                "navigatorLanguage": "en-US",
                "navigatorPlatform": "Win32",
                "navigatorUserAgent": _UA,
                "screenWidth": "1280",
                "screenHeight": "900",
                "screenColorDepth": "24",
                "utcOffset": "300",
                "utcDSTOffset": "240",
                "browserWidth": "1280",
                "browserHeight": "900",
                "hostname": "search.summitohioprobate.com",
            },
        )
        r2.raise_for_status()

        # Step 3: POST disclaimer (Begin button).
        # The Begin button's onclick carries a DIFFERENT ?x= token than the form action.
        # We must extract it from wicketSubmitFormById() — NOT from the form action attr.
        onclick_x = _extract_onclick_x(r2.text)
        if not onclick_x:
            raise RuntimeError("SummitProbate: failed to extract disclaimer onclick ?x= token")

        current_base = str(r2.url).split("?")[0]
        disc_url = current_base + "?x=" + onclick_x
        r3 = await self.client.post(
            disc_url,
            data={"id27_hf_0": "", "linkFrag:beginButton": "linkFrag:beginButton"},
            headers={
                "Wicket-Ajax": "true",
                "Wicket-Ajax-BaseURL": ".",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        r3.raise_for_status()

        # The Wicket-Ajax response is XML with a <redirect> node
        redirect_m = re.search(r"<redirect><!\[CDATA\[([^\]]+)\]\]>", r3.text)
        if not redirect_m:
            raise RuntimeError(
                f"SummitProbate: disclaimer POST did not return an AJAX redirect. "
                f"Response: {r3.text[:200]}"
            )
        redirect_target = redirect_m.group(1)  # e.g. 'search.page?x=...'

        # Step 4a: follow the redirect to reach the search page
        r4 = await self.client.get(_BASE_URL + redirect_target)
        r4.raise_for_status()
        self._search_page_html = r4.text

    def _jsessionid(self) -> str:
        """Return current JSESSIONID from the cookie jar."""
        return self.client.cookies.get("JSESSIONID", "")

    async def goto_case_type_tab(self) -> str:
        """Navigate to the Case Type search tab. Returns the form action ?x= token."""
        # Find 'Case Type' tab link in the post-disclaimer search page
        soup = BeautifulSoup(self._search_page_html, "html.parser")
        tab_link = soup.find("a", string=re.compile(r"Case Type", re.I))
        if not tab_link:
            raise RuntimeError("SummitProbate: 'Case Type' tab link not found on search page")
        tab_href = tab_link.get("href", "")
        tab_x = re.search(r"\?x=(.+)", tab_href)
        if not tab_x:
            raise RuntimeError(f"SummitProbate: no ?x= in Case Type tab href: {tab_href!r}")

        await asyncio.sleep(_REQ_DELAY_SEC)
        r5 = await self.client.get(_BASE_URL + "search.page.3?x=" + tab_x.group(1))
        r5.raise_for_status()

        form_x = _extract_form_action(r5.text, "id8f")
        if not form_x:
            raise RuntimeError("SummitProbate: form id8f action token not found on Case Type tab")
        return form_x

    async def post_case_type_search(
        self,
        form_action_x: str,
        begin: str,
        end: str,
    ) -> str:
        """POST the Case Type search form. Returns the results page HTML."""
        await asyncio.sleep(_REQ_DELAY_SEC)
        r = await self.client.post(
            _BASE_URL + "search.page.3?x=" + form_action_x,
            data={
                "id8f_hf_0": "",
                "topSearchPanel:pageSize": "3",   # 3 = 500 rows per page
                "fileDateRange:dateInputBegin": begin,
                "fileDateRange:dateInputEnd": end,
                "caseCd": "ES        ",    # SPACE-PADDED to 10 chars — critical
                "statCd": "O         ",    # SPACE-PADDED to 10 chars — critical
                "ptyCd": " ",
                "submitLink": "Search",
            },
        )
        r.raise_for_status()
        return r.text

    async def get_next_page(self, next_x: str) -> str:
        """GET the next results page using a single-use ?x= pagination token."""
        await asyncio.sleep(_REQ_DELAY_SEC)
        r = await self.client.get(_BASE_URL + "searchresults.page?x=" + next_x)
        r.raise_for_status()
        return r.text

    async def get_detail_page(self, x_token: str) -> str:
        """GET a case detail page using its single-use ?x= token."""
        await asyncio.sleep(_REQ_DELAY_SEC)
        r = await self.client.get(_BASE_URL + "searchresults.page?x=" + x_token)
        r.raise_for_status()
        return r.text


# ─────────────────────────────────────────────────────────────────────────────
# HTML parsers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_results_grid(html: str) -> tuple[list[dict[str, Any]], str | None]:
    """Parse the search results grid tbody.

    Returns:
        (rows, next_page_x) where next_page_x is the ?x= token for the 'Next'
        pagination link, or None if this is the last page.

    Row dicts contain:
        case_num, party_name, file_date (str MM/DD/YYYY), action_type,
        party_type, case_status, detail_x (the raw ?x=<token> href value)

    CourtView result grid uses cell IDs 'grid~row-N~cell-M' (1-indexed).
    Column mapping (confirmed live 2026-06-13):
        cell-3 = Case Number  (has the detail page link)
        cell-4 = Party/Company Name
        cell-5 = Case Type
        cell-6 = File Date
        cell-7 = Initiating Action
        cell-8 = Party Type
        cell-9 = DOB (usually empty for deceased)
        cell-10 = Case Status
        cell-11 = Affiliation

    NOTE: pageSize=3 (500) is a RESULT-SET cap, not a display-page size.
    CourtView always renders 50 rows per page. Max 10 pages per 500-row window.
    """
    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.find("tbody")

    results: list[dict[str, Any]] = []

    if tbody:
        for tr in tbody.find_all("tr"):
            cells: dict[int, tuple[str, str]] = {}
            for td in tr.find_all("td"):
                cid = td.get("id", "")
                m = re.match(r"grid~row-\d+~cell-(\d+)", cid)
                if not m:
                    continue
                cell_n = int(m.group(1))
                # Content is in a <span> wrapping an inner <span>
                outer = td.find("span", recursive=False)
                inner = outer.find("span") if outer else None
                val = (inner.get_text(strip=True) if inner else td.get_text(strip=True))
                a = td.find("a")
                link_href = a.get("href", "") if a else ""
                cells[cell_n] = (val, link_href)

            case_num = cells.get(3, ("", ""))[0]
            if not case_num:
                continue

            detail_href = cells.get(3, ("", ""))[1]
            detail_x_m = re.search(r"\?x=(.+)", detail_href)
            detail_x = detail_x_m.group(1) if detail_x_m else ""

            results.append({
                "case_num": case_num,
                "party_name": cells.get(4, ("", ""))[0],
                "case_type": cells.get(5, ("", ""))[0],
                "file_date_str": cells.get(6, ("", ""))[0],
                "action_type": cells.get(7, ("", ""))[0],
                "party_type": cells.get(8, ("", ""))[0],
                "case_status": cells.get(10, ("", ""))[0],
                "detail_x": detail_x,
            })

    # Find the 'Next' pagination link (single-use ?x= token)
    next_page_x: str | None = None
    next_link = soup.find("a", string=re.compile(r"^Next$|^>>$|^›$", re.I))
    if next_link:
        href = next_link.get("href", "")
        m_next = re.search(r"\?x=(.+)", href)
        if m_next:
            next_page_x = m_next.group(1)

    return results, next_page_x


def _parse_filing_date(date_str: str) -> date | None:
    """Parse 'MM/DD/YYYY' file date from the results grid."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def _parse_detail_parties(html: str) -> list[dict[str, Any]]:
    """Parse the case detail page and return all party blocks.

    Returns a list of dicts:
        name (str), role (str), address_blob (str | None)

    The address_blob is the raw concatenated text from the ptyContact div
    (e.g. '3250 Conlin DriveAkron,OH44319'). The caller splits it.

    DOM structure (confirmed 2026-06-13 on live site):
        <div class="subSectionHeader2">
            <h5>
                <div class="ptyInfoLabel">Newman, Reva Gay</div>
                <div class="ptyType"> - Decedent</div>
            </h5>
        </div>
        <div class="box ptyPersInfo">...</div>   ← DOD
        <div class="box ptyContact">
            <ul>
                <li class="ptyContactLabel">Address</li>
                <li class="ptyContactInfo">3250 Conlin DriveAkron,OH44319</li>
            </ul>
        </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    parties = []

    for header in soup.find_all("div", class_="subSectionHeader2"):
        name_div = header.find("div", class_="ptyInfoLabel")
        role_div = header.find("div", class_="ptyType")
        name = name_div.get_text(strip=True) if name_div else ""
        role = role_div.get_text(strip=True).lstrip(" -").strip() if role_div else ""

        # Address is in the next .ptyContact sibling after this header
        address_blob: str | None = None
        sibling = header.find_next_sibling()
        while sibling:
            classes = sibling.get("class", [])
            if "subSectionHeader2" in classes:
                break  # next party — stop
            if "ptyContact" in classes:
                addr_label = sibling.find(class_="ptyContactLabel")
                addr_info = sibling.find(class_="ptyContactInfo")
                if addr_label and "Address" in addr_label.get_text():
                    blob = addr_info.get_text(strip=True) if addr_info else ""
                    address_blob = blob if blob else None
                break
            sibling = sibling.find_next_sibling()

        parties.append({"name": name, "role": role, "address_blob": address_blob})

    return parties


def _split_address_blob(blob: str | None) -> tuple[str | None, str | None, str | None]:
    """Split '3250 Conlin DriveAkron,OH44319' into (street, city, zip5).

    CourtView concatenates the address label+info without line breaks, so the
    street bleeds directly into the city name. The pattern is:
        <house-number> <street-name> <street-type><City>,OH<ZIP>

    Strategy: find the zip (5 digits at end), then find ',OH' just before it,
    then find where the street ends and the city begins (we look for the last
    uppercase sequence before ',OH' that starts with a capital letter).
    """
    if not blob:
        return None, None, None

    # Normalize whitespace
    blob = re.sub(r"\s+", " ", blob).strip()

    # Extract ZIP
    zip_m = re.search(r"(\d{5})(?:-\d{4})?$", blob)
    zip5 = zip_m.group(1) if zip_m else None

    # Strip zip from blob
    base = blob[: zip_m.start()].rstrip(" ,") if zip_m else blob

    # Strip state
    state_m = re.search(r",?\s*(?:OH|Ohio)\s*$", base, re.I)
    if state_m:
        base = base[: state_m.start()].rstrip(" ,")

    # Now base = '<street><City>' with no separator between them.
    # Find the city: it begins after a street-type abbreviation followed immediately
    # by an uppercase city word.  E.g. '3250 Conlin DriveAkron' — 'Drive' ends, 'Akron' begins.
    # We look for known street-type endings.
    _STYPE = (
        r"(?:Drive|Dr|Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Court|Ct|Place|Pl|"
        r"Boulevard|Blvd|Circle|Cir|Way|Trail|Trl|Terrace|Ter|Parkway|Pkwy|"
        r"Highway|Hwy|Loop|Square|Sq|Turn)"
    )
    stype_m = re.search(rf"({_STYPE})([A-Z])", base)
    if stype_m:
        street_end = stype_m.start(2)
        street = base[:street_end].strip()
        city = base[street_end:].strip()
    else:
        # Fallback: split at the first capital letter after a word ending with a digit
        # i.e. house-number and street are together until we hit a city word
        cap_m = re.search(r"(\d)([A-Z][a-z])", base)
        if cap_m:
            street = base[: cap_m.start(2)].strip()
            city = base[cap_m.start(2):].strip()
        else:
            # Cannot parse — return the whole blob as street
            return blob, None, zip5

    # Clean up any comma suffix on city
    city = city.rstrip(", ").strip() or None
    street = street.strip() or None

    return street, city, zip5


def _is_valid_case(case_num: str, min_year: int) -> bool:
    """Return True if this is a primary (non-ancillary) Estate case from min_year or later.

    Summit case format: 'YYYY ES NNNNN'
    Ancillary cases have an ' A' suffix: '2005 ES 00884 A'
    We skip both ancient cases and ancillary suffixes.
    """
    if _ANCILLARY_SUFFIX_RE.search(case_num):
        return False  # ancillary/associated filing
    m = re.match(r"^(\d{4})\s+ES\s+\d{5}$", case_num.strip())
    if not m:
        return False
    try:
        year = int(m.group(1))
    except ValueError:
        return False
    return year >= min_year


def _decedent_from_parties(parties: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the Decedent party dict, or None if not found."""
    for p in parties:
        if "decedent" in p.get("role", "").lower():
            return p
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Parcel join (precision-first, address-anchor only)
# ─────────────────────────────────────────────────────────────────────────────

async def _resolve_address_to_parcel(
    pool: Any,
    street: str,
    city: str | None,
) -> list[dict[str, Any]]:
    """Match a decedent street address against tranchi.parcels (market='summit').

    Uses house-number + street-name substring match, scoped to Summit market
    (parcel_number format: 7-digit numeric, no letters, no whitespace).
    Returns a list of {parcel_number, situs_address, owner_name} dicts.
    """
    if pool is None or not street:
        return []

    # Extract house number and street name for the query
    parts = street.split()
    if not parts:
        return []

    house_num = parts[0] if parts[0].isdigit() else None
    street_body = " ".join(parts[1:]) if house_num else street

    # SUFFIX-AGNOSTIC MATCH: the decedent blob carries the FULL street type
    # ("Conlin Drive") but the GIS spine stores the ABBREVIATED form ("Conlin Dr"),
    # so an exact ILIKE on the full suffix matches 0 rows (the real cause of a
    # probate=0 first run). Drop a trailing street-type word and match on the
    # street-name STEM; house# + stem + the _AMBIGUITY_CAP keep precision.
    _STREET_TYPES = {
        "drive", "dr", "street", "st", "avenue", "ave", "road", "rd", "lane", "ln",
        "court", "ct", "boulevard", "blvd", "way", "place", "pl", "circle", "cir",
        "trail", "trl", "terrace", "ter", "parkway", "pkwy", "highway", "hwy",
        "square", "sq", "loop", "run", "path", "point", "pt", "crossing", "xing",
    }
    _sb_parts = street_body.split()
    if len(_sb_parts) >= 2 and _sb_parts[-1].lower().strip(".") in _STREET_TYPES:
        street_body = " ".join(_sb_parts[:-1])

    # Require the house number + at least 3 chars of street stem to avoid
    # false matches across Summit's 261K parcel spine
    if not house_num or len(street_body) < 3:
        return []

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT parcel_number, situs_address, owner_name
                  FROM tranchi.parcels
                 WHERE situs_address ILIKE '%' || $1 || '%'
                   AND situs_address ILIKE '%' || $2 || '%'
                   -- MARKET SCOPE: Summit parcels are 7-digit numeric strings.
                   -- Cuyahoga is DDD-NN-NNN (hyphens); Shelby is 14-char alnum.
                   -- Without this guard a Summit address matches a Cuyahoga situs.
                   AND parcel_number ~ '^[0-9]{7}$'
                 LIMIT 20
                """,
                house_num,
                street_body[:20],  # cap to avoid ILIKE scan blowout
            )
    except Exception as exc:
        logger.warning("SummitProbate: parcel query failed for %r: %s", street, exc)
        return []

    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Signal upsert
# ─────────────────────────────────────────────────────────────────────────────

async def _upsert_signal(
    pool: Any,
    parcel: str,
    case_number: str,
    decedent_name: str,
    score: float,
    dry_run: bool,
) -> None:
    if dry_run:
        logger.debug("[DRY RUN] signal: parcel=%s case=%s", parcel, case_number)
        return
    import json
    now = datetime.now(tz=timezone.utc)
    payload = json.dumps({"case_number": case_number, "decedent_name": decedent_name})
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tranchi.signals
                    (parcel_number, signal_type, source, observed_at, confidence,
                     payload, first_seen_at, last_seen_at, market)
                VALUES ($1, 'probate', 'summit_probate_court', $2, $3, $4::jsonb, $2, $2, 'summit')
                ON CONFLICT DO NOTHING
                """,
                parcel,
                now,
                score,
                payload,
            )
    except Exception as exc:
        logger.debug("SummitProbate: signal insert skipped for %s: %s", parcel, exc)


# ─────────────────────────────────────────────────────────────────────────────
# RawListing builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_listing(
    *,
    parcel_number: str,
    situs_address: str | None,
    city: str | None,
    zip5: str | None,
    case_number: str,
    case_status: str | None,
    filing_date: date | None,
    decedent_name: str | None,
    fiduciary_name: str | None,
    action_type: str | None,
    match_method: str,
    match_confidence: str,
    match_score: float,
) -> RawListing:
    return RawListing(
        source_site=SITE_NAME,
        source_listing_id=normalize_parcel_number(parcel_number) or parcel_number,
        case_number=case_number,
        signal_type=SIGNAL_TYPE,
        property_address=canonical_address(situs_address) or (situs_address or ""),
        property_city=city,
        property_county="Summit",
        property_state="OH",
        property_zip=zip5,
        sale_date=None,
        status="active",
        case_status=case_status,
        match_method=match_method,
        match_confidence=match_confidence,
        match_score=match_score,
        decedent_name=decedent_name,
        trustee_name=fiduciary_name,
        case_title=f"Estate of {decedent_name}" if decedent_name else None,
        filing_date=filing_date,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main scraper
# ─────────────────────────────────────────────────────────────────────────────

class SummitProbateScraper(ListingScraper):
    """Summit County (OH) Probate Court — open Estate cases via CourtView eServices.

    Session walk (Apache Wicket, stateful, single-use ?x= tokens):
      1. GET BrowserInfoPage → JSESSIONID
      2. POST browser fingerprint → disclaimer page
      3. POST disclaimer (Begin) via Wicket-Ajax → redirect to search.page
      4. GET Case Type search tab → form action token
      5. POST search (caseCd=ES, statCd=O, 12-month window) → results grid
      6. For each Decedent row → GET detail page → parse address
      7. Address → tranchi.parcels join (address_anchor, precision-first)
      8. Emit RawListing per parcel; advance cursor

    Cursor: tranchi.summit_probate_cursor (single row, id=1, last_window_end DATE).
    The 12-month window constraint means we chunk the 18-month lookback into two
    passes on first run; subsequent runs only cover since the last window_end.

    Args:
        pool:     asyncpg connection pool (required for cursor + parcel join).
                  Pass None only when dry_run=True (demo mode).
        dry_run:  Skip DB writes (cursor, signals). RawListings still returned.
        min_case_year: Skip cases filed before this year (ancillary noise filter).
                  Defaults to current_year - 2 so we still catch 2-year-old open estates.
    """

    site_name = SITE_NAME

    def __init__(
        self,
        pool: Any = None,
        *,
        dry_run: bool = False,
        min_case_year: int | None = None,
    ) -> None:
        self.pool = pool
        self.dry_run = dry_run
        # Compute the floor at CONSTRUCTION, not module-import — a long-running uvicorn
        # process started in Dec would otherwise keep a stale year across the Jan boundary.
        self._min_case_year = (
            min_case_year if min_case_year is not None else date.today().year - 2
        )

    async def fetch_and_parse(self) -> list[RawListing]:
        """Walk CourtView for open Summit Estate cases. Returns RawListing list."""
        # Determine date windows to fetch.
        # CourtView max range = 12 months. If the cursor is >12 months back we chunk.
        today = date.today()
        if self.pool is not None:
            window_start = await _read_cursor(self.pool)
        else:
            window_start = today - timedelta(days=_FRESH_LOOKBACK_DAYS)

        # Build list of (begin_date, end_date) chunks, each ≤ 12 months
        windows: list[tuple[date, date]] = []
        chunk_start = window_start
        while chunk_start < today:
            chunk_end = min(chunk_start + timedelta(days=_MAX_WINDOW_DAYS), today)
            windows.append((chunk_start, chunk_end))
            chunk_start = chunk_end + timedelta(days=1)

        logger.info(
            "SummitProbate: cursor=%s | %d windows to fetch | dry_run=%s",
            window_start, len(windows), self.dry_run,
        )

        all_listings: list[RawListing] = []
        last_successful_end: date | None = None

        async with _CourtViewSession() as session:
            try:
                await session.establish()
                logger.info("SummitProbate: session established (disclaimer accepted)")
            except Exception as exc:
                logger.error("SummitProbate: failed to establish session: %s", exc)
                raise

            for win_begin, win_end in windows:
                begin_str = win_begin.strftime("%m/%d/%Y")
                end_str = win_end.strftime("%m/%d/%Y")
                logger.info("SummitProbate: fetching window %s – %s", begin_str, end_str)

                try:
                    form_x = await session.goto_case_type_tab()
                    results_html = await session.post_case_type_search(
                        form_x, begin_str, end_str
                    )
                except Exception as exc:
                    logger.error(
                        "SummitProbate: search POST failed for window %s–%s: %s",
                        begin_str, end_str, exc,
                    )
                    break

                # Check for feedback errors
                soup_check = BeautifulSoup(results_html, "html.parser")
                errors = soup_check.find_all(class_=re.compile(r"feedbackPanelERROR", re.I))
                if errors:
                    err_msgs = [e.get_text(strip=True) for e in errors]
                    logger.error(
                        "SummitProbate: search returned error for window %s–%s: %s",
                        begin_str, end_str, err_msgs,
                    )
                    break

                # Paginate through all result pages (max 10 pages / 500 rows per window)
                # pageSize=3 (500) is a result-set cap; the grid always renders 50 per page.
                all_grid_rows: list[dict[str, Any]] = []
                current_html = results_html
                page_num = 1
                _MAX_PAGES = 10  # safety ceiling (500 rows / 50 per page = 10 pages max)
                while True:
                    page_rows, next_page_x = _parse_results_grid(current_html)
                    all_grid_rows.extend(page_rows)
                    logger.info(
                        "SummitProbate: page %d: %d rows (cumulative %d) next=%s",
                        page_num, len(page_rows), len(all_grid_rows),
                        "YES" if next_page_x else "NO",
                    )
                    if not next_page_x or page_num >= _MAX_PAGES:
                        break
                    try:
                        current_html = await session.get_next_page(next_page_x)
                        page_num += 1
                    except Exception as exc:
                        logger.warning(
                            "SummitProbate: pagination error at page %d: %s", page_num, exc
                        )
                        break

                logger.info(
                    "SummitProbate: %d total raw rows for window %s–%s",
                    len(all_grid_rows), begin_str, end_str,
                )

                # Deduplicate by case_number — one detail fetch per case
                # (each case produces N party rows in the grid; we only need the detail once)
                seen_cases: dict[str, list[dict[str, Any]]] = {}
                for row in all_grid_rows:
                    cn = row["case_num"]
                    if not _is_valid_case(cn, self._min_case_year):
                        continue
                    if cn not in seen_cases:
                        seen_cases[cn] = []
                    seen_cases[cn].append(row)

                logger.info(
                    "SummitProbate: %d unique valid ES cases in window (dropped %d ancillary/old)",
                    len(seen_cases),
                    len(all_grid_rows) - sum(len(v) for v in seen_cases.values()),
                )

                for case_num, case_rows in seen_cases.items():
                    # Use the first row's detail link (all rows for the same case share it)
                    detail_x = next(
                        (r["detail_x"] for r in case_rows if r["detail_x"]), None
                    )
                    if not detail_x:
                        logger.warning(
                            "SummitProbate: case %s has no detail link — skipping", case_num
                        )
                        continue

                    # Meta from the grid (use the Decedent row if available)
                    decedent_row = next(
                        (r for r in case_rows if "decedent" in r.get("party_type", "").lower()),
                        case_rows[0],
                    )
                    case_status = decedent_row.get("case_status", "Open")
                    filing_date = _parse_filing_date(decedent_row.get("file_date_str", ""))
                    action_type = decedent_row.get("action_type", "")

                    # GET detail page
                    try:
                        detail_html = await session.get_detail_page(detail_x)
                    except Exception as exc:
                        logger.warning(
                            "SummitProbate: detail fetch failed for %s: %s", case_num, exc
                        )
                        continue

                    parties = _parse_detail_parties(detail_html)
                    if not parties:
                        logger.debug("SummitProbate: no parties parsed for %s", case_num)
                        continue

                    # Bind DECEDENT party (not fiduciary)
                    decedent = _decedent_from_parties(parties)
                    if not decedent:
                        logger.debug(
                            "SummitProbate: no Decedent party found in detail for %s", case_num
                        )
                        continue

                    decedent_name = decedent.get("name", "") or ""
                    address_blob = decedent.get("address_blob")

                    # Find fiduciary name (for trustee_name field)
                    fiduciary = next(
                        (p for p in parties if p.get("role", "").lower() in ("fiduciary", "applicant")),
                        None,
                    )
                    fiduciary_name = fiduciary.get("name") if fiduciary else None

                    if not address_blob:
                        logger.info(
                            "SummitProbate: case %s decedent %r has no address — skip (renter/no property)",
                            case_num, decedent_name,
                        )
                        continue

                    # Parse address blob into components
                    street, city, zip5 = _split_address_blob(address_blob)
                    if not street:
                        logger.warning(
                            "SummitProbate: could not parse address blob %r for %s",
                            address_blob, case_num,
                        )
                        continue

                    logger.info(
                        "SummitProbate: case %s | decedent=%r | address=%r %r %r",
                        case_num, decedent_name, street, city, zip5,
                    )

                    # Parcel join (address_anchor, precision-first)
                    parcels = await _resolve_address_to_parcel(
                        self.pool, street, city
                    )

                    if not parcels:
                        logger.info(
                            "SummitProbate: case %s no parcel match for %r (renter or outside Summit spine)",
                            case_num, street,
                        )
                        continue

                    # Ambiguity cap: if address resolves to too many parcels it's a
                    # multi-unit building and we can't determine which unit the decedent owned
                    if len(parcels) > _AMBIGUITY_CAP:
                        logger.info(
                            "SummitProbate: case %s address %r ambiguous (%d parcels > cap=%d) — skipping",
                            case_num, street, len(parcels), _AMBIGUITY_CAP,
                        )
                        continue

                    # Emit one listing per matched parcel (usually 1)
                    tier = "confirmed" if len(parcels) == 1 else "probable"
                    score = 0.95 if len(parcels) == 1 else 0.80

                    for p in parcels:
                        norm_parcel = normalize_parcel_number(p["parcel_number"])
                        if not norm_parcel:
                            continue

                        # Use situs from spine if available, else fall back to parsed address
                        situs = p.get("situs_address") or f"{street}, {city or 'Akron'}, OH"

                        listing = _build_listing(
                            parcel_number=norm_parcel,
                            situs_address=situs,
                            city=city,
                            zip5=zip5,
                            case_number=case_num,
                            case_status=case_status,
                            filing_date=filing_date,
                            decedent_name=decedent_name,
                            fiduciary_name=fiduciary_name,
                            action_type=action_type,
                            match_method="address_anchor",
                            match_confidence=tier,
                            match_score=score,
                        )
                        all_listings.append(listing)

                        await _upsert_signal(
                            self.pool, norm_parcel, case_num,
                            decedent_name, score, self.dry_run,
                        )

                last_successful_end = win_end

        # Advance cursor
        if last_successful_end is not None and not self.dry_run and self.pool is not None:
            await _write_cursor(self.pool, last_successful_end)
            logger.info(
                "SummitProbate: cursor advanced to %s | %d listings emitted",
                last_successful_end, len(all_listings),
            )
        else:
            logger.info(
                "SummitProbate complete%s: %d listings emitted",
                " [DRY RUN — cursor not advanced]" if self.dry_run else "",
                len(all_listings),
            )

        return all_listings


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run proof
# ─────────────────────────────────────────────────────────────────────────────

async def _dry_run_demo() -> None:
    """Smoke-test the full session walk without a real DB pool.

    Proves:
    1. Session establishment (disclaimer accepted)
    2. Case Type search (ES + Open + recent window)
    3. Detail page fetch + decedent address extraction
    4. Anchor case 2026 ES 00449 → 3250 Conlin Drive, Akron OH 44319

    DB writes are skipped (dry_run=True). Parcel join is skipped (pool=None).
    """
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    print("\n=== SummitProbateScraper DRY RUN ===\n")

    # We pass pool=None and dry_run=True.
    # With pool=None the cursor read falls back to the bootstrap date
    # and the parcel join is skipped (returns empty — printed as unresolved).
    scraper = SummitProbateScraper(pool=None, dry_run=True)
    listings = await scraper.fetch_and_parse()

    print(f"\n--- {len(listings)} RawListings produced (parcel join skipped — pool=None) ---\n")
    for idx, lst in enumerate(listings, 1):
        print(
            f"  [{idx:3d}] {lst.case_number:25s} | decedent={lst.decedent_name!r:35s} | "
            f"addr={lst.property_address!r} {lst.property_city} {lst.property_zip} | "
            f"parcel={lst.source_listing_id} | conf={lst.match_confidence}"
        )

    # Also run a targeted check on the anchor case 2026 ES 00449
    print("\n--- Anchor case check: 2026 ES 00449 ---")
    anchor = [l for l in listings if l.case_number == "2026 ES 00449"]
    if anchor:
        for l in anchor:
            print(f"  FOUND: decedent={l.decedent_name!r} | addr={l.property_address!r} "
                  f"| city={l.property_city} | zip={l.property_zip}")
            if "Conlin" in (l.property_address or ""):
                print("  ANCHOR CONFIRMED: Conlin Drive present in address")
    else:
        # The anchor case (05/01/2026) is within the 12-month lookback window.
        # If it's not in the listings it's because parcel join returned empty (pool=None).
        # Run the session walk directly to confirm address extraction:
        print("  (Not in listings due to pool=None parcel join — running direct probe...)")
        await _probe_anchor_case()


async def _probe_anchor_case() -> None:
    """Direct probe: establish session, search for 2026 ES 00449, print detail."""
    async with _CourtViewSession() as session:
        await session.establish()

        # Use Case Number search tab to find anchor case directly
        # (re-use the search page HTML from session)
        soup = BeautifulSoup(session._search_page_html, "html.parser")
        cn_tab = soup.find("a", string=re.compile("Case Number", re.I))
        if not cn_tab:
            print("  Case Number tab not found")
            return
        tab_x = re.search(r"\?x=(.+)", cn_tab.get("href", ""))
        if not tab_x:
            return

        await asyncio.sleep(_REQ_DELAY_SEC)
        r5 = await session.client.get(_BASE_URL + "search.page.3?x=" + tab_x.group(1))
        soup5 = BeautifulSoup(r5.text, "html.parser")
        form5 = soup5.find("form", id="id8c")
        form5_action = _extract_form_action(r5.text, "id8c")

        await asyncio.sleep(_REQ_DELAY_SEC)
        r6 = await session.client.post(
            _BASE_URL + "search.page.3?x=" + form5_action,
            data={"id8c_hf_0": "", "caseDscr": "2026 ES 00449", "submitLink": "Search"},
        )
        grid_rows, _ = _parse_results_grid(r6.text)
        print(f"  Grid rows for 2026 ES 00449: {len(grid_rows)}")

        detail_x = next((r["detail_x"] for r in grid_rows if r["detail_x"]), None)
        if not detail_x:
            print("  No detail link found")
            return

        await asyncio.sleep(_REQ_DELAY_SEC)
        detail_html = await session.get_detail_page(detail_x)
        parties = _parse_detail_parties(detail_html)
        decedent = _decedent_from_parties(parties)

        if decedent:
            name = decedent.get("name", "")
            blob = decedent.get("address_blob", "")
            street, city, zip5 = _split_address_blob(blob)
            print(f"  Decedent name: {name!r}")
            print(f"  Address blob:  {blob!r}")
            print(f"  Parsed street: {street!r} | city: {city!r} | zip: {zip5!r}")
            if "Conlin" in (street or ""):
                print("  ANCHOR CONFIRMED: 3250 Conlin Drive, Akron OH 44319")
            elif "3250" in (blob or "") and "Conlin" in (blob or ""):
                print("  ANCHOR CONFIRMED (from blob): 3250 Conlin Drive")
        else:
            print("  No Decedent party found in detail page")
            print("  Parties:", [(p["name"], p["role"]) for p in parties])


if __name__ == "__main__":
    asyncio.run(_dry_run_demo())
