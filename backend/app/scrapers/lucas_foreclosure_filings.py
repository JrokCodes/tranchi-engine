"""
Lucas County (OH / Toledo) Foreclosure FILINGS — writes to tranchi.signals.

Pre-distress SIGNAL: a foreclosure COMPLAINT filed in Lucas County Common Pleas,
months before any sheriff sale is scheduled. It is surfaced as a
distress_stage='distress_signal' LEAD (market_config lucas
distress_lead_rules['foreclosure_filing'], gate_sql=None — every fresh filing
surfaces) once its tranchi.distress_lead_types row is enabled. It does NOT mint a
buy_now tranchi.listings row.

Source: Toledo Legal News (TownNews CMS) foreclosure-notice index
  https://www.toledolegalnews.com/legal_notices/foreclosures/
The SAME index also carries Treasurer "Tax Foreclosure Notices" (TF cases) — those
are claimed by lucas_delinquent_tax (signal_type='tax_delinquent'). THIS scraper
takes the mortgage / other foreclosure COMPLAINT notices (CI / non-TF cases) and
emits signal_type='foreclosure_filing'. This mirrors Summit's split:
summit_foreclosure_filings (ALN) is the pre-distress filing lever for Summit.

INVARIANTS (read before editing):
  1. PARCEL or RESOLVE-or-DROP. signals.parcel_number FKs (parcel_number, market)
     -> tranchi.parcels, so every signal needs a real Lucas PARID.
       a. Most notices carry the PARID inline ("Parcel No(s). 23-18171"); strip via
          normalize_parcel_for_market(raw, 'lucas') -> 7-digit PARID.
       b. When the notice has NO parcel, resolve its property address against the
          AREIS spine (situs_address) on a UNIQUE house#+street-core+zip5 key only.
          Ambiguous / no-match -> DROP. Never invent a parcel. (Same discipline as
          shelby_evictions: we cannot signal a parcel we cannot identify.)

  2. EXCLUDE TF (Treasurer tax) CASES. TF<yy>-<nnnnn> cases belong to
     lucas_delinquent_tax (signal_type='tax_delinquent'). Skip any notice whose case
     number matches TF\\d{2}-?\\d{3,5} so the two signal feeds never double-count a parcel.

  3. STABLE observed_at FROM THE PUBLICATION DATE. observed_at = the TLN article
     <meta itemprop=datePublished>. A re-scrape of the same notice UPDATEs in place
     rather than inserting a duplicate. The signals natural key is
     (parcel_number, signal_type, source, observed_at::date).

  4. SIGNAL, NOT LISTING. foreclosure_filing is a pre-distress LEAD surfaced from
     tranchi.signals — never a buy_now tranchi.listings row. (lucas_legalnews used to
     emit these as buy_now listings; that path is removed and this scraper owns the
     foreclosure_filing channel.)

PARSE / JOIN format (verified live 2026-06-22 via lucas_legalnews dry-run + spine):
  Notice body carries: Case No. CI2026-00852, plaintiff vs defendant, a property
  address line ("4031 Packard Rd, Toledo, OH 43612"), and sometimes "Parcel No(s).
  23-18171" (DD-DDDDD display form -> 7-digit PARID).
  AREIS situs form: "4031 Packard Rd, Toledo Oh 43612" (abbreviated suffix). The join
  key drops the street-type token + leading direction so "Walnut Circle" (notice) and
  "Walnut Cir" (situs) collide; match requires identical house# + street-core + zip5.

Anchors (verified live 2026-06-22):
  CI2026-00852 -> parcel 0217994 -> 4031 Packard Rd, Toledo OH 43612 (inline parcel)
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

try:  # asyncpg only needed for the pool type hint / address resolution
    import asyncpg  # noqa: F401
except Exception:  # pragma: no cover
    asyncpg = None  # type: ignore

from app.scrapers.base import SignalScraper
from app.scrapers.db import normalize_parcel_for_market
from app.scrapers.models import RawSignal
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

SITE_NAME = "Lucas Foreclosure Filing (Lead)"   # == source_site in market_config
SIGNAL_SOURCE = "lucas_tln_foreclosures"
_MARKET = "lucas"

_TLN_BASE = "https://www.toledolegalnews.com"
_FILING_INDEX = f"{_TLN_BASE}/legal_notices/foreclosures/"

_TIMEOUT = 30.0
_INTER_REQ_DELAY = 2.5       # TLN bot-tier 1 throttle — ~25 req/min is safe
_RETRY_ATTEMPTS = 4
_RETRY_BASE_DELAY = 6.0       # exponential — TLN tier-1 cooldown is multi-second

# Case numbers. CI = Common Pleas civil (mortgage / other foreclosure COMPLAINT).
# TF = Treasurer tax-foreclosure — EXCLUDED here (owned by lucas_delinquent_tax).
_TF_CASE_RE = re.compile(r"\bTF\d{2}-?\d{3,5}\b", re.I)
_CASE_RE = re.compile(r"\b(CI\d{4}-?\d{3,5}|G-\d{4}-[A-Z]{2,4}-[0-9-]+)\b", re.I)
_PARCEL_RE = re.compile(r"\b(\d{2}-\d{5})\b")
_ARTICLE_HREF_RE = re.compile(
    r"^/legal_notices/foreclosures/[^/]+/article_[0-9a-f-]+\.html$", re.I
)
_TFN_TITLE_RE = re.compile(r"Tax\s+Foreclosure\s+Notices", re.I)

# Property-address line: "<house#> <street...> <suffix>, <city>, OH <zip>".
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

# Join-key normalization (house# + street-core + zip5, UNIQUE match only).
_SUFFIX_TOKENS = {
    "STREET", "ST", "AVENUE", "AVE", "ROAD", "RD", "DRIVE", "DR", "BOULEVARD",
    "BLVD", "COURT", "CT", "LANE", "LN", "PLACE", "PL", "TERRACE", "TER",
    "PARKWAY", "PKWY", "CIRCLE", "CIR", "TRAIL", "TRL", "WAY", "HEIGHTS", "HTS",
    "HIGHWAY", "HWY", "ROW", "ROUTE", "RTE",
}
_DIR_TOKENS = {"N": "N", "S": "S", "E": "E", "W": "W",
               "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}
_ZIP_RE = re.compile(r"(\d{5})(?:-\d{4})?\s*$")


def _addr_key(street_line: str | None, zip5: str | None) -> str | None:
    """Build a UNIQUE-match join key '<house#>|<dir>|<street-core>|<zip5>'.

    Drops the unit, the trailing street-type token, and normalizes the leading
    direction so notice 'Walnut Circle' and situs 'Walnut Cir' collide. Returns
    None when there is no leading house number (key would be too loose to trust).
    Conservative by design — only ever used for a UNIQUE spine match.
    """
    if not street_line:
        return None
    a = re.sub(r"\s+", " ", street_line.strip().upper())
    a = a.split(",")[0].strip()        # drop ", TOLEDO OH 43604" tail if present
    toks = a.split()
    if len(toks) < 2:
        return None
    house = toks[0]
    if not re.match(r"^\d+[A-Z]?$", house):
        return None
    rest = toks[1:]
    direction = ""
    if rest and rest[0] in _DIR_TOKENS:
        direction = _DIR_TOKENS[rest[0]]
        rest = rest[1:]
    if rest and rest[-1] in _SUFFIX_TOKENS:
        rest = rest[:-1]
    core = " ".join(rest).strip()
    if not core:
        return None
    return f"{house}|{direction}|{core}|{(zip5 or '')[:5]}"


def _situs_zip(situs: str | None) -> str | None:
    if not situs:
        return None
    m = _ZIP_RE.search(situs.strip())
    return m.group(1) if m else None


async def _get_with_retry(client: httpx.AsyncClient, url: str) -> str | None:
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url, timeout=_TIMEOUT)
            if resp.status_code == 429:
                if attempt == _RETRY_ATTEMPTS:
                    logger.error("LucasForeclosureFilings: 429 on %s after %d retries", url, attempt)
                    return None
                wait = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning("LucasForeclosureFilings: 429 on %s — sleeping %.1fs", url, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error("LucasForeclosureFilings GET %s failed: %s", url, exc)
                return None
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return None


def _extract_filing_article_links(index_html: str) -> list[str]:
    """Article detail URLs on the /foreclosures/ index, EXCLUDING the consolidated
    'Tax Foreclosure Notices' (TF) articles owned by lucas_delinquent_tax."""
    soup = BeautifulSoup(index_html, "html.parser")
    seen: set[str] = set()
    out: list[str] = []
    for a in soup.find_all("a", href=_ARTICLE_HREF_RE):
        href = a.get("href").split("?")[0]
        txt = a.get_text(strip=True)
        # Skip the Treasurer tax-foreclosure consolidations (claimed by the tax scraper).
        if _TFN_TITLE_RE.search(txt) or "tax-foreclosure" in href.lower():
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append(urljoin(_TLN_BASE, href))
    return out


def _parse_filing_detail(html: str, source_url: str) -> dict | None:
    """Parse one TLN foreclosure-complaint article into a raw notice dict.

    Returns None if no case number, or if the case is a TF (Treasurer tax) case.
    """
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    pub_meta = soup.find("meta", attrs={"itemprop": "datePublished"})
    pub_iso = pub_meta.get("content") if pub_meta else None
    observed_at: datetime
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
        soup.find("div", class_=re.compile(r"asset-content|subscriber-only|tnt-content|tnt-asset-content"))
        or soup.find("section", class_=re.compile(r"body|content"))
        or soup.find("article")
        or soup
    )
    body = body_node.get_text("\n", strip=True)
    if not body:
        return None

    # INVARIANT 2: drop Treasurer tax-foreclosure cases (owned by lucas_delinquent_tax).
    if _TF_CASE_RE.search(title) or _TF_CASE_RE.search(body):
        return None

    case_m = _CASE_RE.search(title) or _CASE_RE.search(body)
    case_number = case_m.group(1).upper().replace(" ", "") if case_m else None
    if not case_number:
        return None

    parcels_raw = sorted(set(_PARCEL_RE.findall(body)))
    parcels: list[str] = []
    for praw in parcels_raw:
        canon = normalize_parcel_for_market(praw, _MARKET)
        if canon and canon not in parcels:
            parcels.append(canon)

    addr_m = _ADDR_RE.search(body)
    if addr_m:
        property_address = addr_m.group(1).strip().title()
        property_city = (addr_m.group(2) or "").strip().title() or None
        property_zip = addr_m.group(3) or None
    else:
        property_address = None
        property_city = None
        property_zip = None

    return {
        "case_number": case_number,
        "observed_at": observed_at,
        "parcels": parcels,
        "property_address": property_address,
        "property_city": property_city,
        "property_zip": property_zip,
        "source_url": source_url,
    }


class LucasForeclosureFilingsScraper(SignalScraper):
    """Lucas County foreclosure complaints (Toledo Legal News) -> tranchi.signals.

    Pre-distress pipeline stage — a complaint filed in Common Pleas, months before
    any sheriff sale. Needs `pool` to resolve parcel-less notices against the AREIS
    spine. Output flows through the signal path in run.py
    (fetch_signals -> _cv_upsert_signals, which normalizes + FK-stubs parcels).
    """

    site_name = SITE_NAME
    signal_source = SIGNAL_SOURCE

    def __init__(self, pool: "asyncpg.Pool | None" = None, dry_run: bool = False) -> None:
        self.pool = pool
        self.dry_run = dry_run

    async def _build_spine_index(self) -> dict[str, set[str]]:
        """Load the Lucas spine ONCE into {addr_key: {parcel,...}} for address resolution.

        Ambiguous keys (>1 parcel) are kept and dropped at lookup time — we only ever
        accept a UNIQUE match. Returns {} when no pool (dry-run without DB)."""
        index: dict[str, set[str]] = {}
        if self.pool is None:
            return index
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT parcel_number, situs_address FROM tranchi.parcels "
                "WHERE market='lucas' AND situs_address IS NOT NULL AND situs_address <> ''"
            )
        for r in rows:
            situs = r["situs_address"]
            key = _addr_key(situs, _situs_zip(situs))
            if not key:
                continue
            parcel = normalize_parcel_for_market(r["parcel_number"], _MARKET)
            if parcel:
                index.setdefault(key, set()).add(parcel)
        logger.info("LucasForeclosureFilings: spine index built — %d unique address keys", len(index))
        return index

    async def fetch_signals(self) -> list[RawSignal]:
        headers = default_headers()
        async with httpx.AsyncClient(headers=headers, timeout=_TIMEOUT, follow_redirects=True) as client:
            index_html = await _get_with_retry(client, _FILING_INDEX)
            if not index_html:
                logger.error("LucasForeclosureFilings: /foreclosures/ index fetch failed")
                return []
            await asyncio.sleep(_INTER_REQ_DELAY)

            links = _extract_filing_article_links(index_html)
            logger.info("LucasForeclosureFilings: %d foreclosure-complaint articles", len(links))

            notices: list[dict] = []
            for url in links:
                html = await _get_with_retry(client, url)
                await asyncio.sleep(_INTER_REQ_DELAY)
                if not html:
                    continue
                notice = _parse_filing_detail(html, url)
                if notice is not None:
                    notices.append(notice)

        # Spine index for parcel-less notices (resolved by unique address match).
        spine = await self._build_spine_index()

        signals: list[RawSignal] = []
        emitted_inline = resolved_addr = dropped_unresolved = 0
        seen_keys: set[tuple[str, str]] = set()   # (parcel, observed-date) de-dupe within a run

        for n in notices:
            parcels: list[str] = list(n["parcels"])
            via = "inline"
            if not parcels:
                key = _addr_key(n.get("property_address"), n.get("property_zip"))
                cands = spine.get(key) if key else None
                if cands and len(cands) == 1:
                    parcels = [next(iter(cands))]
                    via = "address"
                else:
                    dropped_unresolved += 1
                    logger.debug(
                        "LucasForeclosureFilings: drop case %s — no parcel and address %r unresolved",
                        n.get("case_number"), n.get("property_address"),
                    )
                    continue

            for parcel in parcels:
                dedupe = (parcel, n["observed_at"].date().isoformat())
                if dedupe in seen_keys:
                    continue
                seen_keys.add(dedupe)
                payload: dict[str, Any] = {
                    "case_number": n["case_number"],
                    "filing_date": n["observed_at"].date().isoformat(),
                    "property_address": n.get("property_address"),
                    "property_city": n.get("property_city"),
                    "property_zip": n.get("property_zip"),
                    "parcel_match": via,   # 'inline' | 'address' — provenance of the join
                    "source_url": n.get("source_url"),
                }
                signals.append(RawSignal(
                    parcel_number=parcel,
                    signal_type="foreclosure_filing",
                    source=SIGNAL_SOURCE,
                    observed_at=n["observed_at"],
                    confidence=1.0,
                    payload=payload,
                ))
                if via == "inline":
                    emitted_inline += 1
                else:
                    resolved_addr += 1

        logger.info(
            "LucasForeclosureFilings: %d signals (%d inline-parcel, %d address-resolved) "
            "from %d notices; %d dropped (no parcel + unresolved address)",
            len(signals), emitted_inline, resolved_addr, len(notices), dropped_unresolved,
        )
        return signals


if __name__ == "__main__":
    import json
    import os
    import sys
    from pathlib import Path

    _backend = Path(__file__).resolve().parent.parent.parent
    _env = _backend / ".env"
    if _env.exists():
        from dotenv import load_dotenv
        load_dotenv(_env)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout,
    )

    async def _dry_run() -> None:
        print("\n=== Lucas Foreclosure Filings (TLN complaints) dry-run ===\n")
        dsn = os.environ.get("DATABASE_URL", "")
        pool = None
        if dsn:
            dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
            try:
                pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
            except Exception as exc:  # noqa: BLE001
                print(f"(no pool — address resolution disabled: {exc})")

        scraper = LucasForeclosureFilingsScraper(pool=pool, dry_run=True)
        try:
            signals = await scraper.fetch_signals()
        finally:
            if pool is not None:
                await pool.close()

        print(f"\nTotal foreclosure_filing signals: {len(signals)}\n")
        for sig in signals[:6]:
            print(json.dumps({
                "parcel_number": sig.parcel_number,
                "signal_type": sig.signal_type,
                "source": sig.source,
                "observed_at": sig.observed_at.isoformat() if sig.observed_at else None,
                "payload": sig.payload,
            }, indent=2))
            print()

        # Anchor: CI2026-00852 -> parcel 0217994 (inline parcel join)
        anchor = [s for s in signals if s.payload.get("case_number") == "CI2026-00852"]
        if anchor:
            ok = anchor[0].parcel_number == "0217994"
            print(f"ANCHOR CI2026-00852 -> parcel {anchor[0].parcel_number!r} (expect '0217994') — {'PASS' if ok else 'FAIL'}")
        else:
            print("ANCHOR CI2026-00852 not on the page today (may have aged off) — non-fatal.")

    asyncio.run(_dry_run())
