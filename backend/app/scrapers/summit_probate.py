"""
Summit County (OH / Akron) Probate Court -- Estate cases via CourtView eServices v1.56.

Site:     https://search.summitohioprobate.com/eservices/
Vendor:   equivant CourtView Justice Solutions -- eServices v1.56.01 (Apache Wicket on IIS)
Access:   PUBLIC, no auth. Playwright Chromium (headless) for session establishment.

────────────────────────────────────────────────────────────────────────────────
INVARIANT -- v1.56 MIGRATION (httpx is dead):
CourtView upgraded from v1.52 to v1.56.01.  httpx can no longer establish a
session -- the BrowserInfo fingerprint POST is accepted (302) but all follow-up
GETs redirect back to the React shell at /eservices/ui/.  A TLS/JA3 fingerprint
check gates real browsers from headless scrapers at the TCP level; spoofing
headers is insufficient.  The ONLY fix is a real Chromium browser.

New establish flow:
  1. Navigate Playwright to /eservices/casesearch (React route).
     The React shell detects a public-search intent and auto-navigates via the
     BrowserInfo fingerprint redirect directly to a Wicket search.page.N with
     the Search tabs already rendered.  No disclaimer step (removed in v1.56).
  2. All subsequent Wicket requests (tab GET, search POST, pagination GET, detail
     GET) are made via page.evaluate(fetch()) -- same-origin requests share the
     browser's established JSESSIONID, return 200, and let us keep the existing
     BeautifulSoup parsers.

INVARIANT -- DYNAMIC WICKET FORM IDs (do NOT hardcode):
In v1.56 the Wicket form id shifts every page load (id7d, id81, idfb, etc.).
Hardcoding "id8f" (the v1.52 id) silently causes every POST to fail with a
form re-render.  Always derive:
  - The SEARCH form by locating the <form> whose HTML contains name="caseCd".
  - The form action ?x= token from that form's action attribute.
  - The hidden field name as f"{form_id}_hf_0".

INVARIANT -- FORMDATA vs URLSearchParams:
Wicket v1.56 rejects multipart/form-data (FormData).  Use URLSearchParams
(application/x-www-form-urlencoded) in the JavaScript fetch() call.

INVARIANT -- WICKET SINGLE-USE ?x= TOKENS (do NOT cache or reuse):
Every ?x= continuation token in a response page is consumed on the next request.
Re-using a stale token silently resets to home.page.  Re-parse the returned HTML
at each hop to obtain the next fresh token.

INVARIANT -- DISCLAIMER POST IS REMOVED in v1.56:
The old establish() steps 2-3 (fingerprint POST + disclaimer onclick onclick_x)
no longer exist in v1.56.  The React-to-Wicket bridge replaces them entirely.
Do not attempt to re-add the disclaimer POST -- the endpoint is gone.

INVARIANT -- SELECT VALUES ARE SPACE-PADDED TO FIXED WIDTH:
caseCd must be 'ES        ' (10 chars), statCd 'O         ' (10 chars).  Sending
unpadded values silently no-ops the filter -- you get all case types / all statuses.

INVARIANT -- BIND DECEDENT, NOT FIDUCIARY:
Each detail page shows two Party blocks (Decedent + Fiduciary), both with
addresses.  Always bind to the block whose role text contains 'Decedent'.  The
Fiduciary address is the estate administrator's home, not the estate property.

INVARIANT -- DETAIL PAGE DOM CHANGED IN v1.56:
The old subSectionHeader2 / ptyInfoLabel / ptyType / ptyContact / ptyContactInfo
CSS class structure was replaced.  New structure:
  Party block container: div.rowodd or div.roweven
    div.content-title.row
      span.pty-name  (party name)
      span.pty-cd    (role, e.g. " - Decedent")
    div.column.pty-contact
      li.ptyContactInfo
        div.addrLn1  (building name OR street when addrLn2 is empty)
        div.addrLn2  (street; non-empty overrides addrLn1 as the street)
        div.state-city-zip  ("City , OH NNNNN")
Address is now structured -- _split_address_blob is preserved for fallback but
not the primary path.

INVARIANT -- PRECISION-FIRST JOIN + AMBIGUITY CAP:
Parcel resolution against tranchi.parcels uses house-number + street substring
match (address_anchor).  Surname-only joins are REJECTED.  If an address
resolves to more than _AMBIGUITY_CAP parcels (multi-unit building), emit nothing.
Quality over recall.

INVARIANT -- case_number KEEPS ITS SPACES:
Store case_number as '2026 ES 00449' (with the spaces).  The orchestrator's
probate_transfer_rule does substring(1,4) for year and regex ^[0-9]{4} ES to
detect Summit probate cases.  Stripping the spaces breaks the year-parse.

INVARIANT -- DATE RANGE MAX 12 MONTHS:
CourtView rejects 'Begin and End must be within 12 months.'  Chunk into
<=12-month windows; the cursor's last_window_end is the carry-forward anchor.

INVARIANT -- fileDateRange returns ancillary filings of OLD cases:
A 2005 ES case that had new docket activity in the window appears in results
with its original case number.  Filter on case_number year == current year (or
<=3 years back) to avoid ingesting ancient cases.  Ancillary cases have an ' A'
or ' B' suffix -- skip them.

INVARIANT -- pageSize=3 (500) is a result-set cap, not a display-page size:
CourtView always renders 50 rows per page.  '500' (option value 3) caps the
total result set to 500 cases.  Pagination via 'Next' links is still required.
Max theoretical pages = 500/50 = 10 per search window.
────────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

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
# Entry URL: React casesearch route auto-bridges to Wicket search.page.N.
# Navigating here with a real Chromium establishes the JSESSIONID session
# without needing the old BrowserInfo fingerprint POST or disclaimer step.
_ENTRY_URL = "https://search.summitohioprobate.com/eservices/casesearch"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# fileDateRange max per CourtView constraint.  We chunk into 12-month windows.
_MAX_WINDOW_DAYS = 365

# Rolling lookback for fresh installs (no cursor row yet).
# First run fetches the last 12 months; subsequent runs use the cursor window.
_FRESH_LOOKBACK_DAYS = 365

# Oldest case year we will ingest (skip ancillary filings of ancient cases).
_MIN_CASE_YEAR = date.today().year - 2

# Precision thresholds for parcel join
_AMBIGUITY_CAP = 5

# Request pacing (polite crawl)
_REQ_DELAY_SEC = 0.5

# Playwright page load timeout
_PAGE_TIMEOUT_MS = 45_000

# Ancillary case suffix pattern (' A' / ' B')
_ANCILLARY_SUFFIX_RE = re.compile(r"\s+[AB]$")

# Parcel join -- address parsing regexes (kept for fallback)
_ADDR_BLOB_RE = re.compile(
    r"^(\d+\s+.+?)\s*,?\s*([A-Za-z][A-Za-z\s]{1,30}),?\s*"
    r"(OH|Ohio),?\s*(\d{5})(?:-\d{4})?$",
    re.IGNORECASE,
)

# state-city-zip div parser: "Akron , OH 44333" (with nbsp/extra spaces)
_SCZ_RE = re.compile(
    r"([A-Za-z][A-Za-z .'-]{1,40?})\s*,\s*(?:OH|Ohio)\s+(\d{5})",
    re.IGNORECASE,
)

# ─────────────────────────────────────────────────────────────────────────────
# Cursor DDL (declare for migration 018 -- do NOT write the migration here)
# ─────────────────────────────────────────────────────────────────────────────
#
# Migration 018 must create:
#
# CREATE TABLE IF NOT EXISTS tranchi.summit_probate_cursor (
#     id            INTEGER PRIMARY KEY DEFAULT 1,
#     last_window_end DATE NOT NULL,
#     updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
#     CHECK (id = 1)
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
        logger.warning("SummitProbate: cursor read failed (%s) -- using bootstrap date", exc)
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
# Wicket session helpers (dynamic ID extraction -- v1.56 form IDs shift per load)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_search_form(html: str) -> tuple[str, str]:
    """Return (form_action_x, hidden_field_name) for the Case Type SEARCH form.

    The page may contain multiple forms (e.g. a BrowserInfo/postback form plus the
    search form).  We identify the SEARCH form by the presence of name='caseCd'
    inside it -- never by form ID (which shifts every page load in v1.56).

    Returns:
        (x_token, hf_name) -- empty strings if not found.
    """
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        if form.find(attrs={"name": "caseCd"}):
            form_id = form.get("id", "")
            action = form.get("action", "")
            x_m = re.search(r"\?x=([^&\"]+)", action)
            x_token = x_m.group(1) if x_m else ""
            # Hidden field name = formId + "_hf_0" (Wicket convention)
            hf_name = f"{form_id}_hf_0" if form_id else ""
            return x_token, hf_name
    return "", ""


def _find_tab_x(html: str, tab_name: str) -> str:
    """Return the ?x= token from a named search tab link."""
    soup = BeautifulSoup(html, "html.parser")
    tab = soup.find("a", string=re.compile(rf"\b{re.escape(tab_name)}\b", re.I))
    if not tab:
        return ""
    href = tab.get("href", "")
    m = re.search(r"\?x=(.+)", href)
    return m.group(1) if m else ""


# ─────────────────────────────────────────────────────────────────────────────
# JavaScript fetch helpers (same-origin, run inside established Playwright page)
# ─────────────────────────────────────────────────────────────────────────────

# GET via same-origin fetch -- credentials:include carries the JSESSIONID cookie
_JS_GET = (
    "async (u) => { "
    "const r = await fetch(u, {credentials:'include', redirect:'follow'}); "
    "return await r.text(); "
    "}"
)

# POST the Case Type search form via URLSearchParams (NOT FormData -- Wicket v1.56
# rejects multipart/form-data; application/x-www-form-urlencoded is required).
_JS_POST_SEARCH = """
async (args) => {
    const [url, hf_name, begin, end] = args;
    const params = new URLSearchParams();
    params.append(hf_name, '');
    params.append('topSearchPanel:pageSize', '3');
    params.append('fileDateRange:dateInputBegin', begin);
    params.append('fileDateRange:dateInputEnd', end);
    params.append('caseCd', 'ES        ');
    params.append('statCd', 'O         ');
    params.append('ptyCd', ' ');
    params.append('submitLink', 'Search');
    const r = await fetch(url, {
        method: 'POST',
        body: params,
        credentials: 'include',
        redirect: 'follow',
    });
    return await r.text();
}
"""


class _CourtViewSession:
    """Manages a Playwright browser context for the CourtView v1.56 Wicket walk.

    Lifecycle:
      1. establish() -- navigate to /eservices/casesearch, which auto-completes
         the BrowserInfo fingerprint and lands on a Wicket search.page.N form.
         No disclaimer step (removed in v1.56).
      2. goto_case_type_tab() -- GET Case Type tab via fetch(); returns search form
         action ?x= token and hidden field name.
      3. post_case_type_search() -- POST search form via fetch() with URLSearchParams.
      4. get_next_page() / get_detail_page() -- GET paginated results or case detail.

    All subsequent Wicket requests use page.evaluate() so they share the
    browser's JSESSIONID (same-origin fetch).  The browser is always closed
    on __aexit__ even if an exception occurs.
    """

    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._page: Any = None

    async def __aenter__(self) -> "_CourtViewSession":
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().__aenter__()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._ctx = await self._browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 900},
        )
        self._page = await self._ctx.new_page()
        return self

    async def __aexit__(self, *_: Any) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.__aexit__(None, None, None)
        except Exception:
            pass
        self._page = None
        self._ctx = None
        self._browser = None
        self._pw = None

    @property
    def page(self) -> Any:
        if self._page is None:
            raise RuntimeError("_CourtViewSession not entered as a context manager")
        return self._page

    async def establish(self) -> str:
        """Navigate to casesearch, wait for Wicket form, return landing HTML.

        The React /eservices/casesearch route auto-performs the BrowserInfo
        fingerprint redirect and lands on a Wicket search.page.N that already
        has the Name/Case Number/Case Type tabs.  No disclaimer POST required.
        """
        await self.page.goto(_ENTRY_URL, wait_until="networkidle", timeout=_PAGE_TIMEOUT_MS)
        content = await self.page.content()
        logger.info(
            "SummitProbate: session established at %s (content_len=%d)",
            self.page.url, len(content),
        )
        return content

    async def goto_case_type_tab(self, landing_html: str) -> tuple[str, str]:
        """GET the Case Type search tab.

        Returns:
            (form_action_x, hf_name) -- the token and hidden field to POST with.
        """
        tab_x = _find_tab_x(landing_html, "Case Type")
        if not tab_x:
            raise RuntimeError("SummitProbate: 'Case Type' tab link not found on search page")

        tab_url = _BASE_URL + "search.page.3?x=" + tab_x
        await asyncio.sleep(_REQ_DELAY_SEC)
        tab_html = await self.page.evaluate(_JS_GET, tab_url)

        form_x, hf_name = _extract_search_form(tab_html)
        if not form_x:
            raise RuntimeError(
                "SummitProbate: search form (caseCd) not found on Case Type tab response"
            )
        logger.debug("SummitProbate: Case Type tab loaded, form_x=%s hf=%s", form_x[:20], hf_name)
        return form_x, hf_name

    async def post_case_type_search(
        self,
        form_action_x: str,
        hf_name: str,
        begin: str,
        end: str,
    ) -> str:
        """POST the Case Type search form. Returns the results page HTML."""
        search_url = _BASE_URL + "search.page.3?x=" + form_action_x
        await asyncio.sleep(_REQ_DELAY_SEC)
        results_html = await self.page.evaluate(
            _JS_POST_SEARCH, [search_url, hf_name, begin, end]
        )
        return results_html

    async def get_next_page(self, next_x: str) -> str:
        """GET the next results page using a single-use ?x= pagination token."""
        url = _BASE_URL + "searchresults.page?x=" + next_x
        await asyncio.sleep(_REQ_DELAY_SEC)
        return await self.page.evaluate(_JS_GET, url)

    async def get_detail_page(self, x_token: str) -> str:
        """GET a case detail page using its single-use ?x= token."""
        url = _BASE_URL + "searchresults.page?x=" + x_token
        await asyncio.sleep(_REQ_DELAY_SEC)
        return await self.page.evaluate(_JS_GET, url)


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
    Column mapping (confirmed live 2026-06-17 on v1.56):
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
    CourtView always renders 50 rows per page.  Max 10 pages per 500-row window.
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
    next_link = soup.find("a", string=re.compile(r"^Next$|^>>$|^>>>$", re.I))
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

    v1.56 DOM structure (subSectionHeader2 is GONE -- use new structure):
        Party block container: div.rowodd or div.roweven
          div.content-title.row
            span.pty-name  (e.g. 'Marecek, Nancy H.')
            span.pty-cd    (e.g. ' - Decedent')
          div.column.pty-contact
            li.ptyContactInfo
              div.addrLn1  ('Timberland Ridge' or '3558 Ridgewood Road')
              div.addrLn2  ('3558 Ridgewood Road' when addrLn1 is a building name; empty otherwise)
              div.state-city-zip  ('Akron , OH 44333')

    Returns a list of dicts:
        name (str), role (str), street (str|None), city (str|None), zip5 (str|None)

    The caller binds to the Decedent party (role contains 'Decedent').
    """
    soup = BeautifulSoup(html, "html.parser")
    parties = []

    # Party blocks are in div.rowodd / div.roweven containers
    party_containers = soup.find_all(
        "div", class_=lambda c: c and ("rowodd" in c or "roweven" in c)
    )

    for container in party_containers:
        # Party header
        ct = container.find("div", class_=lambda c: c and "content-title" in c)
        if not ct:
            continue
        name_span = ct.find("span", class_="pty-name")
        role_span = ct.find("span", class_="pty-cd")
        if not name_span:
            continue
        name = name_span.get_text(strip=True)
        role = role_span.get_text(strip=True).lstrip(" -").strip() if role_span else ""

        # Address (structured in v1.56 -- addrLn1/addrLn2 + state-city-zip)
        street: str | None = None
        city: str | None = None
        zip5: str | None = None

        pty_contact = container.find("div", class_=lambda c: c and "pty-contact" in c)
        if pty_contact:
            addr_li = pty_contact.find("li", class_="ptyContactInfo")
            if addr_li:
                ln1 = addr_li.find("div", class_="addrLn1")
                ln2 = addr_li.find("div", class_="addrLn2")
                scz_div = addr_li.find("div", class_="state-city-zip")

                ln1t = ln1.get_text(strip=True) if ln1 else ""
                ln2t = ln2.get_text(strip=True) if ln2 else ""
                # addrLn2 non-empty means addrLn1 is a building/complex name;
                # addrLn2 IS the street.  Otherwise addrLn1 IS the street.
                street = ln2t if ln2t else ln1t or None

                if scz_div:
                    # Normalize nbsp and extra whitespace
                    scz_raw = re.sub(r"\s+", " ", scz_div.get_text(" ").replace("\xa0", " ")).strip()
                    scz_m = re.match(
                        r"([A-Za-z][A-Za-z .'-]{1,40}?)\s*,\s*(?:OH|Ohio)\s+(\d{5})",
                        scz_raw,
                        re.IGNORECASE,
                    )
                    if scz_m:
                        city = scz_m.group(1).strip() or None
                        zip5 = scz_m.group(2)

        if name:
            parties.append({"name": name, "role": role, "street": street, "city": city, "zip5": zip5})

    return parties


