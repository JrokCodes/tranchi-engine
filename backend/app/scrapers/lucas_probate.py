"""
Lucas County (OH / Toledo) Probate ESTATE filings — writes to tranchi.signals.

Pre-distress SIGNAL: a newly-opened decedent ESTATE means a property owner has died —
a classic motivated-heir / pre-foreclosure lead. Surfaced as a
distress_stage='distress_signal' LEAD (market_config lucas distress_lead_rules['probate'],
gate_sql=None) once its tranchi.distress_lead_types row is enabled.

WHY THIS SOURCE (and not the court portal):
  Lucas County decommissioned its public probate portal — eaccess.lucas-co-probate-ct.org
  is 100% packet-loss unreachable (dead/firewalled), and the replacement re:SearchOH is
  registration-gated AND carries only the General Division (not Probate). The PUBLIC,
  no-login source is Toledo Legal News, which publishes the court's daily filing lists:
    https://www.toledolegalnews.com/courts/probate/  (TownNews flex template)
  Each "Probate Court Filings Received on M-D-YYYY" article lists that day's filings,
  sectioned (Estate / Guardianship / Trust / Name Change / Adversary / Miscellaneous).
  We take ONLY the Estate entries. Same public-notice fallback philosophy as
  lucas_delinquent_tax (a subset that publishes), not the full sealed court index.

INVARIANTS (read before editing):
  1. ESTATE ENTRIES ONLY. Each estate entry is self-identifying:
     "<YYYY>-EST-<NNNNNN> ESTATE OF <Decedent Name>. <filing actions>".
     The "-EST-" case token + "ESTATE OF" prefix distinguishes estates from guardianships
     (GDN), trusts, name-changes, etc. Match that pattern directly; do NOT try to parse
     section headings.

  2. RECENT DEATHS ONLY. A daily article mixes brand-new estates (current-year case#,
     "App for Authority to Administer Estate") with ongoing activity on OLD estates
     (e.g. 2024-EST "Account Audited"). Keep only case-year >= current_year - 1 so we
     surface recent deaths, not decades-old ones re-appearing on a docket-activity day.
     Dedupe by case# within a run (one estate -> one signal).

  3. NAME-ONLY JOIN, PRECISION-FIRST, BADGED 'probable'. The notice carries NO address or
     parcel — only the decedent name. We join decedent name -> Lucas AREIS owner_name on a
     canonical "SURNAME GIVEN" key, UNIQUE-match only (the key maps to exactly one parcel),
     person-type owners only (entities skipped). Ambiguous / no-match -> DROP (never invent
     a parcel). A name-only match is inherently lower-confidence than the address-anchored
     joins Summit/Shelby use, so every emitted signal is tagged match_method='name_match',
     match_confidence='probable' — the read layer must badge it, never present as confirmed.
     (A coincidental living namesake is the one real mis-join risk; unique-key + person-only
     keeps it conservative, the 'probable' badge keeps it honest.)

  4. STABLE observed_at FROM THE FILING DATE. observed_at = the article datePublished
     (fallback: the M-D-YYYY in the slug). Natural key
     (parcel, signal_type, source, observed_at::date) -> idempotent.

  5. TLN THROTTLES HARD. Bot-tier rate limit -> 429 with multi-second cooldown. Real UA,
     ~1 req / 2.5s, exponential backoff. Process newest articles first; a partial run is
     fine (idempotent — the 3h cron fills gaps).

Name-join examples (TLN decedent -> canonical key -> AREIS owner_name):
  "MELVIN C SCHAMP"   -> "SCHAMP MELVIN"   <- "SCHAMP MELVIN C"
  "Anthony R. Geraci" -> "GERACI ANTHONY"  <- "GERACI ANTHONY R"
  "Deborah Sue Lilje" -> "LILJE DEBORAH"   <- "LILJE DEBORAH S"
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

try:  # asyncpg only needed for the pool type hint / AREIS owner join
    import asyncpg  # noqa: F401
except Exception:  # pragma: no cover
    asyncpg = None  # type: ignore

from app.scrapers._time import today_et
from app.scrapers.base import SignalScraper
from app.scrapers.db import normalize_parcel_for_market
from app.scrapers.models import RawSignal
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

SITE_NAME = "Lucas Probate (TLN)"          # signal scrape_runs identity (distinct from the lead)
SIGNAL_SOURCE = "lucas_tln_probate"
_MARKET = "lucas"

_TLN_BASE = "https://www.toledolegalnews.com"
_PROBATE_INDEX = f"{_TLN_BASE}/courts/probate/"

_TIMEOUT = 30.0
_INTER_REQ_DELAY = 2.5
_RETRY_ATTEMPTS = 4
_RETRY_BASE_DELAY = 6.0

# Only process the most recent N filing articles per run (newest first). Each article is a
# full day's filings (~80 estate entries); a handful of recent days is plenty of fresh
# signal without hammering TLN's throttle. The cron walks forward over time.
_MAX_ARTICLES = 8

# Article links: /courts/probate/<slug-with-"filing">/article_<uuid>.html
# (slug spelling varies in source: "probate-court-filings-..." and the typo "proabte-...").
_ARTICLE_HREF_RE = re.compile(r"^/courts/probate/[^/\"]*fil[^/\"]*/article_[0-9a-f-]+\.html$", re.I)
# Date in the slug: "...received-on-6-17-2026/..."
_SLUG_DATE_RE = re.compile(r"on-(\d{1,2})-(\d{1,2})-(\d{4})", re.I)
# One estate entry: "2026-EST-000089 ESTATE OF Melvin C Schamp."
# The name terminates on a 2+ letter token before the period — NOT on a middle-initial
# period ("Anthony R. Geraci" must capture the full name, not stop at "Anthony R").
_ESTATE_RE = re.compile(r"\b(\d{4})-EST-(\d{6})\s+ESTATE\s+OF\s+(.+?[A-Za-z'\-]{2})\s*\.", re.I)

# Owner strings that are organizations, not people (skip — a decedent is a person).
_ENTITY_RE = re.compile(
    r"\b(LLC|L L C|INC|TRUST|TRUSTEE|COMPANY|CO\b|CORP|BANK|CITY OF|COUNTY|LAND|"
    r"PROPERTIES|PROPERTY|HOLDINGS|LTD|ASSOC|CHURCH|MINISTR|FOUNDATION|FUND|HOMES|"
    r"GROUP|PARTNERS|LP\b|ENTERPRISE|DEVELOPMENT|REALTY|INVESTMENT|MUNICIPAL)\b",
    re.I,
)
_NAME_SUFFIX = {"JR", "SR", "II", "III", "IV", "V"}


def _name_key(raw: str | None, *, order: str) -> str | None:
    """Canonical 'SURNAME GIVEN' join key, uppercased and punctuation-stripped.

    order='first_last'  -> TLN decedent "Melvin C Schamp" (given [middle] surname)
    order='last_first'  -> AREIS owner   "SCHAMP MELVIN C" (surname given [middle])
    Returns None for entities, single-token, or otherwise unkeyable names.
    """
    if not raw:
        return None
    s = re.sub(r"[.,]", " ", raw.upper())
    s = re.sub(r"\s+&.*$", "", s)          # drop joint-owner "& MARY ..." tail (AREIS)
    s = re.sub(r"\s+", " ", s).strip()
    if not s or _ENTITY_RE.search(s):
        return None
    toks = [t for t in s.split() if t]
    # strip trailing generational suffix
    while toks and toks[-1] in _NAME_SUFFIX:
        toks = toks[:-1]
    if len(toks) < 2:
        return None
    if order == "first_last":
        surname, given = toks[-1], toks[0]
    else:  # last_first
        surname, given = toks[0], toks[1]
    if len(surname) < 2 or len(given) < 1:
        return None
    return f"{surname} {given}"


async def _get_with_retry(client: httpx.AsyncClient, url: str) -> str | None:
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url, timeout=_TIMEOUT)
            if resp.status_code == 429:
                if attempt == _RETRY_ATTEMPTS:
                    logger.error("LucasProbate: 429 on %s after %d retries", url, attempt)
                    return None
                wait = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning("LucasProbate: 429 on %s — sleeping %.1fs", url, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.error("LucasProbate GET %s failed: %s", url, exc)
                return None
            await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return None


def _extract_article_links(index_html: str) -> list[str]:
    """Daily probate-filing article URLs from the index (newest-first as rendered)."""
    soup = BeautifulSoup(index_html, "html.parser")
    seen: set[str] = set()
    out: list[str] = []
    for a in soup.find_all("a", href=_ARTICLE_HREF_RE):
        href = a.get("href").split("?")[0]
        if href in seen:
            continue
        seen.add(href)
        out.append(urljoin(_TLN_BASE, href))
    return out


def _article_observed_at(html: str, url: str) -> datetime:
    """Filing date: <meta itemprop=datePublished>, fallback to the M-D-YYYY slug date."""
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"itemprop": "datePublished"})
    iso = meta.get("content") if meta else None
    if iso:
        try:
            dt = datetime.fromisoformat(iso)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    m = _SLUG_DATE_RE.search(url)
    if m:
        mo, da, yr = (int(x) for x in m.groups())
        try:
            return datetime(yr, mo, da, tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _article_body(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = (
        soup.find("div", class_=re.compile(r"asset-content|subscriber-only|tnt-content|tnt-asset-content"))
        or soup.find("article")
        or soup
    )
    return node.get_text("\n", strip=True)


class LucasProbateScraper(SignalScraper):
    """Lucas County probate ESTATE openings (Toledo Legal News) -> tranchi.signals.

    A decedent estate = deceased property owner = pre-distress lead. Needs `pool` to
    resolve decedent names against the AREIS owner spine. Output flows through the signal
    path in run.py (fetch_signals -> _cv_upsert_signals).
    """

    site_name = SITE_NAME
    signal_source = SIGNAL_SOURCE

    def __init__(self, pool: "asyncpg.Pool | None" = None, dry_run: bool = False) -> None:
        self.pool = pool
        self.dry_run = dry_run

    async def _build_owner_index(self) -> dict[str, set[str]]:
        """{ 'SURNAME GIVEN' : {parcel,...} } from person-type Lucas AREIS owners.

        Ambiguous keys (>1 parcel) are kept and dropped at lookup (UNIQUE match only).
        Returns {} when no pool."""
        index: dict[str, set[str]] = {}
        if self.pool is None:
            return index
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT parcel_number, owner_name FROM tranchi.parcels "
                "WHERE market='lucas' AND owner_name IS NOT NULL AND owner_name <> ''"
            )
        for r in rows:
            key = _name_key(r["owner_name"], order="last_first")
            if not key:
                continue
            parcel = normalize_parcel_for_market(r["parcel_number"], _MARKET)
            if parcel:
                index.setdefault(key, set()).add(parcel)
        uniq = sum(1 for v in index.values() if len(v) == 1)
        logger.info("LucasProbate: owner index — %d keys (%d unique-to-one-parcel)", len(index), uniq)
        return index

    async def fetch_signals(self) -> list[RawSignal]:
        min_year = today_et().year - 1   # INVARIANT 2: recent deaths only

        headers = default_headers()
        articles: list[tuple[str, str]] = []   # (url, html)
        async with httpx.AsyncClient(headers=headers, timeout=_TIMEOUT, follow_redirects=True) as client:
            index_html = await _get_with_retry(client, _PROBATE_INDEX)
            if not index_html:
                logger.error("LucasProbate: /courts/probate/ index fetch failed")
                return []
            await asyncio.sleep(_INTER_REQ_DELAY)
            links = _extract_article_links(index_html)[:_MAX_ARTICLES]
            logger.info("LucasProbate: %d filing articles to scan (cap %d)", len(links), _MAX_ARTICLES)
            for url in links:
                html = await _get_with_retry(client, url)
                await asyncio.sleep(_INTER_REQ_DELAY)
                if html:
                    articles.append((url, html))

        owner_index = await self._build_owner_index()

        signals: list[RawSignal] = []
        seen_cases: set[str] = set()
        scanned = kept_recent = matched = ambiguous = nomatch = 0

        for url, html in articles:
            observed_at = _article_observed_at(html, url)
            body = _article_body(html)
            if not body:
                continue
            for m in _ESTATE_RE.finditer(body):
                scanned += 1
                year = int(m.group(1))
                case_number = f"{m.group(1)}-EST-{m.group(2)}"
                decedent = re.sub(r"\s+", " ", m.group(3)).strip()
                if year < min_year:
                    continue
                kept_recent += 1
                if case_number in seen_cases:
                    continue
                seen_cases.add(case_number)

                key = _name_key(decedent, order="first_last")
                cands = owner_index.get(key) if key else None
                if not cands:
                    nomatch += 1
                    continue
                if len(cands) != 1:
                    ambiguous += 1
                    logger.debug("LucasProbate: ambiguous name %r (%d parcels) — drop", decedent, len(cands))
                    continue
                parcel = next(iter(cands))
                matched += 1
                signals.append(RawSignal(
                    parcel_number=parcel,
                    signal_type="probate",
                    source=SIGNAL_SOURCE,
                    observed_at=observed_at,
                    confidence=0.6,   # name-only join — deliberately sub-1.0
                    payload={
                        "case_number": case_number,
                        "decedent_name": decedent,
                        "filing_date": observed_at.date().isoformat(),
                        "match_method": "name_match",
                        "match_confidence": "probable",
                        "source_url": url,
                    },
                ))

        logger.info(
            "LucasProbate: %d probate signals from %d estate entries "
            "(%d recent, %d matched, %d ambiguous-dropped, %d no-match) across %d articles",
            len(signals), scanned, kept_recent, matched, ambiguous, nomatch, len(articles),
        )
        return signals


if __name__ == "__main__":
    import json
    import os
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)

    # Offline join unit-test (no TLN) — proves the name normalizer + key alignment.
    def _selftest() -> None:
        cases = [
            ("MELVIN C SCHAMP", "SCHAMP MELVIN C"),
            ("Anthony R. Geraci", "GERACI ANTHONY R"),
            ("Deborah Sue Lilje", "LILJE DEBORAH S"),
            ("James D Hooper", "HOOPER JAMES D"),
            ("Peyton Robinson", "ROBINSON PEYTON"),
        ]
        print("=== name-key alignment self-test ===")
        ok = 0
        for decedent, owner in cases:
            dk = _name_key(decedent, order="first_last")
            ok_match = dk == _name_key(owner, order="last_first")
            ok += ok_match
            print(f"  {decedent!r:22} -> {dk!r:18} vs owner {owner!r:20} -> {'MATCH' if ok_match else 'MISS'}")
        print(f"  {ok}/{len(cases)} aligned")
        print("  entity skip:", _name_key("WEST CENTRAL HOMES LLC", order="last_first") is None)

    async def _dry_run() -> None:
        dsn = os.environ.get("DATABASE_URL", "")
        pool = None
        if dsn:
            dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
            try:
                pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
            except Exception as exc:  # noqa: BLE001
                print(f"(no pool — name join disabled: {exc})")
        scraper = LucasProbateScraper(pool=pool, dry_run=True)
        try:
            signals = await scraper.fetch_signals()
        finally:
            if pool is not None:
                await pool.close()
        print(f"\nTotal probate signals: {len(signals)}\n")
        for s in signals[:6]:
            print(json.dumps({"parcel_number": s.parcel_number, "signal_type": s.signal_type,
                              "observed_at": s.observed_at.isoformat(), "payload": s.payload}, indent=2))
            print()

    _selftest()
    print()
    asyncio.run(_dry_run())
