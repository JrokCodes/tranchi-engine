"""
Toledo Legal News (Lucas County) sheriff sale + foreclosure filing scraper.

TWO INDEX PAGES (TownNews CMS — `tncms` flex template):
  1. Sheriff Sale roster (matches the RealAuction Wed/Thu auctions):
       https://www.toledolegalnews.com/legal_notices/foreclosure_sherrif_sales_lucas/
     Signal type derived from the parsed sale date weekday:
       Wed -> mortgage_foreclosure, Thu -> tax_delinquent_foreclosure.

  2. Foreclosure complaint FILINGS (pre-distress lead — months before any sale):
       https://www.toledolegalnews.com/legal_notices/foreclosures/
     Signal type is ALWAYS `foreclosure_filing` (the lead lever).

WHY HTML, NOT THE JSON SEARCH:
  The TownNews /search/ endpoint sits behind a bot-tier 1 rate limiter
  (`x-tncms-bot-tier: 1` response header). A handful of /search/ calls from a
  non-warmed IP yields HTTP 429 with a multi-minute cooldown. The flex index
  template, however, pre-renders the first 51 article cards inline in the HTML
  (`discovery_config.offset=51`); the search endpoint only feeds items 52+. The
  51-item HTML window is a full refresh window for Lucas's volume, so we never
  hit the 429-prone path. Pace ~1 req/sec for the detail pages.

ARTICLE CARD SHAPE (verified live 2026-06-21):
  <a href="/legal_notices/foreclosure_sherrif_sales_lucas/ci2026-01128/article_<uuid>.html">
    CI2026-01128
  </a>
  Detail body contains:
    Case No. CI2026-01128
    <plaintiff> vs <defendant>
    "highest bidder on July 15, 2026 at 10:00 A.M."     (sale date)
    "online on July 29, 2026"                            (re-offer date — second sale)
    "Parcel No(s). 23-18171"                             (DD-DDDDD; strips to 7-digit PARID)
    Property address line (city + zip in the same line)
    "Appraised at $X,XXX"                                (mortgage rows only)

FILING-page detail rarely carries a parcel number (complaint notices are filed
by address before parcel is appended); when absent the lead emits
source_listing_id=None and the address backfills via the AREIS spine on the
gate side.

CROSS-CHECK & DEDUP:
  Each sheriff-sale row is the SAME deal as the matching RealAuction roster row
  (same case # / same PARID / same Wed-or-Thu sale date). The orchestrator dedups
  by (market='lucas', source_listing_id=PARID) across sources before activating.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.scrapers._time import today_et
from app.scrapers.base import ListingScraper
from app.scrapers.db import normalize_parcel_for_market
from app.scrapers.models import RawListing
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

SITE_NAME = "Toledo Legal News"
_MARKET = "lucas"
_BASE = "https://www.toledolegalnews.com"
_SHERIFF_INDEX = f"{_BASE}/legal_notices/foreclosure_sherrif_sales_lucas/"
_FILING_INDEX = f"{_BASE}/legal_notices/foreclosures/"

_TIMEOUT = 30.0
_INTER_REQ_DELAY = 2.5       # TLN bot-tier 1 throttle bans rapid hits; ~25 req/min is safe
_RETRY_ATTEMPTS = 4
_RETRY_BASE_DELAY = 6.0      # exponential — TLN's tier-1 throttle requires multi-second cooldown

# Lucas signal weekdays — same convention as lucas_realauction.
_MORTGAGE_WEEKDAY = 2   # Wednesday
_TAX_WEEKDAY = 3        # Thursday

# Article card link href pattern — both indexes look the same way.
_ARTICLE_HREF_RE = re.compile(r"^/legal_notices/[^/]+/[^/]+/article_[0-9a-f-]+\.html$", re.I)

# Parcel: DD-DDDDD (display) -> 7-digit PARID via normalize_parcel_for_market.
_PARCEL_RE = re.compile(r"\b(\d{2}-\d{5})\b")
# Case number: 'CI2025-00547', 'CI20261372', 'TF2025-00090', 'G-4801-TF-...'.
_CASE_RE = re.compile(r"\b(CI\d{4}-?\d{3,5}|TF\d{4}-?\d{3,5}|G-\d{4}-[A-Z]{2,4}-[0-9-]+)\b", re.I)
# Sale-day month-name + year.
_LONG_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2}),\s+(\d{4})\b", re.I,
)
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January","February","March","April","May","June","July","August","September","October","November","December"], 1)}

# Address line — match a full "STREET, CITY, OH ZIP" sequence in one shot.
# Street suffix is matched as a separate group so the captured `street` ends at the
# suffix; the city is whatever sits between the street and the OH/Ohio state token.
_STREET_SUFFIX = (
    r"(?:STREET|ST|AVENUE|AVE|ROAD|RD|DRIVE|DR|BOULEVARD|BLVD|COURT|CT|LANE|LN|"
    r"WAY|PLACE|PL|TERRACE|TER|PARKWAY|PKWY|CIRCLE|CIR|TRAIL|TRL|HEIGHTS|HTS|"
    r"HIGHWAY|HWY|ROW|ROUTE|RTE)"
)
_ADDR_RE = re.compile(
    rf"\b(\d{{1,6}}[A-Z]?\s+[A-Z0-9 .'\-]+?\s+{_STREET_SUFFIX})"
    r"\b\s*,?\s*"
    r"(?:(?:Unit|Apt\.?|Apartment|Suite|Ste\.?|#)\s*[A-Z0-9]+\s*,?\s*)?"
    r"([A-Z][A-Z .\-]+?)?\s*,?\s*"
    r"(?:Ohio|OH)\b\s*,?\s*(\d{5})?",
    re.IGNORECASE,
)
_APPRAISE_RE = re.compile(r"Appraised\s+(?:at|value)\s*\$?([\d,]+)", re.I)


def _parse_long_date(text: str) -> date | None:
    m = _LONG_DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    except (KeyError, ValueError):
        return None


def _parse_money(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^0-9.]", "", str(raw))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _signal_for_sale_weekday(sale_date: date | None) -> str | None:
    if sale_date is None:
        return None
    wd = sale_date.weekday()
    if wd == _MORTGAGE_WEEKDAY:
        return "mortgage_foreclosure"
    if wd == _TAX_WEEKDAY:
        return "tax_delinquent_foreclosure"
    return None


async def _get_with_retry(
    client: httpx.AsyncClient, url: str, *, headers: dict[str, str] | None = None,
) -> str | None:
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url, headers=headers, timeout=_TIMEOUT)
            if resp.status_code == 429:
                if attempt == _RETRY_ATTEMPTS:
                    logger.error("Toledo Legal News: 429 on %s after %d retries", url, attempt)
                    return None
                wait = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning("Toledo Legal News: 429 on %s — sleeping %.1fs", url, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error("Toledo Legal News GET %s failed: %s", url, exc)
                return None
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return None


def _extract_article_links(index_html: str) -> list[str]:
    """Pull unique article detail URLs from an index page's inline cards."""
    soup = BeautifulSoup(index_html, "html.parser")
    seen: set[str] = set()
    out: list[str] = []
    for a in soup.find_all("a", href=_ARTICLE_HREF_RE):
        href = a.get("href").split("?")[0]
        if href in seen:
            continue
        seen.add(href)
        out.append(urljoin(_BASE, href))
    return out


