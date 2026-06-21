"""
Lucas County (OH / Toledo) sheriff/tax foreclosure auction scraper.

Source: https://lucas.sheriffsaleauction.ohio.gov  (RealAuction / RealForeclose)
Same platform as Summit (summit.sheriffsaleauction.ohio.gov) — same calendar /
PREVIEW / LOAD / retHTML grammar. Differences from Summit are minimal:

  - Mortgage sales on WEDNESDAY (Summit: Friday)
  - Tax-foreclosure sales on THURSDAY (Summit: Tuesday)
  - Mortgage deposit is a flat $5,000 (Summit: flat $1,000) — informational only;
    we parse it verbatim and never gate on the amount.
  - Parcel DISPLAY form is DD-DDDDD ('12-10314' -> '1210314' canonical). The 96
    condo/split bases come through as <8digits>S ('04349092S'); preserved verbatim
    by normalize_parcel_lucas. Non-PARID rows ('MULTIPLE', manufactured-home lots)
    get source_listing_id=None.
  - Lucas market dispatches parcel normalization to normalize_parcel_lucas
    (F-008) because a bare '1210314' is byte-identical to a Summit parcel — only
    the `market` column disambiguates.

WEEKDAY + SIGNATURE = CHANNEL SPLIT (do NOT discriminate by plaintiff name):
  - Wednesday -> mortgage_foreclosure: Appraised > 0; Opening Bid = 2/3 appraisal
    (R.C. 2329.52). Case-number prefix observed: 'CI...' (Civil).
  - Thursday  -> tax_delinquent_foreclosure: Appraised = $0.00; Opening Bid =
    taxes + costs; Deposit = 10% of bid (ORC 5721.19). Case prefixes: 'TF...'
    or 'G-4801-TF-...' (treasurer-initiated).

CROSS-SOURCE PROOF (recon 2026-06-21, live):
  Spine PARID '1210314' (STEPHENS CORNELIUS, 3941 VERMAAS AVE, LUC 510, APRTOT
  $71,800) appears on the Thu 06/25/2026 roster as case 'TF2025-00090', display
  parcel '12-10314'. Round-trip: '12-10314' -> normalize_parcel_lucas ->
  '1210314' = spine PARID. The 4-source identity loop is closed.

INVARIANTS — same as Summit, copied here for the local reader:
  1. Full Chrome UA required (any plain UA -> 403).
  2. PREVIEW sets a session cookie scoping LOAD to the auction date — one shared
     httpx.AsyncClient per run, PREVIEW must precede each date's LOAD.
  3. WEEKDAY + SIGNATURE, never plaintiff.
  4. Parcel ID / Property Address can be 'MULTIPLE' -> source_listing_id=None.
  5. 9-digit unhyphenated ZIPs get truncated to 5.
  6. Area W paginates 10/page; last page repeats `rlist` — stop on equality.
  7. Letter-suffix case numbers (CIxxxA/B/C) stay distinct — strip only the
     trailing parenthesized (seq) number.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from typing import Any

import httpx

from app.scrapers._time import today_et
from app.scrapers.base import ListingScraper
from app.scrapers.db import normalize_parcel_for_market
from app.scrapers.models import RawListing
from app.scrapers.user_agents import random_ua

logger = logging.getLogger(__name__)

SITE_NAME = "Lucas Sheriff Sale (RealAuction)"
_MARKET = "lucas"

_BASE_URL = "https://lucas.sheriffsaleauction.ohio.gov/index.cfm"
_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0
_INTER_REQ_DELAY = 1.0
_CALENDAR_LOOKAHEAD_MONTHS = 3

# Lucas channel weekdays (Monday=0 … Sunday=6)
_MORTGAGE_WEEKDAY = 2   # Wednesday
_TAX_WEEKDAY = 3        # Thursday

# Lucas PARID display form on RealAuction: DD-DDDDD ('12-10314') — 7 digits after
# the dash strip. The 96 condo bases come through as <8digits>S ('04349092S').
_LUCAS_PARCEL_OK = re.compile(r"^(\d{7}|\d{8}S)$")


# ─────────────────────────────────────────────────────────────────────────────
# Parse helpers (identical to Summit — keep in sync if those evolve)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_money(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^0-9.]", "", str(raw).strip())
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ("%m/%d/%Y", "%-m/%-d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _truncate_zip(raw: str | None) -> str | None:
    if not raw:
        return None
    z = str(raw).strip()
    if re.match(r"^\d{9,}$", z):
        return z[:5]
    return z


def _strip_seq(case_raw: str) -> str:
    return re.sub(r"\s*\(\d+\)\s*$", "", case_raw.strip())


def _build_headers() -> dict[str, str]:
    return {
        "User-Agent": random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }


def _build_json_headers() -> dict[str, str]:
    hdrs = _build_headers()
    hdrs["Accept"] = "application/json, text/javascript, */*; q=0.01"
    hdrs["X-Requested-With"] = "XMLHttpRequest"
    hdrs["Referer"] = _BASE_URL
    return hdrs


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    as_json: bool = False,
) -> Any | None:
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url, params=params, headers=headers, timeout=_TIMEOUT)
            resp.raise_for_status()
            if as_json:
                return resp.json()
            return resp.text
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error("Lucas RealAuction GET %s params=%s failed after %d attempts: %s",
                             url, params, attempt, exc)
                return None
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Calendar parsing
# ─────────────────────────────────────────────────────────────────────────────

_CALENDAR_DAY_RE = re.compile(
    r"""dayid=['"]([\d/]+)['"][^>]*>(?=(?:(?!dayid=).){0,800}CALTEXT)""",
    re.DOTALL,
)


