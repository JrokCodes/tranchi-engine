"""
Daily Legal News (DLN) scraper — upcoming Cuyahoga sheriff-sale auctions.

Site: https://www.dln.com  (official Cuyahoga County legal-notice journal)
Why this source: the ProWare court docket (sheriff.py) is a PAST-ONLY archive —
it never lists future sale dates. DLN publishes the sale NOTICES weeks ahead, so
it is the only clean public source of UPCOMING tax-deed + mortgage foreclosure
auctions. Public WordPress REST API, no auth, robots allow-all, no EULA.

Two feeds (see Clients/Marc/tranchi/research/dln-field-map.md for the full map):
  - type=delinquent-tax  → tax-deed sales (plaintiff = County Treasurer).
        Clean fields: ppn, case_no, caption, taxes, cost, sale_date.
        NO street address — only a parcel + a legal description in web_export.
        Min bid = taxes + cost. City + re-offer date parsed from web_export.
        Address resolved via the MyPlace parcel-search (fiscal_officer helpers),
        cached into tranchi.parcels so later runs hit the registry first.
  - type=sheriff-sales   → mortgage foreclosures (plaintiff = lender).
        Rich fields: addr, location, parcel_num, appr_value, min_bid, deposit,
        defendant, sale_date, sec_sale_date.

INVARIANT: the DLN `orderby=meta_value&meta_key=sale_date` sort is LEXICAL, not
chronological ("6/3" sorts after "6/10"). Do NOT early-stop on the first past
date — paginate the full feed (bounded by total_pages) and filter
sale_date >= today client-side, or future leads get silently dropped.

Cron: every 3h (registered in run.py listing path).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from typing import Any

import httpx

try:                                   # pool typing only; scraper degrades w/o a pool
    import asyncpg
except Exception:                      # pragma: no cover
    asyncpg = None  # type: ignore

from app.scrapers import fiscal_officer as fo
from app.scrapers._time import today_et
from app.scrapers.base import ListingScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawListing
from app.scrapers.user_agents import random_ua

logger = logging.getLogger(__name__)

SITE_NAME = "Cuyahoga Sheriff Sale (DLN)"

_API_URL = "https://www.dln.com/wp-json/dln/v1/data-table"
_PER_PAGE = 100
_MAX_PAGES = 70                  # safety ceiling; real feeds are ~14 (tax) / ~24 (mortgage) pages
_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0
_INTER_PAGE_DELAY = 0.6          # between API pages
_MYPLACE_CAP = 260               # max per-run MyPlace parcel lookups (covers a 2-date tax window;
                                 # first run pays the cost, later runs hit the cached registry)
_MYPLACE_DELAY = 0.8             # between MyPlace GETs (polite)

# City out of the tax web_export legal description ("Situated in the City of Cleveland,")
_CITY_RE = re.compile(
    r"Situated in the (?:City|Village|Township) of\s+([A-Za-z .'\-]+?)\s*,",
    re.IGNORECASE,
)
# Re-offer date ("...again be offered for sale ... on Wednesday, June 24, 2026,")
_REOFFER_RE = re.compile(
    r"again be offered for sale.*?on\s+[A-Za-z]+day,\s+([A-Z][a-z]+ \d{1,2}, \d{4})",
    re.IGNORECASE | re.DOTALL,
)


# ─────────────────────────────────────────────────────────────────────────────
# Small parse helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_money(raw: str | None) -> float | None:
    """'$6,245.69' / '46667' → float; None/'' → None."""
    if not raw:
        return None
    cleaned = re.sub(r"[^0-9.]", "", str(raw))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_mdy(raw: str | None) -> date | None:
    """'6/10/2026' → date."""
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_longdate(raw: str | None) -> date | None:
    """'June 24, 2026' → date."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%B %d, %Y").date()
    except ValueError:
        return None


def _extract_city(web_export: str | None) -> str | None:
    if not web_export:
        return None
    m = _CITY_RE.search(web_export)
    if not m:
        return None
    city = m.group(1).strip()
    # Guard against the DLN template artifact '[DB PropCounty,CountyName,NO]'
    if not city or "DB Prop" in city or "[" in city:
        return None
    return city


def _extract_reoffer(web_export: str | None) -> date | None:
    if not web_export:
        return None
    m = _REOFFER_RE.search(web_export)
    return _parse_longdate(m.group(1)) if m else None