def _parse_detail(html: str, source_url: str, source_kind: str) -> RawListing | None:
    """Parse one TLN article into a RawListing.

    source_kind ∈ {'sheriff', 'filing'}:
      'sheriff' -> derive signal_type from sale_date weekday (Wed/Thu);
      'filing'  -> always 'foreclosure_filing'.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Title (case # in URL slug, also in h1)
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    # Date the notice was published — TLN renders this in <meta itemprop=datePublished>
    pub_meta = soup.find("meta", attrs={"itemprop": "datePublished"})
    pub_iso = pub_meta.get("content") if pub_meta else None
    filing_date = None
    if pub_iso:
        try:
            filing_date = datetime.fromisoformat(pub_iso).date()
        except ValueError:
            filing_date = None

    # Body — TNCMS wraps the article in subscriber-only/asset-content blocks.
    body_node = (
        soup.find("div", class_=re.compile(r"asset-content|subscriber-only|tnt-content|tnt-asset-content"))
        or soup.find("section", class_=re.compile(r"body|content"))
        or soup.find("article")
        or soup
    )
    body = body_node.get_text("\n", strip=True)
    if not body:
        return None

    # Case # — title first, fall back to body.
    case_m = _CASE_RE.search(title) or _CASE_RE.search(body)
    case_number = case_m.group(1).upper().replace(" ", "") if case_m else None

    # Parcel — DD-DDDDD if the notice carries one.
    parcels = sorted(set(_PARCEL_RE.findall(body)))
    if parcels:
        source_listing_id = normalize_parcel_for_market(parcels[0], _MARKET)
    else:
        source_listing_id = None

    # Sale date (sheriff sales) — first long-date appearance after a "highest bidder"
    # or "auction" cue; the *first* date pattern in the body is the primary sale, the
    # *second* is the re-offer (we capture only the primary). For filings, the body
    # rarely carries a sale date.
    sale_date = _parse_long_date(body) if source_kind == "sheriff" else None

    # Signal type
    if source_kind == "sheriff":
        signal_type = _signal_for_sale_weekday(sale_date)
        if signal_type is None:
            # Unparseable sale date / wrong weekday — keep the row but mark mortgage
            # by default (most sheriff sales are mortgage); orchestrator will re-derive.
            signal_type = "mortgage_foreclosure"
    else:
        signal_type = "foreclosure_filing"

    # Address — heuristic. Filings usually have it; sheriff body has it too.
    addr_m = _ADDR_RE.search(body)
    if addr_m:
        property_address = addr_m.group(1).strip().title()
        property_city = (addr_m.group(2) or "").strip().title() or None
        property_zip = addr_m.group(3) or None
    else:
        property_address = "MULTIPLE"
        property_city = None
        property_zip = None

    appraise_m = _APPRAISE_RE.search(body)
    appraised_value_usd = _parse_money(appraise_m.group(1)) if appraise_m else None

    return RawListing(
        source_site=SITE_NAME,
        case_number=case_number,
        source_listing_id=source_listing_id,
        signal_type=signal_type,
        property_address=property_address,
        property_city=property_city,
        property_county="Lucas",
        property_state="OH",
        property_zip=property_zip,
        sale_date=sale_date,
        filing_date=filing_date if signal_type == "foreclosure_filing" else None,
        appraised_value_usd=appraised_value_usd,
        source_url=source_url,
        status="active",
    )


class LucasLegalNewsScraper(ListingScraper):
    """Toledo Legal News (Lucas County) — sheriff sale roster + foreclosure-FILING leads."""

    site_name = SITE_NAME

    def __init__(self, dry_run: bool = False, max_detail: int | None = None) -> None:
        self.dry_run = dry_run
        self.max_detail = max_detail  # cap detail fetches for the dry-run

    async def fetch_and_parse(self) -> list[RawListing]:
        today = today_et()
        all_rows: list[RawListing] = []

        async with httpx.AsyncClient(
            headers=default_headers(), timeout=_TIMEOUT, follow_redirects=True,
        ) as client:
            # Foreclosure-complaint FILINGS moved to lucas_foreclosure_filings.py (signal
            # path → pre-distress lead). This scraper now owns ONLY the sheriff-sale roster
            # (buy_now listings); emitting filings here as buy_now would suppress their leads.
            for index_url, source_kind in ((_SHERIFF_INDEX, "sheriff"),):
                index_html = await _get_with_retry(client, index_url)
                if not index_html:
                    logger.error("Toledo Legal News: index fetch failed for %s", index_url)
                    continue
                await asyncio.sleep(_INTER_REQ_DELAY)

                links = _extract_article_links(index_html)
                logger.info("Toledo Legal News (%s): %d article cards", source_kind, len(links))
                if self.max_detail:
                    links = links[: self.max_detail]

                for url in links:
                    html = await _get_with_retry(client, url)
                    await asyncio.sleep(_INTER_REQ_DELAY)
                    if not html:
                        continue
                    listing = _parse_detail(html, source_url=url, source_kind=source_kind)
                    if listing is not None:
                        # Drop sheriff-side rows older than today (sale already past).
                        if source_kind == "sheriff" and listing.sale_date and listing.sale_date < today:
                            continue
                        all_rows.append(listing)

        logger.info(
            "Toledo Legal News: total %d rows (mortgage=%d, tax=%d, filing=%d)",
            len(all_rows),
            sum(1 for r in all_rows if r.signal_type == "mortgage_foreclosure"),
            sum(1 for r in all_rows if r.signal_type == "tax_delinquent_foreclosure"),
            sum(1 for r in all_rows if r.signal_type == "foreclosure_filing"),
        )
        return all_rows


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)

    async def _dry_run() -> None:
        print(f"\n=== Toledo Legal News dry-run ===\n")
        scraper = LucasLegalNewsScraper(dry_run=True, max_detail=4)
        rows = await scraper.fetch_and_parse()
        print(f"\nFetched {len(rows)} rows total\n")
        for r in rows[:6]:
            d = r.model_dump(exclude={"source_site"})
            d = {k: v for k, v in d.items() if v is not None}
            print(json.dumps(d, default=str, indent=2))
            print()

    asyncio.run(_dry_run())