def _parse_calendar(html_text: str, today: date) -> list[tuple[str, str]]:
    """Extract sale dates (Wed mortgage / Thu tax) from the calendar HTML.
    Skip dates < today and weekdays that aren't Wed/Thu."""
    results: list[tuple[str, str]] = []
    for m in _CALENDAR_DAY_RE.finditer(html_text):
        raw_dayid = m.group(1).strip()
        if not raw_dayid:
            continue
        sale_date = _parse_date(raw_dayid)
        if sale_date is None or sale_date < today:
            continue
        wd = sale_date.weekday()
        if wd == _MORTGAGE_WEEKDAY:
            signal_type = "mortgage_foreclosure"
        elif wd == _TAX_WEEKDAY:
            signal_type = "tax_delinquent_foreclosure"
        else:
            logger.debug("Lucas RealAuction: skipping %s (weekday=%d, not Wed/Thu)", raw_dayid, wd)
            continue
        results.append((raw_dayid, signal_type))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# retHTML token-template parser (identical to Summit)
# ─────────────────────────────────────────────────────────────────────────────

_FIELD_RE = re.compile(
    r"<th\s[^>]*>([^<]*)</th>\s*<td\s[^>]*>(.*?)(?=<tr|</tbody|$)",
    re.DOTALL | re.IGNORECASE,
)
_CITY_ZIP_RE = re.compile(r"^([A-Z][A-Z\s\-]+?)\s*,\s*(\d{5,9})\s*$", re.IGNORECASE)
_CITY_ONLY_RE = re.compile(r"^([A-Z][A-Z\s\-]+?)\s*,\s*(?:OH)?\s*$", re.IGNORECASE)


def _parse_ret_html(ret_html: str) -> list[dict[str, str]]:
    if not ret_html or not ret_html.strip():
        return []
    items: list[dict[str, str]] = []
    for item_block in ret_html.split("@A"):
        if "Case Status" not in item_block:
            continue
        fields: dict[str, str] = {}
        for seg in item_block.split("@G"):
            m = _FIELD_RE.search(seg)
            if not m:
                continue
            label_raw = m.group(1).strip().rstrip(":").strip()
            value_raw = m.group(2).strip()
            value_clean = re.sub(r"<[^>]+>", "", value_raw).strip()
            if label_raw:
                fields[label_raw] = value_clean
            else:
                fields["__city_zip__"] = value_clean
        if fields:
            items.append(fields)
    return items