async def _get_json(client: httpx.AsyncClient, params: dict[str, Any]) -> dict[str, Any] | None:
    """GET the DLN API with retry. Returns parsed JSON dict or None."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(_API_URL, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error("DLN: GET %s failed after %d attempts: %s", params, attempt, exc)
                return None
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Scraper
# ─────────────────────────────────────────────────────────────────────────────

class DLNScraper(ListingScraper):
    """Upcoming Cuyahoga foreclosure auctions (tax + mortgage) from Daily Legal News.

    Optional `pool` enables tax-address resolution against tranchi.parcels +
    on-demand MyPlace lookups (cached back to the registry). Without a pool the
    scraper still works — tax rows fall back to a parcel/city-anchored address.
    """

    site_name = SITE_NAME

    def __init__(self, pool: "asyncpg.Pool | None" = None, dry_run: bool = False) -> None:
        self.pool = pool
        self.dry_run = dry_run

    async def fetch_and_parse(self) -> list[RawListing]:
        today = today_et()
        headers = {
            "User-Agent": random_ua(),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.dln.com/",
            "X-Requested-With": "XMLHttpRequest",
        }

        listings: list[RawListing] = []
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            # ── Mortgage feed (rich, clean) ──────────────────────────────────
            mort_acfs = await self._fetch_feed(
                client, feed_type="sheriff-sales", today=today,
                orderby="meta_value", meta_key="sale_date",
            )
            for acf in mort_acfs:
                rl = self._mortgage_to_listing(acf)
                if rl:
                    listings.append(rl)

            # ── Tax feed (needs address resolution) ──────────────────────────
            tax_acfs = await self._fetch_feed(
                client, feed_type="delinquent-tax", today=today,
                orderby="case_no", meta_key=None,
            )
            addr_map = await self._resolve_tax_addresses(client, tax_acfs)
            for acf in tax_acfs:
                rl = self._tax_to_listing(acf, addr_map)
                if rl:
                    listings.append(rl)

        logger.info("DLN: returning %d upcoming listings (mortgage + tax)", len(listings))
        return listings

    # ── Feed pagination ─────────────────────────────────────────────────────

    async def _fetch_feed(
        self,
        client: httpx.AsyncClient,
        *,
        feed_type: str,
        today: date,
        orderby: str,
        meta_key: str | None,
    ) -> list[dict[str, Any]]:
        """Paginate one DLN feed fully (bounded by total_pages / _MAX_PAGES) and
        return acf dicts whose sale_date >= today. Full scan — see LEXICAL sort
        INVARIANT in the module docstring."""
        out: list[dict[str, Any]] = []
        total_pages = _MAX_PAGES
        page = 1
        scan_complete = False          # only True when we reach the real end of the feed
        while page <= min(total_pages, _MAX_PAGES):
            params: dict[str, Any] = {
                "page": page,
                "per_page": _PER_PAGE,
                "type": feed_type,
                "orderby": orderby,
                "order": "desc",
            }
            if meta_key:
                params["meta_key"] = meta_key
            data = await _get_json(client, params)
            if not data:
                break                  # fetch failure → scan_complete stays False (flagged below)
            total_pages = int(data.get("total_pages") or total_pages)
            rows = data.get("data") or []
            if not rows:
                scan_complete = True
                break
            for rec in rows:
                acf = rec.get("acf") or {}
                sd = _parse_mdy(acf.get("sale_date"))
                # INVARIANT (see module docstring): FILTER here, never break. The feed's
                # sale_date order is LEXICAL ("6/3" sorts after "6/10"), so a past date on
                # this row says NOTHING about later rows — an early break silently drops
                # future leads. A full scan to total_pages is mandatory.
                if sd and sd >= today:
                    out.append(acf)
            if page >= total_pages:
                scan_complete = True
                break
            page += 1
            await asyncio.sleep(_INTER_PAGE_DELAY)
        if page >= _MAX_PAGES:
            logger.warning("DLN %s: hit _MAX_PAGES ceiling (%d) — feed may be undercounted", feed_type, _MAX_PAGES)
        elif not scan_complete:
            # Guards against a regression (early-stop) or a mid-scan fetch failure leaving
            # the upcoming-sales list truncated. Loud because it means missing future leads.
            logger.error(
                "DLN %s: feed scan ended early at page %d/%d — upcoming sales may be "
                "MISSING (LEXICAL sort invariant; full scan required).",
                feed_type, page, total_pages,
            )
        logger.info("DLN %s: %d upcoming rows (sale_date >= %s)", feed_type, len(out), today)
        return out

    # ── Mortgage mapping ─────────────────────────────────────────────────────

    def _mortgage_to_listing(self, acf: dict[str, Any]) -> RawListing | None:
        addr = (acf.get("addr") or "").strip()
        if not addr:
            return None
        return RawListing(
            source_site=SITE_NAME,
            source_listing_id=normalize_parcel_number(acf.get("parcel_num")),
            case_number=(acf.get("case_no") or "").strip() or None,
            signal_type="mortgage_foreclosure",
            property_address=addr,
            property_city=(acf.get("location") or "").strip() or None,
            property_county="Cuyahoga",
            property_state="OH",
            sale_date=_parse_mdy(acf.get("sale_date")),
            sec_sale_date=_parse_mdy(acf.get("sec_sale_date")),
            deposit_usd=_parse_money(acf.get("deposit")),
            opening_bid_usd=_parse_money(acf.get("min_bid")),
            appraised_value_usd=_parse_money(acf.get("appr_value")),
            trustee_name=(acf.get("defendant") or "").strip() or None,
            status="active",
            auction_status="scheduled",
        )

    # ── Tax mapping ───────────────────────────────────────────────────────────

    def _tax_to_listing(self, acf: dict[str, Any], addr_map: dict[str, dict[str, Any]]) -> RawListing | None:
        ppn = normalize_parcel_number(acf.get("ppn"))
        if not ppn:
            return None
        web = acf.get("web_export") or ""
        resolved = addr_map.get(ppn) or {}
        city = resolved.get("property_city") or _extract_city(web)
        situs = resolved.get("situs_address")
        # Address: real situs if resolved, else a parcel-anchored placeholder so
        # prefilter passes. Parcel (source_listing_id) is the true join key.
        property_address = situs or (f"Parcel {ppn}" + (f", {city}" if city else ""))
        min_bid = (_parse_money(acf.get("taxes")) or 0.0) + (_parse_money(acf.get("cost")) or 0.0)
        return RawListing(
            source_site=SITE_NAME,
            source_listing_id=ppn,
            case_number=(acf.get("case_no") or "").strip() or None,
            signal_type="tax_delinquent_foreclosure",
            property_address=property_address,
            property_city=city,
            property_county="Cuyahoga",
            property_state="OH",
            property_zip=resolved.get("property_zip"),
            sale_date=_parse_mdy(acf.get("sale_date")),
            sec_sale_date=_extract_reoffer(web),
            opening_bid_usd=min_bid or None,
            trustee_name=(acf.get("caption") or "").strip() or None,
            status="active",
            auction_status="scheduled",
        )

    # ── Tax address resolution (registry → MyPlace → cache) ──────────────────

    async def _resolve_tax_addresses(
        self, client: httpx.AsyncClient, tax_acfs: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Return {normalized_parcel: {situs_address, property_city, property_zip}}.

        1) batch-read tranchi.parcels for known situs addresses;
        2) for misses, do MyPlace parcel-search (capped, rate-limited);
        3) cache MyPlace hits back into tranchi.parcels so later runs skip the lookup.
        Degrades to {} (placeholders) when no pool is available.
        """
        parcels = sorted({
            p for p in (normalize_parcel_number(a.get("ppn")) for a in tax_acfs) if p
        })
        if not parcels:
            return {}

        addr_map: dict[str, dict[str, Any]] = {}

        # 1) registry batch
        if self.pool is not None:
            try:
                async with self.pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT parcel_number, situs_address, neighborhood
                        FROM tranchi.parcels
                        WHERE parcel_number = ANY($1::text[])
                          AND situs_address IS NOT NULL AND situs_address <> ''
                        """,
                        parcels,
                    )
                for r in rows:
                    addr_map[r["parcel_number"]] = {
                        "situs_address": r["situs_address"],
                        "property_city": None,
                        "property_zip": None,
                    }
            except Exception as exc:
                logger.warning("DLN: parcels registry batch lookup failed: %s", exc)

        # 2) MyPlace for misses (capped)
        misses = [p for p in parcels if p not in addr_map][:_MYPLACE_CAP]
        newly: list[dict[str, Any]] = []
        for parcel in misses:
            try:
                html = await fo._fetch_search_page(
                    client, parcel, fo._ENTIRE_COUNTY_CODE, fo._MODE_PARCEL
                )
                hits = fo._parse_hit_list(html) if html else []
            except Exception as exc:
                logger.debug("DLN: MyPlace lookup failed for %s: %s", parcel, exc)
                hits = []
            if hits:
                h = hits[0]
                rec = {
                    "situs_address": h.get("situs_address") or None,
                    "property_city": h.get("property_city") or None,
                    "property_zip": h.get("property_zip") or None,
                }
                if rec["situs_address"]:
                    addr_map[parcel] = rec
                    newly.append({
                        "parcel_number": parcel,
                        "owner_name": h.get("owner_name"),
                        "situs_address": rec["situs_address"],
                        "property_city": rec["property_city"],
                        "property_zip": rec["property_zip"],
                    })
            await asyncio.sleep(_MYPLACE_DELAY)

        if misses:
            logger.info(
                "DLN: tax address lookup — %d in registry, %d MyPlace attempts, %d resolved",
                len(parcels) - len(misses), len(misses), len(newly),
            )

        # 3) cache MyPlace hits back into the registry (skip on dry-run)
        if self.pool is not None and newly and not self.dry_run:
            try:
                await fo.upsert_parcels(self.pool, newly)
            except Exception as exc:
                logger.warning("DLN: caching %d resolved parcels failed: %s", len(newly), exc)

        return addr_map
