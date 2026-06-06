"""
Tranchi — Live source cross-verify (Track 7).

INVARIANT: Verifiers are market-keyed on property_state.
  OH rows → Cuyahoga verifiers (DLN, cuyahogalandbank.org, Cuyahoga MyPlace).
  TN rows → Shelby verifiers (ePropertyPlus API, Tax Sale CSV, Shelby ArcGIS).
  Never cross markets — a TN parcel must NEVER be checked against a Cuyahoga URL.
  Route ALL market dispatch through row['property_state'].

For each sampled active listing, visit the live source and confirm presence.
The committed-and-cron-able version of "open the source URL and check by hand"
that the /tranchi-verify skill walks Jayden through.

Per-signal logic by market:
  OH probate                       — confirm case_status=OPEN and last_seen recent.
  OH tax_delinquent_foreclosure    — re-hit DLN REST API (delinquent-tax feed).
  OH mortgage_foreclosure          — re-hit DLN REST API (sheriff-sales feed).
  OH land_bank_inventory           — re-fetch cuyahogalandbank.org HTML, parse table.
  OH forfeited_land                — falls to ERROR (same as before; no verifier).
  TN land_bank_inventory (Shelby County Land Bank) — query ePropertyPlus API,
                                    check parcel in FOR SALE + available=Y set.
  TN land_bank_inventory (Memphis MMLBA) — DB-freshness fallback (Airtable-backed,
                                    can't scrape without Playwright; small set).
  TN tax_deed                      — re-fetch TaxSaleExtract.csv, check Alt_Parcel.
  TN mortgage_foreclosure          — DB-freshness fallback (re-fetching per-row
                                    tnforeclosurenotices is too heavy).
  TN probate                       — DB-freshness fallback (ProWare-style; no
                                    deep-link by case_number in TN either).
  Cross-cut registry:
    OH rows → Cuyahoga MyPlace check (owner_name + situs_address).
    TN rows → Shelby ArcGIS parcel layer check (parcel existence + owner token).

Output: per-listing PASS / FAIL / ERROR + a top-line summary.

Run:
  python scripts/playwright_source_check.py --sample 10
  python scripts/playwright_source_check.py --signal probate --limit 20
  python scripts/playwright_source_check.py --parcels 203-28-051 --json
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import ssl
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

_here = Path(__file__).resolve().parent
_backend = _here.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_env = _backend / ".env"
if _env.exists():
    from dotenv import load_dotenv
    load_dotenv(_env)

import asyncpg  # noqa: E402
import httpx  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("source_check")

# ─────────────────────────────────────────────────────────────────────────────
# OH (Cuyahoga) constants — only ever used for rows where property_state='OH'
# ─────────────────────────────────────────────────────────────────────────────
OH_DLN_API = "https://www.dln.com/wp-json/dln/v1/data-table"
OH_DLN_PER_PAGE = 100
OH_DLN_MAX_PAGES = 70
OH_LANDBANK_URL = "https://cuyahogalandbank.org/all-available-properties/"
OH_MYPLACE_BASE = "https://myplace.cuyahogacounty.gov"
# Cuyahoga parcel format: NNN-NN-NNN (e.g. 203-28-051)
OH_PARCEL_RE = re.compile(r"\b(\d{3}-\d{2}-\d{3})\b")

# ─────────────────────────────────────────────────────────────────────────────
# TN (Shelby) constants — only ever used for rows where property_state='TN'
# ─────────────────────────────────────────────────────────────────────────────
# ePropertyPlus API (mirrors shelby_county_landbank.py constants)
TN_EPROPERTYPLUS_API = (
    "https://public-sctn.epropertyplus.com"
    "/landmgmtpub/remote/public/property/getPublishedProperties"
)
TN_EPROPERTYPLUS_PAGE_SIZE = 200
TN_EPROPERTYPLUS_MAX_PAGES = 150
TN_EPROPERTYPLUS_JSON_PARAM = '{"criterias":[]}'
TN_EPROPERTYPLUS_ACTIVE_STATUS = "FOR SALE"
TN_EPROPERTYPLUS_ACTIVE_AVAILABLE = "Y"

# Tax Sale CSV (mirrors shelby_tax_sale.py)
TN_TAX_SALE_CSV_URL = "https://scgpublic.s3.amazonaws.com/TaxSaleExtract.csv"
# Column that carries the canonical 14-char parcel (matches source_listing_id in DB)
TN_TAX_SALE_PARCEL_COL = "Alt_Parcel"

# ArcGIS parcel layer (mirrors shelby_parcels.py)
TN_ARCGIS_QUERY_URL = (
    "https://scgis.shelbycountytn.gov/serverhigh/rest/services/"
    "Parcel/CurrentParcels/MapServer/0/query"
)
TN_ARCGIS_PARCEL_FIELD = "PARCELID"   # spaced canonical from ReGIS (e.g. '072047  00016')
TN_ARCGIS_PAID_FIELD = "PAID"         # compact form (e.g. '07204700016')
TN_ARCGIS_OWNER_FIELD = "OWNER"

# Memphis MMLBA source site name (Airtable-backed; use DB-freshness fallback)
TN_MMLBA_SOURCE_SITE = "Memphis MMLBA"

# ─────────────────────────────────────────────────────────────────────────────
# Shared constants
# ─────────────────────────────────────────────────────────────────────────────
_PROBATE_FRESHNESS_MAX_DAYS = 14  # stored state considered fresh within this window

# Telegram alerting — same shared @intelleq_monitor_bot as quality_audit / audit_scrapers.
_TELEGRAM_TOKEN_PATH = Path("/home/ubuntu/.secrets/tranchi/telegram-bot-token")
_TELEGRAM_CHAT_ID = "8360510944"  # @intelleq_monitor_bot


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _oh_myplace_url(parcel: str) -> str:
    return f"{OH_MYPLACE_BASE}/{_b64(parcel)}?city={_b64('99')}&searchBy={_b64('Parcel')}"


# ─────────────────────────────────────────────────────────────────────────────
# OH: DLN verifier — re-hit the REST API and check case_no presence
# ─────────────────────────────────────────────────────────────────────────────

_DLN_CACHE: dict[str, set[str]] = {}  # feed_type -> set of case_no strings


async def _load_dln_feed(client: httpx.AsyncClient, feed_type: str) -> set[str]:
    """Return set of all case_no strings currently in the DLN upcoming feed.
    Cached per process invocation to avoid re-paginating for each listing.
    """
    if feed_type in _DLN_CACHE:
        return _DLN_CACHE[feed_type]
    cases: set[str] = set()
    for page in range(1, OH_DLN_MAX_PAGES + 1):
        params = {
            "page": page, "per_page": OH_DLN_PER_PAGE,
            "type": feed_type, "orderby": "case_no",
        }
        try:
            r = await client.get(OH_DLN_API, params=params, timeout=20)
            if r.status_code != 200:
                break
            data = r.json()
            rows = data.get("data") or []
            if not rows:
                break
            for rec in rows:
                acf = rec.get("acf") or {}
                cno = (acf.get("case_no") or "").strip()
                if cno:
                    cases.add(cno)
            total_pages = int(data.get("total_pages") or 1)
            if page >= total_pages:
                break
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning("DLN fetch failed (page=%d type=%s): %s", page, feed_type, e)
            break
    _DLN_CACHE[feed_type] = cases
    logger.info("DLN %s cache: %d cases", feed_type, len(cases))
    return cases


async def verify_dln(client: httpx.AsyncClient, row: dict) -> dict:
    """OH-only: check case_number in the DLN feed."""
    case_no = (row.get("case_number") or "").strip()
    if not case_no:
        return {"verdict": "FAIL", "evidence": "no case_number stored"}
    feed_type = "delinquent-tax" if row["signal_type"] == "tax_delinquent_foreclosure" else "sheriff-sales"
    cases = await _load_dln_feed(client, feed_type)
    if case_no in cases:
        return {"verdict": "PASS", "evidence": f"case {case_no} in live DLN feed ({feed_type})"}
    return {"verdict": "FAIL", "evidence": f"case {case_no} NOT in live DLN {feed_type} feed (size={len(cases)})"}


# ─────────────────────────────────────────────────────────────────────────────
# OH: Land Bank verifier — fetch inventory page once, check parcel present
# ─────────────────────────────────────────────────────────────────────────────

_OH_LANDBANK_CACHE: set[str] | None = None
_OH_LANDBANK_LOCK: asyncio.Lock | None = None


async def _load_oh_landbank(client: httpx.AsyncClient) -> set[str]:
    global _OH_LANDBANK_CACHE, _OH_LANDBANK_LOCK
    if _OH_LANDBANK_CACHE is not None:
        return _OH_LANDBANK_CACHE
    if _OH_LANDBANK_LOCK is None:
        _OH_LANDBANK_LOCK = asyncio.Lock()
    async with _OH_LANDBANK_LOCK:
        if _OH_LANDBANK_CACHE is not None:
            return _OH_LANDBANK_CACHE
        parcels: set[str] = set()
        try:
            r = await client.get(OH_LANDBANK_URL, timeout=30)
            if r.status_code == 200:
                for m in OH_PARCEL_RE.finditer(r.text):
                    parcels.add(m.group(1))
        except Exception as e:
            logger.warning("OH Land Bank fetch failed: %s", e)
        _OH_LANDBANK_CACHE = parcels
        logger.info("OH Land Bank cache: %d parcels", len(parcels))
        return parcels


async def verify_oh_landbank(client: httpx.AsyncClient, row: dict) -> dict:
    """OH-only: check parcel against cuyahogalandbank.org inventory."""
    parcel = (row.get("source_listing_id") or "").strip()
    parcels = await _load_oh_landbank(client)
    if parcel in parcels:
        return {"verdict": "PASS", "evidence": f"parcel {parcel} in OH Land Bank inventory"}
    return {"verdict": "FAIL", "evidence": f"parcel {parcel} NOT in OH Land Bank live inventory ({len(parcels)} listed)"}


# ─────────────────────────────────────────────────────────────────────────────
# OH/TN: Probate verifier — stored case_status freshness (no live ProWare visit)
# ─────────────────────────────────────────────────────────────────────────────

# TN probate status strings that indicate an open case (Shelby ProWare format
# differs from Cuyahoga: TN uses compound strings like "OPEN - OPEN", "OPBD - OPEN",
# "REOPEN - REOPEN" for open-class statuses).
_TN_PROBATE_OPEN_STATUSES: frozenset[str] = frozenset({
    "OPEN - OPEN",
    "OPBD - OPEN",      # Open By Default
    "REOPEN - REOPEN",
})


async def verify_probate(row: dict) -> dict:
    """Honest scope note: a live ProWare visit per case is ~3-5s via Playwright form-fill
    (ProWare has no deep-link by case_number — only by internal int id we don't store).
    Instead we trust the always-on read-API gate (case_status NOT IN {closed/disposed/
    terminated/dismissed}) plus the weekly probate_recheck cron, and we confirm the
    stored state is FRESH (last_seen within _PROBATE_FRESHNESS_MAX_DAYS).

    OH: case_status must be exactly 'OPEN'.
    TN: case_status is a compound string ('OPEN - OPEN', 'OPBD - OPEN', 'REOPEN - REOPEN').
    """
    state = (row.get("property_state") or "OH").strip()
    cs = (row.get("case_status") or "").strip()
    last_seen = row.get("last_seen_at")

    if not cs:
        return {"verdict": "FAIL", "evidence": "no case_status stored"}

    # Check open status by market
    if state == "TN":
        is_open = cs in _TN_PROBATE_OPEN_STATUSES
    else:
        is_open = (cs == "OPEN")

    if not is_open:
        return {"verdict": "FAIL", "evidence": f"case_status={cs!r} (not open)"}

    if last_seen:
        age = (datetime.now(timezone.utc) - last_seen).days
        if age > _PROBATE_FRESHNESS_MAX_DAYS:
            return {"verdict": "FAIL", "evidence": f"case_status={cs!r} but stale (last_seen {age}d ago)"}
        return {"verdict": "PASS", "evidence": f"case_status={cs!r}, last_seen {age}d ago (stored-state check, not live re-fetch)"}
    return {"verdict": "PASS", "evidence": f"case_status={cs!r} (no last_seen; stored-state check, not live re-fetch)"}


# ─────────────────────────────────────────────────────────────────────────────
# TN: DB-freshness fallback — used for mortgage_foreclosure + MMLBA land_bank
# ─────────────────────────────────────────────────────────────────────────────

async def verify_tn_freshness_fallback(row: dict, reason: str) -> dict:
    """Stored-state freshness check used for TN signals where live re-fetch is too
    heavy or impossible (mortgage_foreclosure: per-row tnforeclosurenotices is heavy;
    MMLBA: Airtable requires Playwright).

    PASS if the row is active and last_seen_at is within _PROBATE_FRESHNESS_MAX_DAYS.
    Evidence clearly identifies this as a stored-state check, not a live re-fetch.
    """
    last_seen = row.get("last_seen_at")
    source_site = row.get("source_site", "unknown")
    if last_seen:
        age = (datetime.now(timezone.utc) - last_seen).days
        if age > _PROBATE_FRESHNESS_MAX_DAYS:
            return {
                "verdict": "FAIL",
                "evidence": f"[stored-state, not live re-fetch] {reason}: last_seen {age}d ago (>{_PROBATE_FRESHNESS_MAX_DAYS}d)",
            }
        return {
            "verdict": "PASS",
            "evidence": f"[stored-state, not live re-fetch] {reason}: active, last_seen {age}d ago",
        }
    return {
        "verdict": "PASS",
        "evidence": f"[stored-state, not live re-fetch] {reason}: active, no last_seen_at",
    }


# ─────────────────────────────────────────────────────────────────────────────
# TN: ePropertyPlus land bank verifier — Shelby County Land Bank only
# ─────────────────────────────────────────────────────────────────────────────

_TN_LANDBANK_CACHE: set[str] | None = None  # set of live parcelNumbers
_TN_LANDBANK_LOCK: asyncio.Lock | None = None  # populated in _load_tn_landbank


async def _load_tn_landbank(client: httpx.AsyncClient) -> set[str]:
    """Paginate the ePropertyPlus API and collect live (FOR SALE + available=Y) parcelNumbers.
    Mirrors the kill-filter logic in shelby_county_landbank.py.

    Uses a module-level asyncio.Lock to ensure only one coroutine does the full
    pagination — concurrent verify_one calls would otherwise race to populate the
    cache and could stop early on a partial page, missing parcels on later pages.
    """
    global _TN_LANDBANK_CACHE, _TN_LANDBANK_LOCK
    # Fast path: cache already populated
    if _TN_LANDBANK_CACHE is not None:
        return _TN_LANDBANK_CACHE
    # Lazy-init the lock (must be created inside an async context)
    if _TN_LANDBANK_LOCK is None:
        _TN_LANDBANK_LOCK = asyncio.Lock()
    # Hold the lock for the full pagination so concurrent verify_one calls don't
    # race and produce partial caches (pages 60-64 would be missed if two coroutines
    # each paginate independently and the slower one sets the cache first).
    async with _TN_LANDBANK_LOCK:
        # Re-check after acquiring the lock — another coroutine may have just populated it
        if _TN_LANDBANK_CACHE is not None:
            return _TN_LANDBANK_CACHE

        parcels: set[str] = set()
        total_rows_seen = 0
        total_size: int | None = None

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://public-sctn.epropertyplus.com/",
        }

        for page in range(1, TN_EPROPERTYPLUS_MAX_PAGES + 1):
            params = {
                "page": page,
                "limit": TN_EPROPERTYPLUS_PAGE_SIZE,
                "json": TN_EPROPERTYPLUS_JSON_PARAM,
            }
            try:
                r = await client.get(
                    TN_EPROPERTYPLUS_API,
                    params=params,
                    timeout=30,
                    headers=headers,
                )
                if r.status_code != 200:
                    logger.warning("TN LandBank ePropertyPlus: page %d status=%d, stopping", page, r.status_code)
                    break
                data = r.json()
            except Exception as e:
                logger.warning("TN LandBank ePropertyPlus: page %d fetch error: %s", page, e)
                break

            if total_size is None:
                total_size = int(data.get("size") or 0)
                logger.info("TN LandBank ePropertyPlus: API reports %d total inventory rows", total_size)

            api_rows: list[dict] = data.get("rows") or []
            total_rows_seen += len(api_rows)

            for api_row in api_rows:
                # INVARIANT: only FOR SALE + available=Y (mirrors shelby_county_landbank.py)
                if (api_row.get("currentStatus") == TN_EPROPERTYPLUS_ACTIVE_STATUS
                        and api_row.get("available") == TN_EPROPERTYPLUS_ACTIVE_AVAILABLE):
                    pn = (api_row.get("parcelNumber") or "").strip()
                    if pn:
                        parcels.add(pn)

            if not api_rows or len(api_rows) < TN_EPROPERTYPLUS_PAGE_SIZE:
                logger.info("TN LandBank ePropertyPlus: short/empty page at %d — end of inventory", page)
                break
            if total_size and total_rows_seen >= total_size:
                logger.info("TN LandBank ePropertyPlus: consumed all %d rows after page %d", total_size, page)
                break

            # No inter-page delay in the verifier (vs scraper's 1s/page for politeness).
            # The verifier runs once per day; server latency already provides ~5s spacing.
            await asyncio.sleep(0)

        _TN_LANDBANK_CACHE = parcels
        logger.info("TN LandBank ePropertyPlus cache: %d FOR SALE parcels", len(parcels))
        return parcels


async def verify_tn_landbank(client: httpx.AsyncClient, row: dict) -> dict:
    """TN Shelby County Land Bank: check parcel in live ePropertyPlus FOR SALE set."""
    parcel = (row.get("source_listing_id") or "").strip()
    if not parcel:
        return {"verdict": "FAIL", "evidence": "no source_listing_id (parcel) stored"}
    live_parcels = await _load_tn_landbank(client)
    if parcel in live_parcels:
        return {"verdict": "PASS", "evidence": f"parcel {parcel} in live ePropertyPlus FOR SALE set ({len(live_parcels)} listed)"}
    return {"verdict": "FAIL", "evidence": f"parcel {parcel} NOT in live ePropertyPlus FOR SALE set ({len(live_parcels)} listed)"}


# ─────────────────────────────────────────────────────────────────────────────
# TN: Tax Sale CSV verifier
# ─────────────────────────────────────────────────────────────────────────────

_TN_TAX_SALE_CACHE: set[str] | None = None  # set of Alt_Parcel values from live CSV
_TN_TAX_SALE_LOCK: asyncio.Lock | None = None


async def _load_tn_tax_sale_csv(client: httpx.AsyncClient) -> set[str]:
    """Fetch TaxSaleExtract.csv and collect all Alt_Parcel values.
    Mirrors the CSV parsing in shelby_tax_sale.py (Alt_Parcel column, 14-char canonical).
    """
    global _TN_TAX_SALE_CACHE, _TN_TAX_SALE_LOCK
    if _TN_TAX_SALE_CACHE is not None:
        return _TN_TAX_SALE_CACHE
    if _TN_TAX_SALE_LOCK is None:
        _TN_TAX_SALE_LOCK = asyncio.Lock()
    async with _TN_TAX_SALE_LOCK:
        if _TN_TAX_SALE_CACHE is not None:
            return _TN_TAX_SALE_CACHE

        parcels: set[str] = set()
        try:
            r = await client.get(TN_TAX_SALE_CSV_URL, timeout=30, headers={"Accept": "text/csv,*/*"})
            if r.status_code != 200:
                logger.warning("TN Tax Sale CSV: HTTP %d", r.status_code)
                _TN_TAX_SALE_CACHE = parcels
                return parcels
            import csv, io
            reader = csv.DictReader(io.StringIO(r.text))
            if reader.fieldnames is None or TN_TAX_SALE_PARCEL_COL not in reader.fieldnames:
                logger.warning(
                    "TN Tax Sale CSV: '%s' column not found (headers: %s)",
                    TN_TAX_SALE_PARCEL_COL, reader.fieldnames,
                )
                _TN_TAX_SALE_CACHE = parcels
                return parcels
            for csv_row in reader:
                pn = (csv_row.get(TN_TAX_SALE_PARCEL_COL) or "").strip()
                if pn:
                    parcels.add(pn)
        except Exception as e:
            logger.warning("TN Tax Sale CSV: fetch/parse error: %s", e)

        _TN_TAX_SALE_CACHE = parcels
        logger.info("TN Tax Sale CSV cache: %d Alt_Parcel entries", len(parcels))
        return parcels


async def verify_tn_tax_sale(client: httpx.AsyncClient, row: dict) -> dict:
    """TN tax_deed: check parcel (Alt_Parcel / source_listing_id) in live CSV."""
    parcel = (row.get("source_listing_id") or "").strip()
    if not parcel:
        return {"verdict": "FAIL", "evidence": "no source_listing_id (Alt_Parcel) stored"}
    live_parcels = await _load_tn_tax_sale_csv(client)
    if parcel in live_parcels:
        return {"verdict": "PASS", "evidence": f"parcel {parcel} present in live TaxSaleExtract.csv ({len(live_parcels)} rows)"}
    return {"verdict": "FAIL", "evidence": f"parcel {parcel} NOT in live TaxSaleExtract.csv ({len(live_parcels)} rows) — may have paid delinquent taxes"}


# ─────────────────────────────────────────────────────────────────────────────
# OH: MyPlace verifier — Cuyahoga cross-cut registry check
# ─────────────────────────────────────────────────────────────────────────────

async def verify_oh_myplace(client: httpx.AsyncClient, row: dict) -> dict:
    """OH cross-cut: check the stored parcel record against the live Cuyahoga MyPlace page.

    Three signals (any FAIL → FAIL):
      1. parcel number appears on the live page (proves the URL didn't redirect/error)
      2. owner_name leading token (last name) appears on the page
      3. situs_address (if stored) substantially matches the live page (street + number)

    A stricter MyPlace check than just owner-token: catches address drift between
    our stored row and the live county record (e.g., the parcel got re-platted, or
    we recorded a wrong situs at upsert time).
    """
    parcel = (row.get("source_listing_id") or "").strip()
    expected_owner = (row.get("owner_name") or "").strip()
    expected_situs = (row.get("situs_address") or "").strip()
    if not parcel:
        return {"verdict": "FAIL", "evidence": "no parcel"}
    try:
        r = await client.get(_oh_myplace_url(parcel), timeout=20, follow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return {"verdict": "FAIL", "evidence": f"MyPlace status={r.status_code}"}
        html_upper = r.text.upper()

        # 1. Parcel number must appear (else the page redirected away)
        if parcel not in r.text:
            return {"verdict": "FAIL", "evidence": f"parcel {parcel} not on live MyPlace page"}

        signals: list[str] = []

        # 2. Owner token check
        if expected_owner:
            first_token = expected_owner.split(",")[0].strip().split(" ")[0].upper()
            if first_token and first_token in html_upper:
                signals.append(f"owner '{first_token}' ✓")
            else:
                return {"verdict": "FAIL",
                        "evidence": f"parcel ✓ but owner token '{first_token}' missing on live page"}

        # 3. Situs address — compare leading numeric token (house number) + first 4 chars
        #    of street name. Tolerant of suffix variations (St vs Street) and case.
        if expected_situs:
            stoks = re.findall(r"[A-Z0-9]+", expected_situs.upper())
            if stoks:
                num_token = next((t for t in stoks if t.isdigit()), None)
                street_token = next((t for t in stoks if not t.isdigit()), None)
                if num_token and street_token:
                    if num_token in html_upper and street_token[:5] in html_upper:
                        signals.append(f"situs '{num_token} {street_token[:5]}…' ✓")
                    else:
                        return {"verdict": "FAIL",
                                "evidence": f"parcel + owner ✓ but situs '{num_token} {street_token[:8]}' missing"}

        return {"verdict": "PASS", "evidence": "MyPlace live: " + ", ".join(signals or ["parcel ✓"])}
    except Exception as e:
        return {"verdict": "ERROR", "evidence": f"MyPlace fetch error: {str(e)[:80]}"}


# ─────────────────────────────────────────────────────────────────────────────
# TN: Shelby ArcGIS parcel layer cross-cut registry check
# ─────────────────────────────────────────────────────────────────────────────

def _tn_arcgis_ssl_context() -> ssl.SSLContext:
    """Legacy TLS for ReGIS (mirrors shelby_parcels.py _regis_ssl_context)."""
    ctx = ssl.create_default_context()
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4) or 0x4
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _canonical_to_parcelid_spaced(canonical: str) -> str | None:
    """Convert a 14-char canonical TN parcel to the ArcGIS ReGIS PARCELID spaced format.

    14-char canonical structure (from shelby_parcels.py normalize_parcel_number docs):
      MAP(6) + qualifier(1) + GROUP_padded(6) + sub_qualifier(1)
      e.g. '07204700000290' -> MAP='072047', qual='0', group='000029', sub='0'

    ArcGIS PARCELID format (confirmed against live ReGIS data):
      Numeric group: MAP(6) + '  ' (2 spaces) + GROUP_int.zfill(5)
        e.g. '072047  00029'   (13 chars)
      Alpha group:  MAP(6) + ' ' (1 space)  + ALPHA_CHAR + digits.zfill(5)
        e.g. '074037 H00009'  (13 chars)

    Returns None for parcels that cannot be cleanly mapped (caller falls back to FAIL).
    """
    if not canonical or len(canonical) != 14:
        return None
    map_part = canonical[:6]   # e.g. '072047'
    group_6 = canonical[7:13]  # e.g. '000029' or 'H00009'
    first_char = group_6[0]

    if first_char.isdigit():
        # Numeric group — convert 6-char zero-padded to 5-char zero-padded integer
        try:
            group_5 = str(int(group_6)).zfill(5)
        except ValueError:
            return None
        return map_part + "  " + group_5
    else:
        # Alpha-qualified group — MAP + ' ' + alpha_char + trailing_digits.zfill(5)
        alpha_char = first_char
        digits_part = group_6[1:]  # remaining 5 chars, e.g. '00009'
        try:
            digits_5 = str(int(digits_part)).zfill(5)
        except ValueError:
            digits_5 = digits_part[:5]
        return map_part + " " + alpha_char + digits_5


async def verify_tn_arcgis(client: httpx.AsyncClient, row: dict) -> dict:
    """TN cross-cut: query Shelby ArcGIS CurrentParcels layer and confirm parcel exists.

    Converts the stored 14-char canonical parcel to the ArcGIS PARCELID spaced format
    (e.g. '07204700000290' -> '072047  00029') and does an exact PARCELID lookup.

    Checks:
      1. The query returns a feature for this PARCELID (parcel exists in registry).
      2. If owner_name is stored, the first token appears in OWNER (soft check —
         ownership changes don't hard-fail; evidenced but PASS still returned).

    Graceful on ArcGIS errors: returns ERROR (not a crash) with the error as evidence.
    Returns FAIL if the parcel is not in the layer (genuinely missing from ReGIS).
    """
    parcel = (row.get("source_listing_id") or "").strip()
    expected_owner = (row.get("owner_name") or "").strip()
    if not parcel:
        return {"verdict": "FAIL", "evidence": "no source_listing_id stored"}

    parcelid_spaced = _canonical_to_parcelid_spaced(parcel)
    if not parcelid_spaced:
        return {
            "verdict": "ERROR",
            "evidence": f"could not convert canonical parcel {parcel!r} to ArcGIS PARCELID format",
        }

    params = {
        "where": f"PARCELID = '{parcelid_spaced}'",
        "outFields": f"{TN_ARCGIS_PARCEL_FIELD},{TN_ARCGIS_PAID_FIELD},{TN_ARCGIS_OWNER_FIELD}",
        "returnGeometry": "false",
        "resultRecordCount": "3",
        "f": "json",
    }

    try:
        ssl_ctx = _tn_arcgis_ssl_context()
        # Create a short-lived client with the legacy TLS context — can't inject into the
        # shared httpx.AsyncClient which was built without it.
        async with httpx.AsyncClient(
            verify=ssl_ctx,
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
        ) as arcgis_client:
            r = await arcgis_client.get(TN_ARCGIS_QUERY_URL, params=params)
    except Exception as e:
        return {"verdict": "ERROR", "evidence": f"Shelby ArcGIS fetch error: {str(e)[:120]}"}

    try:
        data = r.json()
    except Exception:
        return {"verdict": "ERROR", "evidence": f"Shelby ArcGIS non-JSON response (status={r.status_code})"}

    if "error" in data:
        err_msg = data["error"].get("message", str(data["error"]))[:100]
        return {"verdict": "ERROR", "evidence": f"Shelby ArcGIS API error: {err_msg}"}

    features = data.get("features") or []
    if not features:
        return {
            "verdict": "FAIL",
            "evidence": (
                f"parcel {parcel} (PARCELID '{parcelid_spaced}') "
                "not found in Shelby ArcGIS CurrentParcels"
            ),
        }

    # Parcel exists — optionally check owner token (soft check only)
    signals: list[str] = [f"parcel {parcel} (PARCELID '{parcelid_spaced}') ✓ in Shelby ArcGIS"]
    if expected_owner:
        first_token = expected_owner.split(",")[0].strip().split(" ")[0].upper()
        if first_token:
            live_owner = ""
            for feat in features:
                attrs = feat.get("attributes") or {}
                live_owner = (attrs.get(TN_ARCGIS_OWNER_FIELD) or "").strip().upper()
                if live_owner:
                    break
            if first_token in live_owner:
                signals.append(f"owner token '{first_token}' ✓")
            else:
                # Soft note — ownership changes don't hard-fail the registry check.
                signals.append(
                    f"owner token '{first_token}' not in ArcGIS OWNER='{live_owner[:40]}' "
                    "(ownership may have changed)"
                )

    return {"verdict": "PASS", "evidence": "Shelby ArcGIS: " + ", ".join(signals)}


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration — market-aware dispatch
# ─────────────────────────────────────────────────────────────────────────────

async def verify_one(row: dict, client: httpx.AsyncClient) -> dict:
    sig = row["signal_type"]
    parcel = row["source_listing_id"]
    state = (row.get("property_state") or "OH").strip()
    source_site = (row.get("source_site") or "").strip()

    # ── Source-specific verifier (market-keyed on property_state) ─────────────
    if state == "OH":
        if sig == "probate":
            src_result = await verify_probate(row)
        elif sig in ("tax_delinquent_foreclosure", "mortgage_foreclosure"):
            src_result = await verify_dln(client, row)
        elif sig == "land_bank_inventory":
            src_result = await verify_oh_landbank(client, row)
        else:
            # OH forfeited_land and any unknown OH signal — preserve existing ERROR behavior
            src_result = {"verdict": "ERROR", "evidence": f"no OH verifier for signal_type={sig}"}

    elif state == "TN":
        if sig == "land_bank_inventory":
            if source_site == TN_MMLBA_SOURCE_SITE:
                # MMLBA is Airtable-backed — can't scrape without Playwright in this script.
                # DB-freshness fallback: the scraper runs daily and the view is pre-filtered
                # to "Available" only; a fresh last_seen_at means the scraper confirmed presence.
                src_result = await verify_tn_freshness_fallback(
                    row,
                    "Memphis MMLBA (Airtable-backed; live re-scrape requires Playwright)",
                )
            else:
                # Shelby County Land Bank — ePropertyPlus API
                src_result = await verify_tn_landbank(client, row)
        elif sig == "tax_deed":
            src_result = await verify_tn_tax_sale(client, row)
        elif sig == "mortgage_foreclosure":
            # Re-fetching tnforeclosurenotices per-row is too heavy; use DB-freshness.
            src_result = await verify_tn_freshness_fallback(
                row,
                "Shelby County Foreclosure (live re-fetch per-row too heavy; stored-state check)",
            )
        elif sig == "probate":
            # TN probate uses a different case_status format — verify_probate handles both.
            src_result = await verify_probate(row)
        else:
            src_result = {"verdict": "ERROR", "evidence": f"no TN verifier for signal_type={sig}"}

    else:
        src_result = {"verdict": "ERROR", "evidence": f"unknown property_state={state!r}"}

    # ── Cross-cut registry check (market-keyed) ────────────────────────────────
    if state == "OH":
        reg_result = await verify_oh_myplace(client, row)
        reg_label = "myplace"
    elif state == "TN":
        reg_result = await verify_tn_arcgis(client, row)
        reg_label = "arcgis"
    else:
        reg_result = {"verdict": "ERROR", "evidence": f"unknown property_state={state!r}"}
        reg_label = "registry"

    # Combined verdict: PASS only if BOTH pass
    combined = "PASS"
    if src_result["verdict"] != "PASS" or reg_result["verdict"] != "PASS":
        if src_result["verdict"] == "ERROR" or reg_result["verdict"] == "ERROR":
            combined = "ERROR"
        else:
            combined = "FAIL"

    return {
        "id": str(row["id"]),
        "signal_type": sig,
        "property_state": state,
        "source_site": source_site,
        "parcel": parcel,
        "address": f"{row['property_address']}, {row['property_city']}",
        "case_number": row.get("case_number"),
        "source_verdict": src_result["verdict"],
        "source_evidence": src_result["evidence"],
        f"{reg_label}_verdict": reg_result["verdict"],
        f"{reg_label}_evidence": reg_result["evidence"],
        # Keep 'myplace_verdict'/'myplace_evidence' keys for backward-compat in Telegram formatter
        "myplace_verdict": reg_result["verdict"],
        "myplace_evidence": reg_result["evidence"],
        "combined": combined,
    }


async def _sample_rows(conn: asyncpg.Connection, *, sample: int, signal: str | None,
                       limit: int | None, parcels: list[str] | None) -> list[dict]:
    if parcels:
        rows = await conn.fetch(
            """
            SELECT l.id, l.signal_type, l.source_listing_id, l.property_address, l.property_city,
                   l.property_state, l.source_site,
                   l.case_number, l.case_status, l.last_seen_at,
                   p.owner_name, p.situs_address
            FROM tranchi.listings l LEFT JOIN tranchi.parcels p ON p.parcel_number = l.source_listing_id
            WHERE l.source_listing_id = ANY($1) AND l.status='active' AND l.duplicate_of IS NULL
            """,
            parcels,
        )
        return [dict(r) for r in rows]
    where = "l.status='active' AND l.duplicate_of IS NULL"
    params: list = []
    if signal:
        where += " AND l.signal_type = $1"
        params.append(signal)
    n = limit or sample or 10
    rows = await conn.fetch(
        f"""
        SELECT l.id, l.signal_type, l.source_listing_id, l.property_address, l.property_city,
               l.property_state, l.source_site,
               l.case_number, l.case_status, l.last_seen_at,
               p.owner_name, p.situs_address
        FROM tranchi.listings l LEFT JOIN tranchi.parcels p ON p.parcel_number = l.source_listing_id
        WHERE {where}
        ORDER BY random() LIMIT {int(n)}
        """,
        *params,
    )
    return [dict(r) for r in rows]


async def run(args) -> int:
    url = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(url)
    try:
        rows = await _sample_rows(
            conn, sample=args.sample, signal=args.signal, limit=args.limit,
            parcels=args.parcels,
        )
        if not rows:
            print("No rows selected.")
            return 0

        t0 = time.time()
        async with httpx.AsyncClient() as client:
            # Concurrent (httpx + DB-stored probate are both quick)
            sem = asyncio.Semaphore(args.concurrency)

            async def _bounded(r):
                async with sem:
                    return await verify_one(r, client)
            results = await asyncio.gather(*[_bounded(r) for r in rows])
        elapsed = time.time() - t0

        if args.json:
            print(json.dumps({"elapsed_s": round(elapsed, 1), "results": results}, indent=2, default=str))
            return 0

        # Human-readable
        print(f"\n=== PLAYWRIGHT-CROSS-VERIFY — {len(results)} listings ({elapsed:.1f}s) ===\n")
        counts = {"PASS": 0, "FAIL": 0, "ERROR": 0}
        for i, r in enumerate(results, 1):
            counts[r["combined"]] = counts.get(r["combined"], 0) + 1
            state_tag = r.get("property_state", "??")
            print(f"[{i:>2}] {r['combined']:<5} [{state_tag}] {r['signal_type']:<26} {r['address']} ({r['parcel']})")
            print(f"      source:   [{r['source_verdict']}] {r['source_evidence']}")
            print(f"      registry: [{r['myplace_verdict']}] {r['myplace_evidence']}")
        print("\n" + "=" * 70)
        print(f"  PASS={counts['PASS']}  FAIL={counts['FAIL']}  ERROR={counts['ERROR']}  (of {len(results)})")
        if counts["FAIL"]:
            print("\n  Failing listing IDs (for spot-check / dispute):")
            for r in results:
                if r["combined"] == "FAIL":
                    print(f"    {r['id']} -- {r['address']} ({r['parcel']}) — {r['source_evidence']}")
        print("=" * 70 + "\n")

        # Telegram alert on any FAIL/ERROR (off if --no-alert)
        if not args.no_alert:
            msg = _format_telegram_alert(results, elapsed)
            if msg:
                _send_telegram(msg)
        return 0
    finally:
        await conn.close()


def _send_telegram(message: str) -> None:
    """Best-effort Telegram alert. Same graceful-no-op pattern as quality_audit.py."""
    if not _TELEGRAM_TOKEN_PATH.exists():
        logger.info("Telegram token not at %s — alert suppressed.", _TELEGRAM_TOKEN_PATH)
        return
    try:
        token = _TELEGRAM_TOKEN_PATH.read_text().strip()
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": _TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
        r.raise_for_status()
        logger.info("Telegram alert sent.")
    except Exception as exc:
        logger.error("Telegram send failed (non-fatal): %s", exc)


def _format_telegram_alert(results: list[dict], elapsed: float) -> str:
    fails = [r for r in results if r["combined"] == "FAIL"]
    errs = [r for r in results if r["combined"] == "ERROR"]
    if not fails and not errs:
        return ""
    lines = [
        "*Tranchi source-check — FAIL(s) detected*",
        f"_{len(fails)} FAIL, {len(errs)} ERROR of {len(results)} ({elapsed:.1f}s)_",
        "",
    ]
    for r in (fails + errs)[:12]:
        state_tag = r.get("property_state", "??")
        lines.append(f"• `[{state_tag}] {r['signal_type']}` {r['address']} ({r['parcel']})")
        lines.append(f"    {r['source_evidence']}")
        if r['myplace_verdict'] != 'PASS':
            lines.append(f"    Registry: {r['myplace_evidence']}")
    if len(fails) + len(errs) > 12:
        lines.append(f"...and {len(fails) + len(errs) - 12} more")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Tranchi live-source cross-verify")
    ap.add_argument("--sample", type=int, default=10)
    ap.add_argument("--signal", type=str, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--parcels", nargs="*", default=None)
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-alert", action="store_true", help="Skip Telegram alert on FAIL")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