def _extract_city_zip(fields: dict[str, str]) -> tuple[str | None, str | None]:
    raw = fields.get("__city_zip__", "").strip()
    if not raw:
        return None, None
    m = _CITY_ZIP_RE.match(raw)
    if m:
        return m.group(1).strip().title(), _truncate_zip(m.group(2).strip())
    m2 = _CITY_ONLY_RE.match(raw)
    if m2:
        return m2.group(1).strip().title(), None
    return None, None


def _parse_item(fields: dict[str, str], signal_type: str, sale_date: date) -> RawListing | None:
    """Map one retHTML field dict to a RawListing. Returns None to skip."""
    case_status_raw = fields.get("Case Status", "").strip().upper()
    if case_status_raw != "ACTIVE":
        logger.debug("Lucas RealAuction: skipping non-ACTIVE row (status=%r)", case_status_raw)
        return None

    case_raw = fields.get("Case #", fields.get("Case#", "")).strip()
    case_number = _strip_seq(case_raw) if case_raw else None

    parcel_raw = fields.get("Parcel ID", fields.get("Parcel", "")).strip()
    source_listing_id: str | None
    if parcel_raw.upper() == "MULTIPLE":
        source_listing_id = None
    elif parcel_raw:
        source_listing_id = normalize_parcel_for_market(parcel_raw, _MARKET)
        # Anything that isn't a 7-digit deal PARID or an 8-digit + 'S' condo base
        # can't join the spine — keep the listing but null the parcel key so it
        # never carries a bogus FK / dedup token (mirrors Summit's MHLOT guard).
        if source_listing_id and not _LUCAS_PARCEL_OK.match(source_listing_id):
            logger.debug("Lucas RealAuction: non-spine parcel form %r — nulling", parcel_raw)
            source_listing_id = None
    else:
        source_listing_id = None

    addr_raw = fields.get("Property Address", "").strip()
    property_address = addr_raw if addr_raw else "MULTIPLE"

    city, zip_code = _extract_city_zip(fields)

    appr_raw = fields.get("Appraised Value", "")
    if signal_type == "tax_delinquent_foreclosure":
        # Tax rows always show Appraised $0.00 — don't store the artifact.
        appraised_value_usd = None
    else:
        appraised_value_usd = _parse_money(appr_raw)

    opening_bid_usd = _parse_money(fields.get("Opening Bid", ""))
    deposit_usd = _parse_money(fields.get("Deposit Requirement", ""))

    return RawListing(
        source_site=SITE_NAME,
        case_number=case_number,
        source_listing_id=source_listing_id,
        signal_type=signal_type,
        property_address=property_address,
        property_city=city,
        property_county="Lucas",
        property_state="OH",
        property_zip=zip_code,
        sale_date=sale_date,
        appraised_value_usd=appraised_value_usd,
        opening_bid_usd=opening_bid_usd,
        deposit_usd=deposit_usd,
        auction_status=case_status_raw,
        status="active",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Roster fetcher
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_roster_for_date(
    client: httpx.AsyncClient,
    auction_date: str,
    signal_type: str,
    today: date,
) -> list[RawListing]:
    sale_date = _parse_date(auction_date)
    if sale_date is None or sale_date < today:
        return []

    preview_params = {"zaction": "AUCTION", "zmethod": "PREVIEW", "AuctionDate": auction_date}
    preview_html = await _get_with_retry(
        client, _BASE_URL, params=preview_params, headers=_build_headers()
    )
    if preview_html is None:
        logger.error("Lucas RealAuction: PREVIEW failed for %s — skipping date", auction_date)
        return []
    await asyncio.sleep(_INTER_REQ_DELAY)

    listings: list[RawListing] = []
    ts = str(int(datetime.now().timestamp() * 1000))

    c_listings = await _load_area(client, area="C", auction_date=auction_date,
                                  signal_type=signal_type, sale_date=sale_date, today=today, ts=ts)
    listings.extend(c_listings)

    w_listings = await _load_area(client, area="W", auction_date=auction_date,
                                  signal_type=signal_type, sale_date=sale_date, today=today, ts=ts)
    listings.extend(w_listings)

    logger.info(
        "Lucas RealAuction: %s (%s) → %d listings (C=%d W=%d)",
        auction_date, signal_type, len(listings), len(c_listings), len(w_listings),
    )
    return listings


async def _load_area(
    client: httpx.AsyncClient,
    *,
    area: str,
    auction_date: str,
    signal_type: str,
    sale_date: date,
    today: date,
    ts: str,
) -> list[RawListing]:
    listings: list[RawListing] = []
    seen_rlist: set[str] = set()
    page_dir = 0

    while True:
        params: dict[str, Any] = {
            "zaction": "AUCTION",
            "Zmethod": "UPDATE",
            "FNC": "LOAD",
            "AREA": area,
            "PageDir": page_dir,
            "doR": "1",
            "tx": ts,
            "bypassPage": "0",
            "test": "1",
            "_": ts,
        }
        data = await _get_with_retry(
            client, _BASE_URL, params=params,
            headers=_build_json_headers(), as_json=True,
        )
        await asyncio.sleep(_INTER_REQ_DELAY)

        if not data or not isinstance(data, dict):
            break

        rlist: str = (data.get("rlist") or "").strip()
        ret_html: str = data.get("retHTML") or ""

        if not rlist and not ret_html.strip():
            break
        if rlist in seen_rlist:
            break
        seen_rlist.add(rlist)

        for item_fields in _parse_ret_html(ret_html):
            listing = _parse_item(item_fields, signal_type, sale_date)
            if listing is not None:
                listings.append(listing)

        page_dir = 1

    return listings


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class LucasRealAuctionScraper(ListingScraper):
    """Lucas County (OH) sheriff + tax foreclosure listings from RealAuction.

    One scraper, two signal_types:
      - mortgage_foreclosure       → Wednesday sale dates
      - tax_delinquent_foreclosure → Thursday sale dates
    """

    site_name = SITE_NAME

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    async def fetch_and_parse(self) -> list[RawListing]:
        today = today_et()
        all_listings: list[RawListing] = []

        async with httpx.AsyncClient(follow_redirects=True) as client:
            sale_dates = await self._collect_sale_dates(client, today)
            logger.info(
                "Lucas RealAuction: found %d upcoming sale dates across %d month(s)",
                len(sale_dates), _CALENDAR_LOOKAHEAD_MONTHS,
            )
            for auction_date, signal_type in sale_dates:
                listings = await _fetch_roster_for_date(client, auction_date, signal_type, today)
                all_listings.extend(listings)

        logger.info(
            "Lucas RealAuction: total %d listings (%d mortgage, %d tax)",
            len(all_listings),
            sum(1 for l in all_listings if l.signal_type == "mortgage_foreclosure"),
            sum(1 for l in all_listings if l.signal_type == "tax_delinquent_foreclosure"),
        )
        return all_listings

    async def _collect_sale_dates(
        self, client: httpx.AsyncClient, today: date,
    ) -> list[tuple[str, str]]:
        sale_dates: list[tuple[str, str]] = []
        seen: set[str] = set()
        for month_offset in range(_CALENDAR_LOOKAHEAD_MONTHS):
            m = today.month + month_offset
            y = today.year + (m - 1) // 12
            m = ((m - 1) % 12) + 1
            sel_date = f"{{ts '{y:04d}-{m:02d}-01 00:00:00'}}"
            params = {"zaction": "USER", "zmethod": "CALENDAR", "selCalDate": sel_date}
            html_text = await _get_with_retry(
                client, _BASE_URL, params=params, headers=_build_headers()
            )
            await asyncio.sleep(_INTER_REQ_DELAY)
            if html_text is None:
                logger.warning("Lucas RealAuction: calendar fetch failed for %s", sel_date)
                continue
            for day_str, sig_type in _parse_calendar(html_text, today):
                if day_str not in seen:
                    seen.add(day_str)
                    sale_dates.append((day_str, sig_type))
        return sorted(sale_dates, key=lambda t: _parse_date(t[0]) or date.max)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    async def _dry_run() -> None:
        today = today_et()
        print(f"\n=== Lucas RealAuction dry-run (today={today}) ===\n")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            scraper = LucasRealAuctionScraper(dry_run=True)
            sale_dates = await scraper._collect_sale_dates(client, today)
            print(f"Upcoming sale dates ({len(sale_dates)} total):")
            for d, st in sale_dates:
                print(f"  {d}  →  {st}")
            print()

            all_rows: list[RawListing] = []
            for auction_date, signal_type in sale_dates:
                rows = await _fetch_roster_for_date(client, auction_date, signal_type, today)
                all_rows.extend(rows)
                print(f"--- {auction_date} ({signal_type}) → {len(rows)} rows ---")
                for i, r in enumerate(rows[:1]):
                    d = r.model_dump(exclude={"source_site"})
                    d = {k: v for k, v in d.items() if v is not None}
                    print(f"  [{i}] {json.dumps(d, default=str, indent=4)}")

            print(f"\n=== TOTALS ===")
            mort = [r for r in all_rows if r.signal_type == "mortgage_foreclosure"]
            tax = [r for r in all_rows if r.signal_type == "tax_delinquent_foreclosure"]
            print(f"  mortgage_foreclosure:       {len(mort)}")
            print(f"  tax_delinquent_foreclosure: {len(tax)}")
            print(f"  grand total:                {len(all_rows)}")

            # Format-lock spot-check: PARID 1210314 (TF2025-00090) on Thu 06/25/2026
            anchor = "1210314"
            anchor_hits = [r for r in all_rows if r.source_listing_id == anchor]
            if anchor_hits:
                a = anchor_hits[0]
                print(f"\n  FORMAT-LOCK: parcel {anchor} FOUND — case={a.case_number} "
                      f"signal={a.signal_type} sale={a.sale_date} owner address {a.property_address}")
            else:
                print(f"\n  FORMAT-LOCK: parcel {anchor} NOT in sampled window "
                      "(may be past — recon row was 06/25/2026).")

            # ZIP invariant
            bad_zips = [r for r in all_rows if r.property_zip and len(r.property_zip) > 5
                        and "-" not in r.property_zip]
            print(f"  ZIP invariant: {'WARN ' + str(len(bad_zips)) + ' bad' if bad_zips else 'OK'}")

            # Tax signature sanity: every tax row should have Appraised=None and Deposit~=10% of opening
            bad_tax = []
            for r in tax:
                if r.appraised_value_usd is not None:
                    bad_tax.append((r, "appraised not null"))
                elif r.opening_bid_usd and r.deposit_usd:
                    ratio = r.deposit_usd / r.opening_bid_usd
                    if not (0.09 <= ratio <= 0.11):
                        bad_tax.append((r, f"deposit ratio {ratio:.3f} outside 9-11%"))
            if bad_tax:
                print(f"  Tax signature WARN: {len(bad_tax)} rows off-spec")
                for r, reason in bad_tax[:3]:
                    print(f"    {r.case_number} {r.source_listing_id}: {reason}")
            else:
                print(f"  Tax signature OK ({len(tax)} rows)")

            # Mortgage signature: appraised > 0, opening ≈ 2/3 appraised
            bad_mort = []
            for r in mort:
                if not r.appraised_value_usd:
                    bad_mort.append((r, "no appraised"))
                elif r.opening_bid_usd:
                    ratio = r.opening_bid_usd / r.appraised_value_usd
                    if not (0.64 <= ratio <= 0.69):
                        bad_mort.append((r, f"opening ratio {ratio:.3f} outside 64-69% of appraisal"))
            if bad_mort:
                print(f"  Mortgage signature WARN: {len(bad_mort)} rows off-spec")
                for r, reason in bad_mort[:3]:
                    print(f"    {r.case_number} {r.source_listing_id}: {reason}")
            else:
                print(f"  Mortgage signature OK ({len(mort)} rows)")

    asyncio.run(_dry_run())
