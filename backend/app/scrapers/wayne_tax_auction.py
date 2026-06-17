"""
Wayne County (MI / Detroit) Treasurer tax-foreclosure AUCTION scraper — SEASONAL, ships DORMANT.

SOURCE: Wayne County Treasurer in-house auction platform (waynecountytreasurermi.com).
  Judicial tax-DEED sale (MCL 211.78+): ~3yr delinquency → Circuit Court Judgment of
  Foreclosure → the Treasurer sells a CLEAN fee-title deed. NO post-sale redemption (unlike
  MI mortgage foreclosure) — so a past sale_date here is simply expired (run.py:_mark_expired,
  no redemption carve-out). Post-Rafaeli surplus claims (MCL 211.78t) don't affect buyer title.

SEASONAL — SHIPS DORMANT:
  The portal serves ONE auction at a time. As of 2026-06 it still serves the CLOSED Oct-2025
  round (every row STATUS_CD='CL'/STATUS='CLOSED', AUC_STATUS_CD != 'OP'). The 2026 catalog
  publishes ~mid-Aug 2026 (Sept main auction = thousands; Oct no-reserve round). We GATE
  ingestion on GetAuctionInfo.AUC_STATUS_CD == 'OP' AND now < AB_END_DT: until the window opens
  the scraper runs every cycle and writes ZERO rows (never ingesting the stale closed round),
  then SELF-ACTIVATES when the catalog opens — no code change, no redeploy.

INVARIANT (read before editing):
  - site_name = "Wayne County Treasurer's Auction" — wired into market_config 'wayne'
    source_sites + staleness_policies (FULL_RESCAN, year-versioned) + source_meta.
  - oReturnObject is DOUBLE-JSON-ENCODED: json.loads(json.loads(resp)['oReturnObject']).
  - ITEMCOUNT / count strings are SPACE-PADDED ('       608') — .strip() before int().
  - X-API-KEY: 'your-secret-api-key' is a LITERAL placeholder baked into the client JS — it is
    the working key, NOT a credential to protect.
  - REMOVAL is EXPLICIT, not silent: a redeemed/withdrawn parcel keeps its row with
    STATUS_CD='RM' (list) / STATUS='REMOVED' (detail). We DROP RM rows (only STATUS_CD='CL'
    becomes a listing); the pto.waynecounty.com live cross-check is the verify-tool authority.
  - PARCEL: AI_PARCEL_ID carries the SIGNIFICANT trailing '.'/'-'/'.NNN[alpha]' — pass VERBATIM
    to normalize_parcel_number (Wayne branch). Bundle pseudo-ids 'B-KELLY' have no parcel → skip
    as listings (or keep address-only → REVIEW). sale_date = AB_END_DT (batch close).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from typing import Any

import httpx

from app.scrapers.base import ListingScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawListing
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

SITE_NAME = "Wayne County Treasurer's Auction"
SIGNAL_TYPE = "tax_delinquent_foreclosure"  # → "Tax Foreclosure" in _DIM_MAP

_BASE = "https://waynecountytreasurermi.com/api"
_SEARCH_URL = f"{_BASE}/General/SearchItems"
_CITYCOUNT_URL = f"{_BASE}/General/GetCityItemsCount"
_AUCTIONINFO_URL = f"{_BASE}/General/GetAuctionInfo"
_API_KEY = "your-secret-api-key"  # literal placeholder shipped in /js/config.js (not a secret)

_TIMEOUT = 40.0
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0

# TLS: waynecountytreasurermi.com sends an INCOMPLETE certificate chain (omits the Sectigo
# 'Public Server Authentication CA DV R36' intermediate), so openssl/httpx raise
# CERTIFICATE_VERIFY_FAILED ('unable to verify the first certificate') — a SERVER
# misconfiguration browsers hide via AIA fetching. The data is PUBLIC, read-only auction
# listings (no secrets transmitted), so we skip verification for THIS host only. If the
# server ever fixes its chain, flip back to verify=True. (Mirrors the gov-site TLS-workaround
# precedent — Shelby ReGIS legacy-TLS.)
_VERIFY_TLS = False


def _headers() -> dict[str, str]:
    h = default_headers()
    h.update({"Content-Type": "application/json", "X-API-KEY": _API_KEY, "accept": "application/json"})
    return h


def _double_decode(data: dict[str, Any]) -> Any:
    """Envelope {bSuccess, sErrorInfo, oReturnObject}; oReturnObject is a JSON-encoded STRING."""
    obj = data.get("oReturnObject")
    if obj is None:
        return None
    if isinstance(obj, str):
        try:
            return json.loads(obj)
        except (ValueError, TypeError):
            return None
    return obj  # already decoded (defensive)


def _parse_ms_or_iso(v: Any) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return datetime.fromtimestamp(v / 1000).date()
        except (ValueError, OverflowError, OSError):
            return None
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s[:19].replace("Z", "")).date()
    except ValueError:
        return None


def _to_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(str(v).strip().replace(",", ""))
    except (ValueError, TypeError):
        return None


async def _post(client: httpx.AsyncClient, url: str, body: dict[str, Any]) -> dict[str, Any] | None:
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.post(url, json=body, headers=_headers(), timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error("WayneTaxAuction: POST %s failed after %d attempts: %s", url, attempt, exc)
                return None
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return None


async def _auction_is_open(client: httpx.AsyncClient) -> tuple[bool, date | None]:
    """Return (is_open, auc_end_date). The DORMANT gate.

    GOTCHA: AUC_STATUS_CD is PERMANENTLY 'OP' even on a long-ended auction — the portal
    still serves the closed Oct-2025 round (AUC_END_DT 2025-10-23) with AUC_STATUS_CD='OP'.
    So the status code is useless as a liveness signal. The real gate is the DATE WINDOW:
    the auction is live only while today <= AUC_END_DT. When the 2026 catalog publishes
    (~mid-Aug), GetAuctionInfo returns the new auction with a future AUC_END_DT and this
    flips true automatically — no code change. We ingest as soon as an upcoming auction's
    end date is in the future (gives lead time on the catalog before bidding closes).
    """
    data = await _post(client, _AUCTIONINFO_URL, {})
    if not data:
        return False, None
    info = _double_decode(data)
    if isinstance(info, list):
        info = info[0] if info else {}
    if not isinstance(info, dict):
        return False, None
    status = str(info.get("AUC_STATUS_CD") or "").strip().upper()
    auc_end = _parse_ms_or_iso(info.get("AUC_END_DT") or info.get("AB_END_DT"))
    name = info.get("AUC_NAME")
    today = datetime.now().date()
    # Date window is the authority; status=='OP' is a necessary sanity check (it's always OP).
    is_open = status == "OP" and auc_end is not None and today <= auc_end
    logger.info("WayneTaxAuction: GetAuctionInfo name=%r AUC_STATUS_CD=%r AUC_END_DT=%s → open=%s",
                name, status, auc_end, is_open)
    return is_open, auc_end


async def _fetch_city_items(client: httpx.AsyncClient, city: str) -> list[dict[str, Any]]:
    data = await _post(client, _SEARCH_URL, {
        "ParcelID": "", "AuctionItemID": "", "StreetNbr": "",
        "StreetAddress": "", "City": city, "Zip": "",
    })
    if not data:
        return []
    rows = _double_decode(data)
    if isinstance(rows, dict):
        rows = rows.get("Items") or rows.get("items") or []
    return rows if isinstance(rows, list) else []


def _row_to_listing(r: dict[str, Any]) -> RawListing | None:
    # Only active rows become listings; RM = redeemed/withdrawn (explicit removal).
    status_cd = str(r.get("STATUS_CD") or "").strip().upper()
    if status_cd == "RM":
        return None
    raw_parcel = str(r.get("AI_PARCEL_ID") or "").strip()
    # Bundle pseudo-ids ('B-KELLY') are not real parcels — skip (no spine join).
    parcel = None
    if raw_parcel and not raw_parcel.upper().startswith("B-"):
        parcel = normalize_parcel_number(raw_parcel)  # VERBATIM in (keep trailing '.'/'-')
    street = str(r.get("AI_ADDR") or "").strip()
    if not street:
        return None
    city = str(r.get("AI_CITY") or "").strip() or "Detroit"
    zip_cd = str(r.get("ZIP_CD") or "").strip() or None
    sale_date = _parse_ms_or_iso(r.get("AB_END_DT") or r.get("EXTD_BIDDING_END_DT"))
    return RawListing(
        source_site=SITE_NAME,
        source_listing_id=parcel,
        case_number=str(r.get("AI_ID") or "").strip() or None,
        signal_type=SIGNAL_TYPE,
        property_address=street,
        property_city=city.title(),
        property_county="Wayne",
        property_state="MI",
        property_zip=zip_cd,
        sale_date=sale_date,
        opening_bid_usd=_to_float(r.get("AI_MIN_BID_AMT")),
        status="active",
        auction_status="scheduled",
    )


class WayneTaxAuctionScraper(ListingScraper):
    """Wayne County Treasurer tax-foreclosure auction — ships DORMANT (gated on an OPEN window).

    `ignore_gate=True` bypasses the GetAuctionInfo gate for VALIDATION ONLY (parse-proof the
    closed round). Production always keeps the gate so the stale closed round is never ingested.
    """

    site_name = SITE_NAME

    def __init__(self, pool: Any = None, dry_run: bool = False, ignore_gate: bool = False) -> None:
        self.pool = pool
        self.dry_run = dry_run
        self.ignore_gate = ignore_gate

    async def fetch_and_parse(self) -> list[RawListing]:
        async with httpx.AsyncClient(headers=_headers(), follow_redirects=True,
                                     verify=_VERIFY_TLS) as client:
            if not self.ignore_gate:
                is_open, _ = await _auction_is_open(client)
                if not is_open:
                    logger.info("WayneTaxAuction: no OPEN auction window — DORMANT, returning 0 rows.")
                    return []
            # Active window (or validation override): iterate cities and collect CL items.
            cities = await self._city_list(client)
            listings: list[RawListing] = []
            seen: set[str] = set()
            for city in cities:
                for r in await _fetch_city_items(client, city):
                    listing = _row_to_listing(r)
                    if listing is None:
                        continue
                    key = listing.case_number or f"{listing.property_address}|{listing.sale_date}"
                    if key in seen:
                        continue
                    seen.add(key)
                    listings.append(listing)
        logger.info("WayneTaxAuction: returning %d active (CL) listings across %d cities",
                    len(listings), len(cities))
        return listings

    async def _city_list(self, client: httpx.AsyncClient) -> list[str]:
        """Cities with auction items (GetCityItemsCount); fall back to DETROIT only."""
        data = await _post(client, _CITYCOUNT_URL, {})
        rows = _double_decode(data) if data else None
        cities: list[str] = []
        if isinstance(rows, list):
            for r in rows:
                name = str(r.get("CITY") or r.get("AI_CITY") or r.get("City") or "").strip()
                if name:
                    cities.append(name)
        return cities or ["DETROIT"]


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run proof
# ─────────────────────────────────────────────────────────────────────────────

async def _dry_run() -> None:
    print("\n=== Wayne Tax Auction — DORMANT gate proof ===\n")
    gated = await WayneTaxAuctionScraper().fetch_and_parse()
    print(f"Production (gated) returned {len(gated)} listings (expect 0 while dormant)\n")

    print("=== Parser proof (ignore_gate=True — parses the CLOSED Oct-2025 round) ===\n")
    forced = await WayneTaxAuctionScraper(ignore_gate=True).fetch_and_parse()
    print(f"Parsed {len(forced)} CL rows (double-decode + RM-drop + parcel verbatim)\n")
    for l in forced[:10]:
        print(f"  parcel={l.source_listing_id!r:16} bid={l.opening_bid_usd} sale={l.sale_date} "
              f"addr={l.property_address!r} city={l.property_city} zip={l.property_zip}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_dry_run())
