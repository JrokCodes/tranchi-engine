"""
Lucas County (OH / Toledo) Tax-Delinquent SIGNAL — writes to tranchi.signals.

THE 5K LEVER — WHY THIS IS A DEFERRED PARTIAL (NOT THE FULL ~19K LIST):

  The original target was the Lucas County Auditor "Delinquent Land Tax List"
  (~19K parcels published annually on Column / ohio.column.us — the published
  newspaper of record). Engine-box recon (2026-06-21) confirmed:

    1. ohio.column.us is a React SPA whose `/api/v1/public/notices` route
       returns the React shell, not JSON; the data is fetched client-side
       behind a Cloudflare-protected, JWT-authed `api.column.us` backend
       (see `cf-2fa-verify` token in the page <meta>). Programmatic scrape
       requires either (a) Playwright + Cloudflare-challenge resolution, or
       (b) a paid Column data partnership. Neither is in scope for S3.

    2. Neither lucascountyohioauditor.gov nor co.lucas.oh.us/treasurer
       publishes the consolidated list as a downloadable PDF or CSV. The
       "annual statutory delinquent-list PDF" path the build brief offered
       does not exist as a standalone artifact — the legally required
       publication lives ON Column.

  THIS BUILD'S FALLBACK: parse the Toledo Legal News weekly "Tax Foreclosure
  Notices" article (a Treasurer-filed-cases consolidation, ~4 cases/week ≈ 200
  parcels/year). This is *downstream* of the 19K pre-distress list — these are
  parcels the Treasurer has actually filed against in Common Pleas Court —
  i.e. the next-stage signal, slightly later in the funnel than the Column
  19K but still earlier than the RealAuction-Thursday sheriff sale. It gives
  Lucas a real `tax_delinquent` signal channel from day one.

  TODO (G3): integrate ohio.column.us via Playwright once Cloudflare access
  is resolved — this scraper's parse logic is decoupled from the source
  fetcher so swapping fetch implementations does not change emit shape.

SHIPS DISABLED — `distress_lead_types` row for 'tax_delinquent' is NOT
inserted for Lucas in this branch. The market_config gate is wired so the
moment the Column source lands and we have enough volume, flipping the row
to enabled is a one-line change. Today the signals still write to
tranchi.signals (idempotent monthly tape pattern) so the catalog dedup +
verification surfaces them as cross-checks against RealAuction-Thu rows.

EMIT SHAPE — matches the Summit/Shelby convention:
  signal_type    = 'tax_delinquent'
  source         = 'lucas_tln_tfn'
  observed_at    = the article's datePublished (one stable timestamp per
                   article; idempotent re-runs UPDATE in place)
  payload keys   = { delq_amount, luc, owner, taxbill_address,
                     property_address, case_number }
  luc            = backfilled from the AREIS spine via a single batch query
                   per run (free; AREIS L38 is open). The market_config
                   gate_sql looks for `payload->>'luc' ~ '^5'`, so the LUC
                   MUST be present at emit time.

PARSE FORMAT (verified live 2026-06-21 on article _38f3ca2a_):
  Each case block in the consolidated article looks roughly like:

    TF25-00180 vs. The Unknown Spouse, Heirs ... NOTICE To Defendants:
    Vennrell A. Dowell ... (last known addresses: 1434 Lincoln Ave,
    Toledo, OH 43607 & 959 Woodland Ave ...), their Unknown Spouses ...
    Impositions $21,554.27 levied upon PPN 02-14864, Property Address:
    1434 Lincoln Ave, Toledo, OH 43607.
    06-11, 06-18, 06-25-2026 3Thu

  Each block carries:
    - case#: TFxx-xxxxx
    - defendants (free-text)
    - last-known mailing address(es) — used as taxbill_address
    - Impositions $X — the certified delinquent balance (delq_amount)
    - PPN DD-DDDDD — normalize_parcel_lucas → 7-digit PARID
    - Property Address — situs (canonical)
    - publication trailer (3 weekly insertions ending in '<sale-day>Thu')

  Multi-parcel cases (multiple "PPN" lines) emit one signal per parcel,
  sharing the same case_number + observed_at.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.scrapers.arcgis_client import query_features
from app.scrapers.base import SignalScraper
from app.scrapers.db import normalize_parcel_for_market
from app.scrapers.models import RawSignal
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

SITE_NAME = "Lucas Tax Delinquent (Lead)"
SIGNAL_SOURCE = "lucas_tln_tfn"
_MARKET = "lucas"

_TLN_BASE = "https://www.toledolegalnews.com"
_TLN_FORECLOSURES_INDEX = f"{_TLN_BASE}/legal_notices/foreclosures/"

# AREIS spine — used to backfill LUC on each parcel (gate_sql checks payload->>'luc')
_AREIS_PARCEL_LAYER = (
    "https://lcaudgis.co.lucas.oh.us/gisaudserver/rest/services/"
    "AREIS_Web_Map_MIL1/MapServer/38"
)

_TIMEOUT = 30.0
_INTER_REQ_DELAY = 2.5
_RETRY_ATTEMPTS = 4
_RETRY_BASE_DELAY = 6.0

# A case starts with `TFxx-xxxxx` (Treasurer-initiated tax foreclosure case#).
_TF_CASE_RE = re.compile(r"\bTF\d{2}-\d{5}\b")
_PPN_RE = re.compile(r"\bPPN\s*(\d{2}-\d{5})\b", re.I)
_PARCEL_DISPLAY_RE = re.compile(r"\b\d{2}-\d{5}\b")
_IMPOSITIONS_RE = re.compile(r"Impositions\s*\$?([\d,]+(?:\.\d{2})?)", re.I)
_PROP_ADDR_RE = re.compile(
    r"Property\s+Address[:\s]+([^.]+?(?:OH|Ohio)\s+\d{5})", re.I,
)
_LAST_KNOWN_RE = re.compile(
    r"last\s+known\s+addresses?[:\s]+\(?([^)]*?(?:OH|Ohio)[^)]*?\d{5}[^)]*?)\)?",
    re.I | re.DOTALL,
)
_DEFENDANTS_RE = re.compile(r"NOTICE\s+To\s+Defendants?[:\s]+(.+?)(?:last\s+known)", re.I | re.DOTALL)
_TFN_TITLE_RE = re.compile(r"Tax\s+Foreclosure\s+Notices", re.I)
_ARTICLE_HREF_RE = re.compile(r"^/legal_notices/foreclosures/[^/]+/article_[0-9a-f-]+\.html$", re.I)


def _clean_amount(raw: str | None) -> str:
    if not raw:
        return ""
    cleaned = raw.strip().lstrip("$").replace(",", "")
    return cleaned if cleaned else ""


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


async def _get_with_retry(client: httpx.AsyncClient, url: str) -> str | None:
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url, timeout=_TIMEOUT)
            if resp.status_code == 429:
                if attempt == _RETRY_ATTEMPTS:
                    logger.error("LucasDelinquentTax: 429 on %s after %d retries", url, attempt)
                    return None
                wait = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning("LucasDelinquentTax: 429 on %s — sleeping %.1fs", url, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error("LucasDelinquentTax GET %s failed: %s", url, exc)
                return None
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return None


def _split_into_cases(body: str) -> list[tuple[str, str]]:
    """Split the consolidated article body into (case_number, body_chunk) pairs.

    Each TF case# acts as the split anchor — the chunk runs from one case# up to
    (but not including) the next TF case#. Anything before the first TF case# is
    the article preamble and gets discarded.
    """
    matches = list(_TF_CASE_RE.finditer(body))
    if not matches:
        return []
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        case_no = m.group(0)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out.append((case_no, body[start:end]))
    return out


def _parse_case_block(case_no: str, chunk: str, observed_at: datetime,
                      article_url: str) -> list[RawSignal]:
    """Extract one signal per PPN in a single case block."""
    impose_m = _IMPOSITIONS_RE.search(chunk)
    delq_amount = _clean_amount(impose_m.group(1)) if impose_m else ""

    addr_m = _PROP_ADDR_RE.search(chunk)
    property_address = _norm_ws(addr_m.group(1)) if addr_m else ""

    last_known_m = _LAST_KNOWN_RE.search(chunk)
    taxbill_address = _norm_ws(last_known_m.group(1)) if last_known_m else ""

    def_m = _DEFENDANTS_RE.search(chunk)
    defendants = _norm_ws(def_m.group(1)) if def_m else ""
    # Trim trailing punctuation/whitespace including the open paren of "(last known addresses: ...".
    defendants = re.sub(r"[\s,;(]+$", "", defendants)

    ppns: list[str] = []
    for m in _PPN_RE.finditer(chunk):
        ppn_raw = m.group(1)
        canon = normalize_parcel_for_market(ppn_raw, _MARKET)
        if canon and canon not in ppns:
            ppns.append(canon)

    out: list[RawSignal] = []
    for parcel in ppns:
        payload: dict[str, Any] = {
            "delq_amount": delq_amount,
            "luc": "",  # backfilled after batch AREIS lookup
            "owner": defendants,
            "taxbill_address": taxbill_address,
            "property_address": property_address,
            "case_number": case_no,
            "source_url": article_url,
        }
        out.append(RawSignal(
            parcel_number=parcel,
            signal_type="tax_delinquent",
            source=SIGNAL_SOURCE,
            observed_at=observed_at,
            confidence=1.0,
            payload=payload,
        ))
    return out


async def _backfill_luc_from_areis(parcels: list[str]) -> dict[str, str]:
    """One batch query against AREIS L38 — returns {parcel_number: luc_code}.

    LUC must be present in the payload for the market_config gate_sql to fire
    (`payload->>'luc' ~ '^5'`). The batch is free (open ArcGIS REST) and fast
    enough that we don't bother chunking unless the parcel list grows beyond
    a few hundred (well above the ~200/yr ceiling of this fallback fetcher).
    """
    if not parcels:
        return {}
    # ArcGIS query string: PARID IN ('1210314','1041297',...)
    quoted = ",".join(f"'{p}'" for p in parcels[:1000])
    out: dict[str, str] = {}
    try:
        async for batch in query_features(
            _AREIS_PARCEL_LAYER, where=f"PARID IN ({quoted})",
            out_fields="PARID,LUC", batch_size=2000,
        ):
            for attrs in batch:
                pid = (attrs.get("PARID") or "").strip()
                pid_canon = normalize_parcel_for_market(pid, _MARKET)
                if pid_canon:
                    out[pid_canon] = (attrs.get("LUC") or "").strip()
    except Exception as exc:
        logger.warning("LucasDelinquentTax: LUC backfill failed (%s) — emitting blank LUCs", exc)
    return out


class LucasDelinquentTaxScraper(SignalScraper):
    """Lucas tax-delinquent SIGNAL via TLN consolidated Tax Foreclosure Notices.

    Partial fallback for the 5K-lever target until ohio.column.us integration
    lands (G3). Emits ~200 signals/yr from Treasurer-filed cases instead of
    the 19K Column pre-distress universe; payload shape is identical so the
    Column path can drop into the same gate/dashboard wiring later.
    """

    site_name = SITE_NAME
    signal_source = SIGNAL_SOURCE

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    async def fetch_signals(self) -> list[RawSignal]:
        headers = default_headers()
        async with httpx.AsyncClient(
            headers=headers, timeout=_TIMEOUT, follow_redirects=True,
        ) as client:
            index_html = await _get_with_retry(client, _TLN_FORECLOSURES_INDEX)
            if not index_html:
                logger.error("LucasDelinquentTax: TLN /foreclosures/ index fetch failed")
                return []
            await asyncio.sleep(_INTER_REQ_DELAY)

            soup = BeautifulSoup(index_html, "html.parser")

            # Find every "Tax Foreclosure Notices" article URL on the index.
            article_urls: list[str] = []
            for a in soup.find_all("a", href=_ARTICLE_HREF_RE):
                href = a.get("href").split("?")[0]
                txt = a.get_text(strip=True)
                if _TFN_TITLE_RE.search(txt) or "tax-foreclosure" in href.lower():
                    full = urljoin(_TLN_BASE, href)
                    if full not in article_urls:
                        article_urls.append(full)

            if not article_urls:
                logger.warning("LucasDelinquentTax: no 'Tax Foreclosure Notices' articles "
                               "found on /foreclosures/ index — Treasurer may be between "
                               "publication windows.")
                return []

            logger.info("LucasDelinquentTax: %d TFN articles", len(article_urls))

            all_signals: list[RawSignal] = []
            for url in article_urls:
                html = await _get_with_retry(client, url)
                await asyncio.sleep(_INTER_REQ_DELAY)
                if not html:
                    continue

                art = BeautifulSoup(html, "html.parser")
                pub_meta = art.find("meta", attrs={"itemprop": "datePublished"})
                pub_iso = pub_meta.get("content") if pub_meta else None
                if pub_iso:
                    try:
                        observed_at = datetime.fromisoformat(pub_iso)
                        if observed_at.tzinfo is None:
                            observed_at = observed_at.replace(tzinfo=timezone.utc)
                    except ValueError:
                        observed_at = datetime.now(timezone.utc)
                else:
                    observed_at = datetime.now(timezone.utc)

                body_node = (
                    art.find("div", class_=re.compile(r"asset-content|subscriber-only|tnt-content"))
                    or art.find("article") or art
                )
                body = body_node.get_text("\n", strip=True)
                if not body:
                    continue

                for case_no, chunk in _split_into_cases(body):
                    all_signals.extend(_parse_case_block(case_no, chunk, observed_at, url))

            # ── LUC backfill ────────────────────────────────────────────────
            parcels = sorted({s.parcel_number for s in all_signals})
            luc_map = await _backfill_luc_from_areis(parcels)
            for sig in all_signals:
                sig.payload["luc"] = luc_map.get(sig.parcel_number, "")

            # ── Report the GATED count (RULE #1: residential + real balance) ─
            def _gated(sig: RawSignal) -> bool:
                amt_raw = sig.payload.get("delq_amount") or ""
                if not re.match(r"^[0-9.]+$", amt_raw):
                    return False
                try:
                    amt = float(amt_raw)
                except ValueError:
                    return False
                if amt < 2000:
                    return False
                luc = sig.payload.get("luc") or ""
                return luc.startswith("5")

            gated = sum(1 for s in all_signals if _gated(s))
            logger.info(
                "LucasDelinquentTax: %d total signals, %d GATED (residential LUC 5xx + balance >= $2000)",
                len(all_signals), gated,
            )
            return all_signals


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)

    async def _dry_run() -> None:
        print("\n=== Lucas Delinquent Tax (TLN TFN fallback) dry-run ===\n")
        scraper = LucasDelinquentTaxScraper(dry_run=True)
        signals = await scraper.fetch_signals()
        print(f"\nTotal signals: {len(signals)}")
        for sig in signals[:5]:
            d = {
                "parcel_number": sig.parcel_number,
                "signal_type": sig.signal_type,
                "source": sig.source,
                "observed_at": sig.observed_at.isoformat() if sig.observed_at else None,
                "confidence": sig.confidence,
                "payload": sig.payload,
            }
            print(json.dumps(d, indent=2))
            print()

    asyncio.run(_dry_run())