def _split_address_blob(blob: str | None) -> tuple[str | None, str | None, str | None]:
    """Split '3250 Conlin DriveAkron,OH44319' into (street, city, zip5).

    Kept for legacy callers; the primary path now uses structured address from
    _parse_detail_parties directly.  CourtView concatenates the address
    label+info without line breaks in some older endpoints, so the street
    bleeds directly into the city name.
    """
    if not blob:
        return None, None, None

    blob = re.sub(r"\s+", " ", blob).strip()
    zip_m = re.search(r"(\d{5})(?:-\d{4})?$", blob)
    zip5 = zip_m.group(1) if zip_m else None
    base = blob[: zip_m.start()].rstrip(" ,") if zip_m else blob

    state_m = re.search(r",?\s*(?:OH|Ohio)\s*$", base, re.I)
    if state_m:
        base = base[: state_m.start()].rstrip(" ,")

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
        cap_m = re.search(r"(\d)([A-Z][a-z])", base)
        if cap_m:
            street = base[: cap_m.start(2)].strip()
            city = base[cap_m.start(2):].strip()
        else:
            return blob, None, zip5

    city = city.rstrip(", ").strip() or None
    street = street.strip() or None
    return street, city, zip5


def _is_valid_case(case_num: str, min_year: int) -> bool:
    """Return True if this is a primary (non-ancillary) Estate case from min_year or later.

    Summit case format: 'YYYY ES NNNNN'
    Ancillary cases have an ' A' suffix: '2005 ES 00884 A'
    """
    if _ANCILLARY_SUFFIX_RE.search(case_num):
        return False
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

    parts = street.split()
    if not parts:
        return []

    house_num = parts[0] if parts[0].isdigit() else None
    street_body = " ".join(parts[1:]) if house_num else street

    # SUFFIX-AGNOSTIC MATCH: the decedent address carries the FULL street type
    # ("Ridgewood Road") but the GIS spine stores the ABBREVIATED form ("Ridgewood Rd"),
    # so an exact ILIKE on the full suffix matches 0 rows.  Drop the trailing
    # street-type word and match on the street-name STEM.
    _STREET_TYPES = {
        "drive", "dr", "street", "st", "avenue", "ave", "road", "rd", "lane", "ln",
        "court", "ct", "boulevard", "blvd", "way", "place", "pl", "circle", "cir",
        "trail", "trl", "terrace", "ter", "parkway", "pkwy", "highway", "hwy",
        "square", "sq", "loop", "run", "path", "point", "pt", "crossing", "xing",
    }
    _sb_parts = street_body.split()
    if len(_sb_parts) >= 2 and _sb_parts[-1].lower().strip(".") in _STREET_TYPES:
        street_body = " ".join(_sb_parts[:-1])

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
                   AND parcel_number ~ '^[0-9]{7}$'
                 LIMIT 20
                """,
                house_num,
                street_body[:20],
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

def _surname_mismatch(decedent_name: str | None, owner_name: str | None) -> bool:
    """True when the current owner's surname does NOT match the decedent's.

    The address-anchor join confirms the decedent LIVED at the parcel, not that
    they OWNED it.  If the current owner's surname is absent from the decedent's
    surname, it's a likely MIS-JOIN (decedent rented / never owned).
    """
    if not decedent_name or not owner_name:
        return False
    surname = decedent_name.split(",")[0].strip().split()
    if not surname:
        return False
    last = surname[0].upper()
    if len(last) < 2:
        return False
    return last not in owner_name.upper()


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
    # property_address = STREET ONLY (situs includes city, which double-prints on dashboard)
    _street_only = (situs_address or "").split(",")[0].strip()
    return RawListing(
        source_site=SITE_NAME,
        source_listing_id=normalize_parcel_number(parcel_number) or parcel_number,
        case_number=case_number,
        signal_type=SIGNAL_TYPE,
        property_address=canonical_address(_street_only) or _street_only or (situs_address or ""),
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
    """Summit County (OH) Probate Court -- open Estate cases via CourtView eServices v1.56.

    Session walk (Playwright Chromium + Apache Wicket hybrid):
      1. Navigate to /eservices/casesearch (React shell auto-completes BrowserInfo
         fingerprint, lands on Wicket search.page.N with tabs)
      2. GET Case Type search tab via fetch() -- extract dynamic form action x-token
      3. POST search (caseCd=ES, statCd=O, 12-month window) via fetch() -- results grid
      4. For each Decedent row -- GET detail page via fetch() -- parse address
      5. Address -- tranchi.parcels join (address_anchor, precision-first)
      6. Emit RawListing per parcel; advance cursor

    Cursor: tranchi.summit_probate_cursor (single row, id=1, last_window_end DATE).

    Args:
        pool:     asyncpg connection pool (required for cursor + parcel join).
                  Pass None only when dry_run=True (demo mode).
        dry_run:  Skip DB writes (cursor, signals).  RawListings still returned.
        min_case_year: Skip cases filed before this year (ancillary noise filter).
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
        self._min_case_year = (
            min_case_year if min_case_year is not None else date.today().year - 2
        )

    async def fetch_and_parse(self) -> list[RawListing]:
        """Walk CourtView for open Summit Estate cases.  Returns RawListing list."""
        today = date.today()
        if self.pool is not None:
            window_start = await _read_cursor(self.pool)
        else:
            window_start = today - timedelta(days=_FRESH_LOOKBACK_DAYS)

        # Build list of (begin_date, end_date) chunks, each <= 12 months
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
                landing_html = await session.establish()
                logger.info("SummitProbate: session established (v1.56 Playwright flow)")
            except Exception as exc:
                logger.error("SummitProbate: failed to establish session: %s", exc)
                raise

            for win_begin, win_end in windows:
                begin_str = win_begin.strftime("%m/%d/%Y")
                end_str = win_end.strftime("%m/%d/%Y")
                logger.info("SummitProbate: fetching window %s - %s", begin_str, end_str)

                try:
                    form_x, hf_name = await session.goto_case_type_tab(landing_html)
                    results_html = await session.post_case_type_search(
                        form_x, hf_name, begin_str, end_str
                    )
                except Exception as exc:
                    logger.error(
                        "SummitProbate: search POST failed for window %s-%s: %s",
                        begin_str, end_str, exc,
                    )
                    break

                # Check for feedback errors
                soup_check = BeautifulSoup(results_html, "html.parser")
                errors = soup_check.find_all(class_=re.compile(r"feedbackPanelERROR", re.I))
                if errors:
                    err_msgs = [e.get_text(strip=True) for e in errors]
                    logger.error(
                        "SummitProbate: search returned error for window %s-%s: %s",
                        begin_str, end_str, err_msgs,
                    )
                    break

                # Paginate through all result pages
                all_grid_rows: list[dict[str, Any]] = []
                current_html = results_html
                page_num = 1
                _MAX_PAGES = 10
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
                    "SummitProbate: %d total raw rows for window %s-%s",
                    len(all_grid_rows), begin_str, end_str,
                )

                # Deduplicate by case_number -- one detail fetch per case
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
                    detail_x = next(
                        (r["detail_x"] for r in case_rows if r["detail_x"]), None
                    )
                    if not detail_x:
                        logger.warning(
                            "SummitProbate: case %s has no detail link -- skipping", case_num
                        )
                        continue

                    decedent_row = next(
                        (r for r in case_rows if "decedent" in r.get("party_type", "").lower()),
                        case_rows[0],
                    )
                    case_status = decedent_row.get("case_status", "Open")
                    filing_date = _parse_filing_date(decedent_row.get("file_date_str", ""))
                    action_type = decedent_row.get("action_type", "")

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

                    decedent = _decedent_from_parties(parties)
                    if not decedent:
                        logger.debug(
                            "SummitProbate: no Decedent party found in detail for %s", case_num
                        )
                        continue

                    decedent_name = decedent.get("name", "") or ""
                    # v1.56: address is structured (street/city/zip5 are separate fields)
                    street = decedent.get("street")
                    city = decedent.get("city")
                    zip5 = decedent.get("zip5")

                    fiduciary = next(
                        (p for p in parties if p.get("role", "").lower() in ("fiduciary", "applicant")),
                        None,
                    )
                    fiduciary_name = fiduciary.get("name") if fiduciary else None

                    if not street:
                        logger.info(
                            "SummitProbate: case %s decedent %r has no address -- skip",
                            case_num, decedent_name,
                        )
                        continue

                    logger.info(
                        "SummitProbate: case %s | decedent=%r | address=%r %r %r",
                        case_num, decedent_name, street, city, zip5,
                    )

                    parcels = await _resolve_address_to_parcel(self.pool, street, city)

                    if not parcels:
                        logger.info(
                            "SummitProbate: case %s no parcel match for %r",
                            case_num, street,
                        )
                        continue

                    if len(parcels) > _AMBIGUITY_CAP:
                        logger.info(
                            "SummitProbate: case %s address %r ambiguous (%d parcels > cap=%d) -- skipping",
                            case_num, street, len(parcels), _AMBIGUITY_CAP,
                        )
                        continue

                    tier = "confirmed" if len(parcels) == 1 else "probable"
                    score = 0.95 if len(parcels) == 1 else 0.80

                    for p in parcels:
                        norm_parcel = normalize_parcel_number(p["parcel_number"])
                        if not norm_parcel:
                            continue

                        situs = p.get("situs_address") or f"{street}, {city or 'Akron'}, OH"

                        listing_tier = tier
                        if _surname_mismatch(decedent_name, p.get("owner_name")):
                            listing_tier = "review"
                            logger.info(
                                "SummitProbate: case %s parcel %s owner %r != decedent %r -- REVIEW",
                                case_num, norm_parcel, p.get("owner_name"), decedent_name,
                            )

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
                            match_confidence=listing_tier,
                            match_score=score,
                        )
                        all_listings.append(listing)

                        await _upsert_signal(
                            self.pool, norm_parcel, case_num,
                            decedent_name, score, self.dry_run,
                        )

                # Mark this window as successfully fetched BEFORE the inter-window
                # navigation refresh so cursor advances even if the next window's
                # session re-navigate fails.
                last_successful_end = win_end

                # Refresh landing_html for the next window iteration.
                # The Wicket ?x= tab token from the original landing is single-use
                # and was consumed when goto_case_type_tab() fetched it.  Re-navigate
                # to the entry URL (fast -- JSESSIONID is still live) to get a fresh
                # page with new tab tokens.
                try:
                    await asyncio.sleep(_REQ_DELAY_SEC)
                    await session.page.goto(_ENTRY_URL, wait_until="networkidle", timeout=_PAGE_TIMEOUT_MS)
                    landing_html = await session.page.content()
                    logger.debug("SummitProbate: session refreshed for next window")
                except Exception as exc:
                    logger.error("SummitProbate: could not refresh session between windows: %s", exc)
                    break

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
                " [DRY RUN -- cursor not advanced]" if self.dry_run else "",
                len(all_listings),
            )

        return all_listings


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run proof
# ─────────────────────────────────────────────────────────────────────────────

async def _dry_run_demo() -> None:
    """Smoke-test the full session walk without a real DB pool.

    Proves:
    1. Session establishment (v1.56 Playwright flow)
    2. Case Type search (ES + Open + recent window)
    3. Detail page fetch + decedent address extraction (new v1.56 DOM)
    4. Address parsing (street/city/zip from structured divs)

    DB writes are skipped (dry_run=True).  Parcel join is skipped (pool=None).
    """
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    print("\n=== SummitProbateScraper DRY RUN (v1.56 Playwright) ===\n")

    scraper = SummitProbateScraper(pool=None, dry_run=True)
    listings = await scraper.fetch_and_parse()

    print(f"\n--- {len(listings)} RawListings produced (parcel join skipped -- pool=None) ---\n")
    for idx, lst in enumerate(listings, 1):
        print(
            f"  [{idx:3d}] {lst.case_number:25s} | decedent={lst.decedent_name!r:35s} | "
            f"addr={lst.property_address!r} {lst.property_city} {lst.property_zip} | "
            f"parcel={lst.source_listing_id} | conf={lst.match_confidence}"
        )


if __name__ == "__main__":
    asyncio.run(_dry_run_demo())
