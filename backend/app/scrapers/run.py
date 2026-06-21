"""
Tranchi Engine — Scraper CLI entrypoint.

Usage:
    python -m app.scrapers.run                     # run all scrapers
    python -m app.scrapers.run --site land_bank    # run one scraper
    python -m app.scrapers.run --dry-run           # parse only, no DB writes
    python -m app.scrapers.run --dry-run --site sheriff_sales

Exit code:
    0 — all scrapers succeeded (or no errors)
    1 — at least one scraper had errors

Dedup invariant: _dedup_cross_source_listings collapses by normalized PARCEL
(source_listing_id, display format DDD-NN-NNN) when present, else by
normalized_address, across the live pool (status IN ('active','not_listed')).
Parcel-primary keying collapses the same property across sources even when the
address strings differ. NULL sale_date does not split clusters. Canonical row =
most recent non-NULL sale_date (fresh re-offer beats stale original), tiebroken
by oldest first_seen_at. Expired/cancelled rows are excluded so re-listed
properties re-canonicalize cleanly.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap: allow running as `python -m app.scrapers.run` from backend/
# ─────────────────────────────────────────────────────────────────────────────

_here = Path(__file__).resolve().parent        # backend/app/scrapers/
_backend = _here.parent.parent                 # backend/
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

_env_file = _backend / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scrapers.run")

import asyncpg  # noqa: E402 — after path setup

from app.market_config import (  # noqa: E402
    MARKETS, market_for_scraper, full_run_skip_keys,
)
from app.scrapers.base import SignalScraper  # noqa: E402
from app.scrapers.code_violations import (  # noqa: E402
    CodeViolationsScraper,
    upsert_signals as _cv_upsert_signals,
)
from app.scrapers.db import upsert_listings  # noqa: E402
from app.scrapers.surface_distress import surface_distress_leads  # noqa: E402
from app.scrapers.collapse_guard import check_and_alert_collapse  # noqa: E402
from app.scrapers.wayne_blight_tiering import tier_wayne_blight_leads  # noqa: E402
from app.scrapers.delinquent_tax import DelinquentTaxScraper  # noqa: E402
from app.scrapers.dln import DLNScraper  # noqa: E402
from app.scrapers.forfeited_land import ForfeitedLandScraper  # noqa: E402
from app.scrapers.fiscal_officer import (  # noqa: E402
    FiscalOfficerScraper,
    upsert_parcels as _fo_upsert_parcels,
)
from app.scrapers.shelby_parcels import ShelbyParcelsScraper  # noqa: E402
from app.scrapers.summit_parcels import SummitParcelsScraper  # noqa: E402
from app.scrapers.lucas_parcels import LucasParcelsScraper  # noqa: E402
from app.scrapers.wayne_parcels import WayneParcelsScraper  # noqa: E402
from app.scrapers.wayne_foreclosure import WayneForeclosureScraper  # noqa: E402
from app.scrapers.wayne_dlba import WayneDLBAScraper  # noqa: E402
from app.scrapers.wayne_wclb import WayneCountyLandBankScraper  # noqa: E402
from app.scrapers.wayne_tax_auction import WayneTaxAuctionScraper  # noqa: E402
from app.scrapers.wayne_blight import WayneBlightScraper  # noqa: E402
from app.scrapers.wayne_delinquent_tax import WayneDelinquentTaxScraper  # noqa: E402
from app.scrapers.summit_realauction import SummitRealAuctionScraper  # noqa: E402
from app.scrapers.summit_legalnews import SummitLegalNewsScraper  # noqa: E402
from app.scrapers.summit_probate import SummitProbateScraper  # noqa: E402
from app.scrapers.summit_landbank import SummitLandBankScraper  # noqa: E402
from app.scrapers.summit_delinquent_tax import SummitDelinquentTaxScraper  # noqa: E402
from app.scrapers.summit_foreclosure_filings import SummitForeclosureFilingsScraper  # noqa: E402
from app.scrapers.shelby_tax_sale import ShelbyTaxSaleScraper  # noqa: E402
from app.scrapers.shelby_foreclosure import ShelbyForeclosureScraper  # noqa: E402
from app.scrapers.shelby_delinquent_tax import ShelbyDelinquentTaxScraper  # noqa: E402
from app.scrapers.shelby_county_landbank import ShelbyCountyLandBankScraper  # noqa: E402
from app.scrapers.shelby_mmlba import MemphisMMLBAScraper  # noqa: E402
from app.scrapers.shelby_probate import ShelbyProbateScraper  # noqa: E402
from app.scrapers.shelby_evictions import ShelbyEvictionsScraper  # noqa: E402
from app.scrapers.landbank import LandBankScraper  # noqa: E402
from app.scrapers.models import ScrapeResult  # noqa: E402
from app.scrapers.prefilter import prefilter  # noqa: E402
from app.scrapers.probate import ProbateScraper  # noqa: E402
from app.scrapers.sheriff import SheriffSalesScraper  # noqa: E402
from app.scrapers.staleness import full_rescan_sources  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# Scraper registry
# ─────────────────────────────────────────────────────────────────────────────
# ListingScraper subclasses → write to tranchi.listings via upsert_listings().
# SignalScraper subclasses  → write to tranchi.signals  via their upsert_signals().
# The _run_scraper() dispatcher checks isinstance(scraper, SignalScraper) to route.
_SCRAPERS: dict[str, type] = {
    "code_violations": CodeViolationsScraper,
    "land_bank": LandBankScraper,
    "sheriff_sales": SheriffSalesScraper,
    "fiscal_officer": FiscalOfficerScraper,
    "probate": ProbateScraper,
    "dln": DLNScraper,
    "forfeited_land": ForfeitedLandScraper,   # tax-deed listings (ArcGIS)
    "delinquent_tax": DelinquentTaxScraper,   # tax-distress SIGNAL (ArcGIS)
    "shelby_parcels": ShelbyParcelsScraper,   # Shelby County (TN) registry spine (ArcGIS)
    "summit_parcels": SummitParcelsScraper,   # Summit County (OH / Akron) registry spine (ArcGIS)
    "lucas_parcels": LucasParcelsScraper,     # Lucas County (OH / Toledo) registry spine (AREIS ArcGIS)
    "wayne_parcels": WayneParcelsScraper,     # Wayne County (MI / Detroit) registry spine + Property-Sales overlay (ArcGIS)
    "wayne_foreclosure": WayneForeclosureScraper,  # Wayne (MI) mortgage foreclosure (mipublicnotices area=82 + auction.com), redemption lifecycle
    "wayne_dlba": WayneDLBAScraper,                # Detroit Land Bank (buildingdetroit + ArcGIS) — structures + buyable lots
    "wayne_wclb": WayneCountyLandBankScraper,      # Wayne County Land Bank (ePropertyPlus, out-county)
    "wayne_tax_auction": WayneTaxAuctionScraper,   # Wayne Treasurer tax-foreclosure auction — SEASONAL, ships dormant
    "wayne_blight": WayneBlightScraper,            # Detroit blight tickets SIGNAL (DELTA on ticket_updated_at)
    "wayne_delinquent_tax": WayneDelinquentTaxScraper,  # Wayne Treasurer forfeiture-PDF SIGNAL (pre-distress)
    "shelby_tax_sale": ShelbyTaxSaleScraper,           # Shelby (TN) tax-deed pre-sale catalog (CSV)
    "shelby_foreclosure": ShelbyForeclosureScraper,    # Shelby (TN) mortgage/trustee-sale foreclosure (tnforeclosurenotices + auction.com)
    "shelby_delinquent_tax": ShelbyDelinquentTaxScraper,  # Shelby (TN) tax-delinquent SIGNAL (Trustee lawsuit XLSX)
    "shelby_county_landbank": ShelbyCountyLandBankScraper,  # Shelby (TN) County land bank (ePropertyPlus)
    "shelby_mmlba": MemphisMMLBAScraper,               # Memphis (TN) City land bank MMLBA (Airtable)
    "shelby_probate": ShelbyProbateScraper,            # Shelby (TN) probate estate cases (CourtConnect, precision-first)
    "shelby_evictions": ShelbyEvictionsScraper,        # Shelby (TN) eviction filings SIGNAL (Data Midsouth)
    "summit_realauction": SummitRealAuctionScraper,    # Summit (OH) sheriff sale — mortgage (Fri) + tax (Tue), 2 signal_types
    "summit_legalnews": SummitLegalNewsScraper,        # Summit (OH) Akron Legal News sheriff-sale cross-check
    "summit_probate": SummitProbateScraper,            # Summit (OH) probate estate cases (CourtView/Wicket, cursor)
    "summit_landbank": SummitLandBankScraper,          # Summit (OH) County Land Bank (Tolemi GraphQL)
    "summit_delinquent_tax": SummitDelinquentTaxScraper,  # Summit (OH) certified-delinquent tax SIGNAL (pre-distress)
    "summit_foreclosure_filings": SummitForeclosureFilingsScraper,  # Summit (OH) foreclosure-FILING SIGNAL (pre-distress, ALN)
}


async def _get_last_successful_run(pool: asyncpg.Pool, source_site: str) -> str | None:
    """Query tranchi.scrape_runs for the most recent successful started_at for source_site.

    Returns ISO date string (YYYY-MM-DD) of the last success, or None if no prior
    successful run exists (first run → scraper does full backfill).
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchval(
                """
                SELECT started_at
                FROM tranchi.scrape_runs
                WHERE source_site = $1
                  AND status = 'success'
                ORDER BY started_at DESC
                LIMIT 1
                """,
                source_site,
            )
        if row is None:
            return None
        # row is a datetime (asyncpg returns timestamptz as aware datetime)
        return row.date().isoformat()
    except Exception as exc:
        logger.warning("Failed to fetch last successful run for %r: %s", source_site, exc)
        return None


async def _run_scraper(
    scraper_key: str,
    pool: asyncpg.Pool,
    dry_run: bool,
    full: bool = False,
) -> ScrapeResult:
    """Run a single scraper. Routes to registry, signal, or listing path.

    full=True forces a FULL pull for delta-pull scrapers (last_run_date=None) instead of
    the incremental window — used by the code_violations status-refresh cron so existing
    signals' live status + last_seen_at are re-pulled (a delta pull only catches NEW
    filings, leaving old violations' Open/Closed status + freshness frozen).

    fiscal_officer (registry path): calls fetch_parcels() + upsert_parcels().
        Populates tranchi.parcels only — does NOT write tranchi.listings.
    SignalScraper instances: calls fetch_signals() + upsert_signals() + scrape_runs row.
    ListingScraper instances: calls fetch_and_parse() + prefilter + upsert_listings().
    Never raises — errors are captured into ScrapeResult.
    """
    scraper_cls = _SCRAPERS[scraper_key]

    # ── Delta-pull: resolve last_run_date for scrapers that accept it ─────────
    # Scrapers that support incremental pulls take a last_run_date constructor arg
    # (ISO date string or None for full backfill).
    #   code_violations: delta via FILE_DATE >= <last_run_date - 1 day>
    #   fiscal_officer:  does not accept last_run_date (full A-Z sweep each run)
    # Scrapers that manage their own state (probate cursor) or do full pulls
    # by design (landbank, sheriff) are constructed with no arguments as before.
    _DELTA_PULL_SCRAPERS = {"code_violations", "wayne_blight"}
    if scraper_key in _DELTA_PULL_SCRAPERS:
        # Peek at the site_name by instantiating briefly — cheaper than a separate
        # registry. Alternatively derive site_name from a mock instance.
        _probe = scraper_cls.__new__(scraper_cls)
        site_name_for_query = getattr(_probe, "site_name", scraper_key)
        last_run_date = None if full else await _get_last_successful_run(pool, site_name_for_query)
        if full:
            logger.info("%s: FULL re-pull (status refresh — last_run_date=None)", site_name_for_query)
        elif last_run_date:
            logger.info(
                "%s: incremental pull from last successful run %s",
                site_name_for_query, last_run_date,
            )
        else:
            logger.info("%s: no prior successful run — full backfill", site_name_for_query)
        scraper = scraper_cls(last_run_date=last_run_date)
    elif scraper_key == "fiscal_officer":
        # Registry sweep: enrich_detail=False (light — parcel# + owner + address
        # only, ~26 page fetches). enrich_detail=True does a per-parcel detail
        # POST for all ~20K parcels which takes hours.
        scraper = scraper_cls(enrich_detail=False)
    elif scraper_key == "shelby_parcels":
        # Full sweep of Shelby County (TN) ReGIS at 500/page, 1 req/sec.
        # ~353K parcels = ~707 pages = ~12 minutes. max_parcels=None = full sweep.
        # For a test run: ShelbyParcelsScraper(max_parcels=500)
        scraper = scraper_cls()
    elif scraper_key == "summit_parcels":
        # Full sweep of Summit County (OH) GIS Tax Parcels at 2000/page.
        # 261K parcels = ~131 pages. max_parcels=None = full sweep.
        # For a test run: SummitParcelsScraper(max_parcels=500)
        scraper = scraper_cls()
    elif scraper_key == "wayne_parcels":
        # Full sweep of the Detroit parcel roll (378K, ~190 pages) + the Property-Sales
        # overlay (509K ordered DESC) folded into the spine for the transferred-guard.
        # max_parcels=None = full sweep. For a test run: WayneParcelsScraper(max_parcels=500).
        scraper = scraper_cls()
    elif scraper_key == "lucas_parcels":
        # Full sweep of Lucas County (OH) AREIS at 2000/page (L38 parcels + L84 value join).
        # ~192K parcels = ~96 pages. max_parcels=None = full sweep.
        # For a test run: LucasParcelsScraper(max_parcels=500).
        scraper = scraper_cls()
    elif scraper_key == "wayne_foreclosure":
        # Resolves notice/auction street addresses to Wayne parcels against tranchi.parcels
        # (market='wayne', house# + zip + street), so it needs the pool. Plain ListingScraper.
        scraper = scraper_cls(pool=pool, dry_run=dry_run)
    elif scraper_key == "probate":
        # Probate manages its own cursor (tranchi.probate_cursor) and signal
        # writes internally, so it needs the pool + dry_run flag at construction.
        # It still returns RawListings that flow through the standard listing path.
        scraper = scraper_cls(pool=pool, dry_run=dry_run)
    elif scraper_key == "dln":
        # DLN resolves tax-sale addresses against tranchi.parcels + on-demand
        # MyPlace lookups (cached back), so it needs the pool + dry_run flag.
        # Still a plain ListingScraper — output flows through the listing path.
        scraper = scraper_cls(pool=pool, dry_run=dry_run)
    elif scraper_key == "shelby_foreclosure":
        # Resolves source street addresses to Shelby parcels against tranchi.parcels
        # (house# + zip + street), so it needs the pool. Plain ListingScraper.
        scraper = scraper_cls(pool=pool, dry_run=dry_run)
    elif scraper_key == "shelby_probate":
        # Manages its own cursor (tranchi.shelby_probate_cursor), cross-refs the spine
        # for the name/address join, and writes probate signals — needs pool + dry_run.
        # Drives CourtConnect via Playwright (Cloudflare gate). Plain ListingScraper out.
        scraper = scraper_cls(pool=pool, dry_run=dry_run)
    elif scraper_key == "shelby_evictions":
        # SignalScraper, but resolves property addresses to spine parcels (no parcel in
        # source), so it needs the pool. Output flows through the signal path below.
        scraper = scraper_cls(pool=pool, dry_run=dry_run)
    elif scraper_key == "summit_probate":
        # Manages its own cursor (tranchi.summit_probate_cursor) and resolves the decedent
        # address → Summit parcel against the spine (market='summit'), so it needs the pool.
        # Drives CourtView eServices (Wicket single-use tokens). Plain ListingScraper out.
        scraper = scraper_cls(pool=pool, dry_run=dry_run)
    else:
        scraper = scraper_cls()
    site_name = scraper.site_name
    result = ScrapeResult(source_site=site_name)

    try:
        logger.info("Starting scraper: %s", site_name)

        if scraper_key in ("fiscal_officer", "shelby_parcels", "summit_parcels", "wayne_parcels", "lucas_parcels"):
            # ── Registry path ──────────────────────────────────────────────────
            # Registry scrapers are parcel identity spines, not deal-listing feeds.
            # We populate tranchi.parcels only; tranchi.listings is never touched.
            #
            # fiscal_officer: Cuyahoga County (OH) — MyPlace A-Z sweep.
            # shelby_parcels: Shelby County (TN / Memphis) — ReGIS ArcGIS layer.
            #   Both call fetch_parcels() and feed hits into _fo_upsert_parcels().
            #
            # upsert_signals is SKIPPED for both: tax-distress flags are not
            # fetched in the lightweight registry sweep. Distress-signal enrichment
            # belongs in a separate targeted job.
            started_at = datetime.now(tz=timezone.utc)
            hits = await scraper.fetch_parcels()
            result.found = len(hits)
            result.passed = len(hits)   # registry: every hit is passed through
            logger.info("%s: fetched %d parcels from registry sweep", site_name, result.found)

            upsert_counts = await _fo_upsert_parcels(
                pool, hits, dry_run=dry_run, market=market_for_scraper(scraper_key)
            )
            result.new_inserted = upsert_counts.get("inserted", 0)
            result.updated = upsert_counts.get("updated", 0)
            result.errors = upsert_counts.get("errors", 0)

            if not dry_run:
                try:
                    async with pool.acquire() as conn:
                        result.active = await conn.fetchval(
                            "SELECT COUNT(*) FROM tranchi.parcels"
                        ) or 0
                except Exception:
                    pass

            # Write scrape_runs row so Sources dashboard shows this registry source.
            if not dry_run:
                completed_at = datetime.now(tz=timezone.utc)
                final_status = "error" if result.errors > 0 and result.new_inserted == 0 else "success"
                try:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            """
                            INSERT INTO tranchi.scrape_runs
                                (source_site, started_at, completed_at, status,
                                 found, passed, active, filtered, new_today,
                                 error_message)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, 0, 0, $8)
                            """,
                            site_name,
                            started_at,
                            completed_at,
                            final_status,
                            result.found,
                            result.passed,
                            result.active,
                            result.error_message,
                        )
                except Exception as exc:
                    logger.error("Failed to write scrape_run for %r: %s", site_name, exc)

        elif isinstance(scraper, SignalScraper):
            # ── Signal path ────────────────────────────────────────────────────
            started_at = datetime.now(tz=timezone.utc)
            raw_signals = await scraper.fetch_signals()
            result.found = len(raw_signals)
            result.passed = len(raw_signals)   # signals are not prefiltered
            logger.info("%s: fetched %d signals", site_name, result.found)

            upsert_result = await _cv_upsert_signals(
                pool, raw_signals, market=market_for_scraper(scraper_key), dry_run=dry_run
            )
            result.new_inserted = upsert_result.get("inserted", 0)
            result.updated = upsert_result.get("updated", 0)
            result.errors = upsert_result.get("errors", 0)
            # active for signals = total signals in DB for THIS source. Each
            # SignalScraper declares its signals.source via a `signal_source`
            # attribute (code_violations → cleveland_open_data, delinquent_tax →
            # cuyahoga_fiscal_officer); fall back to the legacy value if unset.
            signal_src = getattr(scraper, "signal_source", "cleveland_open_data")
            if not dry_run:
                try:
                    async with pool.acquire() as conn:
                        result.active = await conn.fetchval(
                            "SELECT COUNT(*) FROM tranchi.signals WHERE source = $1",
                            signal_src,
                        ) or 0
                except Exception:
                    pass

            # Write scrape_runs row so Sources dashboard shows code_violations.
            if not dry_run:
                completed_at = datetime.now(tz=timezone.utc)
                final_status = "error" if result.errors > 0 and result.new_inserted == 0 else "success"
                try:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            """
                            INSERT INTO tranchi.scrape_runs
                                (source_site, started_at, completed_at, status,
                                 found, passed, active, filtered, new_today,
                                 error_message)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, 0, 0, $8)
                            """,
                            site_name,
                            started_at,
                            completed_at,
                            final_status,
                            result.found,
                            result.passed,
                            result.active,
                            result.error_message,
                        )
                except Exception as exc:
                    logger.error("Failed to write scrape_run for %r: %s", site_name, exc)

        else:
            # ── Listing path ───────────────────────────────────────────────────
            raw_listings = await scraper.fetch_and_parse()
            result.found = len(raw_listings)
            logger.info("%s: fetched %d raw listings", site_name, result.found)

            filtered_listings = []
            filtered_out = 0
            for listing in raw_listings:
                passes, reason = prefilter(listing)
                if passes:
                    filtered_listings.append(listing)
                else:
                    filtered_out += 1
                    logger.debug(
                        "%s: filtered %r — %s", site_name, listing.property_address, reason
                    )

            result.filtered = filtered_out
            result.passed = len(filtered_listings)
            logger.info(
                "%s: %d passed filters, %d filtered out",
                site_name, result.passed, filtered_out,
            )

            upsert_result = await upsert_listings(
                pool,
                filtered_listings,
                site_name,
                market=market_for_scraper(scraper_key),
                found_raw=result.found,
                filtered_count=filtered_out,
                dry_run=dry_run,
            )
            result.new_inserted = upsert_result.new_inserted
            result.updated = upsert_result.updated
            result.active = upsert_result.active
            result.new_today = upsert_result.new_today
            result.errors = upsert_result.errors
            result.error_message = upsert_result.error_message

    except Exception as exc:
        logger.exception("Unhandled error in scraper %r: %s", site_name, exc)
        result.errors += 1
        result.error_message = str(exc)

    return result


def _print_results_table(results: list[ScrapeResult]) -> None:
    """Print a formatted results table matching the Sources dashboard shape."""
    print()
    header = f"{'SITE':<25} {'FOUND':>6} {'PASSED':>7} {'ACTIVE':>7} {'FILT':>6} {'DUPES':>6} {'NEW':>5} {'ERR':>5}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r.source_site:<25} "
            f"{r.found:>6} "
            f"{r.passed:>7} "
            f"{r.active:>7} "
            f"{r.filtered:>6} "
            f"{r.dupes:>6} "
            f"{r.new_today:>5} "
            f"{r.errors:>5}"
        )
    print("-" * len(header))

    print(
        f"{'TOTAL':<25} "
        f"{sum(r.found for r in results):>6} "
        f"{sum(r.passed for r in results):>7} "
        f"{sum(r.active for r in results):>7} "
        f"{sum(r.filtered for r in results):>6} "
        f"{sum(r.dupes for r in results):>6} "
        f"{sum(r.new_today for r in results):>5} "
        f"{sum(r.errors for r in results):>5}"
    )
    print()

    for r in results:
        if r.error_message:
            print(f"  ERROR [{r.source_site}]: {r.error_message}")
    print()


async def _mark_stale_listings(
    pool: asyncpg.Pool,
    results: list[ScrapeResult],
    run_start: datetime,
) -> int:
    """Mark listings as 'not_listed' if not refreshed in this scrape cycle.

    Time-not-seen retirement ONLY applies to FULL_RESCAN sources (see
    staleness.py). Cursor sources (probate) never re-visit old rows, so applying
    this here would wrongly retire their entire back-catalog — they retire via the
    periodic case_status re-check instead. Archive sources never go stale.
    Only marks stale for FULL_RESCAN sources that succeeded and found > 0.
    """
    successful_sources = [
        r.source_site for r in results
        if r.found > 0 and r.errors == 0
    ]
    successful_sources = full_rescan_sources(successful_sources)
    if not successful_sources:
        logger.info("Stale detection: no FULL_RESCAN sources to check, skipping.")
        return 0

    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE tranchi.listings
                SET status = 'not_listed'
                WHERE status NOT IN ('not_listed', 'cancelled', 'sold', 'expired')
                  AND last_seen_at < $1
                  AND source_site = ANY($2::text[])
                  -- TN redemption: a tax-deed parcel legitimately LEAVES the pre-sale
                  -- catalog once it sells, so absence != delisted. Never time-retire a
                  -- post-sale or mid-redemption tax_deed row — its lifecycle is owned by
                  -- the redemption post-passes + Chancery confirmation reader, not staleness.
                  AND NOT (signal_type = 'tax_deed'
                           AND (redemption_status = 'pending' OR sale_date < CURRENT_DATE))
                  -- MI redemption (MCL 600.3240): a foreclosed Wayne mortgage notice DROPS
                  -- OFF the mipublicnotices feed after the sale, but the owner can still
                  -- redeem for 6 months — so absence != delisted during that window. Keep
                  -- in-redemption rows visible for 180 days past sale_date (market-scoped so
                  -- OH mortgage_foreclosure, where the sale is final, is unaffected).
                  AND NOT (market = 'wayne' AND signal_type = 'mortgage_foreclosure'
                           AND sale_date >= CURRENT_DATE - INTERVAL '180 days')
                """,
                run_start,
                successful_sources,
            )
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info("Stale detection: marked %d listings as not_listed", count)
            return count
    except Exception as exc:
        logger.warning("Stale detection failed: %s", exc)
        return 0


async def _dedup_cross_source_listings(pool: asyncpg.Pool) -> int:
    """Collapse live listings of the SAME property into one canonical row.

    Cluster key = normalized parcel number (source_listing_id, stored in display
    format DDD-NN-NNN at upsert) when present, else normalized_address. Parcel is
    exact and survives address-string drift across DLN / sheriff / land-bank /
    probate — critical now that multiple sources describe the same parcel.

    Live pool = status IN ('active','not_listed'). Canonical preference:
    rows with a real sale_date over NULL, then most-recent sale_date (so a fresh
    re-offer date wins over a stale original), then oldest first_seen_at. All
    other live rows in the cluster get duplicate_of set to the canonical id.
    Returns count of flagged dupes.
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                WITH clusters AS (
                    SELECT
                        id,
                        COALESCE(NULLIF(source_listing_id, ''), normalized_address) AS cluster_key,
                        FIRST_VALUE(id) OVER (
                            -- market-scoped: a property lives in exactly one market, so two
                            -- different-market rows sharing a parcel string OR a city-less
                            -- normalized_address (e.g. a Detroit vs Memphis "100 MAIN ST"
                            -- NULL-parcel foreclosure) must NEVER collapse cross-market. The
                            -- safety-net pass below is already market-scoped; this matches it.
                            PARTITION BY market, COALESCE(NULLIF(source_listing_id, ''), normalized_address)
                            ORDER BY (sale_date IS NULL), sale_date DESC NULLS LAST, first_seen_at, id
                        ) AS canonical_id
                    FROM tranchi.listings
                    WHERE status IN ('active', 'not_listed')
                      AND COALESCE(NULLIF(source_listing_id, ''), normalized_address) IS NOT NULL
                )
                UPDATE tranchi.listings l
                SET duplicate_of = CASE
                        WHEN c.id = c.canonical_id THEN NULL
                        ELSE c.canonical_id
                    END
                FROM clusters c
                WHERE l.id = c.id
                  AND l.duplicate_of IS DISTINCT FROM CASE
                        WHEN c.id = c.canonical_id THEN NULL
                        ELSE c.canonical_id
                    END
                """
            )
            # SAFETY-NET (foreclosure cross-source): a foreclosure row that is NOT a
            # clean atomic parcel — either a NULL parcel (RealAuction failed to resolve)
            # OR a raw multi-parcel STRING (ALN '68-19967 & 68-19968', an unsplit leftover)
            # — keys on normalized_address instead of a parcel, so the main pass never
            # collapses it onto its parcel-bearing twin (RealAuction<->ALN show the same
            # sheriff sale twice). Attach such a row to the parcel-bearing canonical that
            # shares the SAME normalized_address AND the same case (digit-core compare, so a
            # 'CV…A' re-notice suffix or a source prefix still matches), ONLY when exactly
            # ONE *clean atomic* canonical exists.
            # INVARIANT — a source_listing_id containing '&', ',' or a space is a raw
            # multi-parcel string, NOT a parcel: it must never count as a canonical (it
            # breaks the uniqueness guard, leaving the null twin un-collapsed) and must
            # itself be collapsed into the clean single-parcel row. NEVER match on case
            # alone — a Shelby TS#### tax-sale case covers thousands of parcels; requiring
            # the address too means one sheriff/mortgage case = one property. Runs after the
            # main pass each cycle (idempotent).
            await conn.execute(
                """
                UPDATE tranchi.listings o
                SET duplicate_of = c.id
                FROM tranchi.listings c
                WHERE o.status IN ('active', 'not_listed')
                  AND (NULLIF(o.source_listing_id, '') IS NULL
                       OR o.source_listing_id ~ '[&, ]')
                  AND o.duplicate_of IS NULL
                  AND o.signal_type LIKE '%foreclosure%'
                  AND o.case_number IS NOT NULL
                  AND o.normalized_address IS NOT NULL
                  AND regexp_replace(o.case_number, '[^0-9]', '', 'g') <> ''
                  AND c.status IN ('active', 'not_listed')
                  AND NULLIF(c.source_listing_id, '') IS NOT NULL
                  AND c.source_listing_id !~ '[&, ]'
                  AND c.duplicate_of IS NULL
                  AND c.case_number IS NOT NULL
                  AND c.id <> o.id
                  AND c.market = o.market
                  AND c.normalized_address = o.normalized_address
                  AND regexp_replace(c.case_number, '[^0-9]', '', 'g')
                      = regexp_replace(o.case_number, '[^0-9]', '', 'g')
                  AND (
                        SELECT count(*) FROM tranchi.listings c2
                        WHERE c2.status IN ('active', 'not_listed')
                          AND NULLIF(c2.source_listing_id, '') IS NOT NULL
                          AND c2.source_listing_id !~ '[&, ]'
                          AND c2.duplicate_of IS NULL
                          AND c2.normalized_address = o.normalized_address
                          AND regexp_replace(c2.case_number, '[^0-9]', '', 'g')
                              = regexp_replace(o.case_number, '[^0-9]', '', 'g')
                      ) = 1
                """
            )
            marked = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM tranchi.listings
                WHERE status IN ('active', 'not_listed')
                  AND duplicate_of IS NOT NULL
                """
            )
            logger.info("Dedup: %d live listings currently flagged as duplicates", marked)
            return marked or 0
    except Exception as exc:
        logger.warning("Dedup pass failed: %s", exc)
        return 0


async def _ensure_parcels_for_listings(pool: asyncpg.Pool) -> int:
    """Guarantee every referenced parcel has a row in tranchi.parcels (all sources).

    Many sources reference a parcel (source_listing_id) but don't persist it:
    DLN mortgage listings arrive with an address (no MyPlace lookup), Land Bank
    has no owner data, etc. Without a parcels row the listing can't be
    independently cross-confirmed and parcel-keyed signals are FK-dropped. This
    inserts a STUB row (parcel_number + the listing's address) for any live
    listing whose parcel is missing. Owner/value/tax are filled later by the
    enrich_parcels job (source_url='stub:listing' marks rows needing enrichment).
    Probate upserts full owner data inline, so this mostly covers DLN/Land Bank.
    Returns count of stubs created.
    """
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO tranchi.parcels
                    (parcel_number, situs_address, market, property_state,
                     first_seen_at, last_seen_at, source_url)
                SELECT DISTINCT ON (l.source_listing_id, l.market)
                       l.source_listing_id, l.property_address, l.market, l.property_state,
                       now(), now(), 'stub:listing'
                FROM tranchi.listings l
                LEFT JOIN tranchi.parcels p
                       ON p.parcel_number = l.source_listing_id AND p.market = l.market
                WHERE l.source_listing_id IS NOT NULL AND l.source_listing_id <> ''
                  AND l.status IN ('active', 'not_listed')
                  AND p.parcel_number IS NULL
                ON CONFLICT (parcel_number, market) DO NOTHING
                """
            )
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info("Parcel coverage: created %d stub parcel rows", count)
            return count
    except Exception as exc:
        logger.warning("Parcel coverage step failed: %s", exc)
        return 0


async def _backfill_owner_from_delinquency(pool: asyncpg.Pool) -> int:
    """Fill owner_name on owner-less Shelby parcels from the county tax-delinquency record.

    Some Shelby parcels (alpha-suffix sub-parcels like ...003M, and address-only
    foreclosures) never join the ReGIS spine, so their owner_name stays NULL. But the
    Trustee delinquent-tax lawsuit list names the owner of record ('Name' column),
    which we already capture in the tax_delinquent signal payload ('owner'). This copies
    that authoritative name onto the owner-less parcel.

    Safety invariants (do not weaken):
      - market='shelby' only (the delinquency 'owner' field is TN/Shelby-specific; OH
        owners come from MyPlace and are ~100% covered already).
      - COALESCE-null-only: fills ONLY where parcels.owner_name IS NULL/'' — never
        clobbers a registry-sourced owner. Idempotent + safe to run every cycle, so new
        owner-less rows self-heal.
    Returns count of parcels filled.
    """
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE tranchi.parcels p
                SET owner_name = s.owner,
                    last_seen_at = now()
                FROM (
                    SELECT DISTINCT ON (parcel_number)
                           parcel_number,
                           NULLIF(btrim(payload->>'owner'), '') AS owner
                    FROM tranchi.signals
                    WHERE signal_type = 'tax_delinquent'
                      AND NULLIF(btrim(payload->>'owner'), '') IS NOT NULL
                    ORDER BY parcel_number, observed_at DESC
                ) s
                WHERE p.parcel_number = s.parcel_number
                  AND p.market = 'shelby'
                  AND (p.owner_name IS NULL OR p.owner_name = '')
                """
            )
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info(
                    "Owner backfill (delinquency name): filled %d owner-less Shelby parcels", count
                )
            return count
    except Exception as exc:
        logger.warning("Owner backfill from delinquency failed: %s", exc)
        return 0


async def _mark_expired_listings(pool: asyncpg.Pool) -> int:
    """Mark listings with past sale dates as 'expired'."""
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE tranchi.listings
                SET status = 'expired'
                WHERE sale_date < CURRENT_DATE
                  AND status IN ('active', 'not_listed')
                  -- TN redemption: do NOT expire a tax-deed row just because its sale
                  -- date passed. Post-sale it enters the redemption lifecycle (awaiting
                  -- confirmation -> pending -> final/redeemed), owned by the redemption
                  -- post-passes. Expiring it here would drop the speculative lead.
                  AND NOT (signal_type = 'tax_deed'
                           AND (confirmation_order_date IS NULL
                                OR redemption_status = 'pending'))
                  -- MI redemption (MCL 600.3240): a Wayne mortgage-foreclosure sheriff-deed
                  -- is NOT final at sale — residential owners redeem for 6 months. Do NOT
                  -- expire an in-redemption row; keep it a live lead for 180 days past
                  -- sale_date, then it expires normally. Market-scoped so OH mortgage
                  -- foreclosure (final at sale) still expires the moment its date passes.
                  AND NOT (market = 'wayne' AND signal_type = 'mortgage_foreclosure'
                           AND sale_date >= CURRENT_DATE - INTERVAL '180 days')
                """,
            )
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info("Expired detection: marked %d listings as expired", count)
            return count
    except Exception as exc:
        logger.warning("Expired detection failed: %s", exc)
        return 0


async def _mark_transferred_listings(pool: asyncpg.Pool) -> int:
    """Mark active listings as 'transferred' when the parcel has SOLD out from under
    the lead — applies to EVERY source (generalized 2026-05-29), two complementary rules:

      1. PROBATE (sold-after-filing): the case_number prefix is the filing year
         ('2026EST307208' → 2026). last_sale_date >= Jan 1 of that year means the asset
         changed hands at-or-after the case opened — even if the court case is still OPEN
         (administering proceeds), the house is no longer an estate asset.
      2. ANY SOURCE (sold-while-listed): last_sale_date > first_seen_at means the parcel
         changed hands AFTER we started listing it — a sale/redemption we'd otherwise miss
         between feed cycles (the cross-cutting hardening prompted by the forfeited-land
         catalog finding). Read API's status='active' filter hides transferred rows.

    NOTE — depends on last_sale_date enrichment coverage (scripts/enrich_sales.py). Today
    that backfill is probate-focused, so rule 2 only bites on non-probate parcels once
    enrichment is extended to them (the enrich cron's --signal scope). The guard is in
    place and self-activates as coverage grows.

    Does NOT catch "sold BEFORE we first ingested" (last_sale < first_seen) — that's the
    static-catalog case, handled at the SOURCE (e.g. forfeited_land's live EPV
    current-owner gate). Conservative: never auto-removes without a positive county
    sale-date signal.
    """
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"""
                UPDATE tranchi.listings AS l
                SET status = 'transferred'
                FROM tranchi.parcels AS p
                WHERE l.source_listing_id = p.parcel_number
                  AND l.status = 'active'
                  AND p.last_sale_date IS NOT NULL
                  AND ({_transfer_predicate()})
                """
            )
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info(
                    "Transferred detection: marked %d probate listings as transferred", count
                )
            return count
    except Exception as exc:
        logger.warning("Transferred detection failed: %s", exc)
        return 0


def _transfer_predicate() -> str:
    """Build the OR-predicate for _mark_transferred_listings from market config.

    Rule (1) PROBATE (per market that declares `probate_transfer_rule`): a probate listing
    whose parcel sold at/after the case filing year. The regex + filing-year substring come
    from each market's config (Cuyahoga '2026EST...' => year is chars 1..4; Shelby declares
    None so it contributes no clause — the tracked auto-transfer gap). Rule (2) ANY-SOURCE
    (market-agnostic): parcel changed hands after we first listed it.
    """
    clauses: list[str] = []
    seen: set[str] = set()
    for cfg in MARKETS.values():
        rule = cfg.get("probate_transfer_rule")
        if not rule:
            continue
        if "filing_year_substr" in rule:
            # Year encoded in the case number (Cuyahoga '2026EST...').
            start, length = rule["filing_year_substr"]
            clause = (
                "(l.signal_type = 'probate' "
                f"AND l.case_number ~ '{rule['case_regex']}' "
                "AND p.last_sale_date >= make_date("
                f"CAST(substring(l.case_number FROM {int(start)} FOR {int(length)}) AS INTEGER), 1, 1))"
            )
        elif rule.get("mode") == "filing_date":
            # No year in the case number (Shelby PR#####) — compare against the persisted
            # filing_date column. Only probate rows ever carry filing_date, so this naturally
            # scopes to the markets that set it.
            clause = (
                "(l.signal_type = 'probate' AND l.filing_date IS NOT NULL "
                "AND p.last_sale_date >= l.filing_date)"
            )
        else:
            continue
        if clause not in seen:
            seen.add(clause)
            clauses.append(clause)
    # (2) any source: parcel changed hands after we first listed it.
    clauses.append("(p.last_sale_date > l.first_seen_at::date)")
    return " OR ".join(clauses)


async def _flag_incomplete_addresses(pool: asyncpg.Pool) -> int:
    """Tag active listings whose address has no leading house number.

    Verified 2026-05-28: these are NOT parse bugs and are NOT recoverable. The
    county's own MyPlace situs for the same parcel also lacks a number, and the
    dominant land_use_code is 5000 (residential vacant land) with developer-LLC
    owners. A vacant lot has no structure, so the county assigns no street number.
    They stay in the feed (valid land deals); the tag tells the UI / verifier to
    confirm them by PARCEL NUMBER on MyPlace, not by street address. Returns the
    count currently flagged.
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE tranchi.listings
                SET address_status = 'no_street_number'
                WHERE status IN ('active', 'not_listed')
                  AND property_address !~ '^[[:space:]]*[0-9]'
                  AND address_status IS DISTINCT FROM 'no_street_number'
                """
            )
            # Clear the flag if a listing later gains a numbered address.
            await conn.execute(
                """
                UPDATE tranchi.listings
                SET address_status = NULL
                WHERE address_status = 'no_street_number'
                  AND property_address ~ '^[[:space:]]*[0-9]'
                """
            )
            flagged = await conn.fetchval(
                """
                SELECT COUNT(*) FROM tranchi.listings
                WHERE status IN ('active', 'not_listed')
                  AND address_status = 'no_street_number'
                """
            ) or 0
            if flagged:
                logger.info("Address quality: %d active listings flagged no_street_number", flagged)
            return flagged
    except Exception as exc:
        logger.warning("Address-flagging step failed: %s", exc)
        return 0


async def _compute_redemption_windows(pool: asyncpg.Pool) -> int:
    """Resolve the TN tax-deed redemption window for confirmed-sale rows.

    Runs only on tax_deed listings that have a Chancery `confirmation_order_date`
    (set by shelby_tax_confirmation.py). The window length is driven by delinquency
    age (TCA 67-5-2701): <=5yr -> 365d, 5-7yr -> 180d, 8yr+ -> 90d. When delinquency
    age is unknown (parcels.tax_years_delinquent NULL) we DEFAULT TO THE LONGEST
    window (365d) — the conservative, money-safe direction: a parcel stays flagged
    speculative LONGER, so we never tell Marc a deal is final/safe when it might still
    be clawed back.

    Vacant (30d) and IRS-lien (120d) tiers are NOT triggered: there is no authoritative
    vacancy / IRS-lien flag in the schema, and those are the SHORTEST windows — firing
    them on a guess would mark a still-redeemable deal final too early (the dangerous
    direction). They fall through to the delinquency tier / 365d default until a real
    source exists. redemption_ends = confirmation_order_date + window.

    Idempotent: recomputes 'pending'/newly-confirmed rows, leaves 'redeemed'/'final'
    alone. Does NOT change listings.status (stays 'active' = visible while pending);
    _finalize_expired_redemptions flips elapsed windows to 'final'.
    """
    total = 0
    try:
        async with pool.acquire() as conn:
            # One UPDATE per market that defines a redemption policy (TN today; OH has
            # none — final at sale). Scoped to the market's listings so per-market window
            # tiers never bleed across markets. property_state is the scope today; it
            # upgrades to the market column in the #10 cutover.
            for market, cfg in MARKETS.items():
                rw = cfg.get("redemption_windows")
                if not rw:
                    continue
                days_case, basis_case = _redemption_case_sql(rw)
                result = await conn.execute(
                    f"""
                    UPDATE tranchi.listings AS l
                    SET redemption_window_days = w.window_days,
                        redemption_basis       = w.basis,
                        redemption_ends        = l.confirmation_order_date
                                                 + (w.window_days || ' days')::interval,
                        redemption_status      = 'pending',
                        redemption_checked_at  = now()
                    FROM (
                        SELECT l2.id,
                               {days_case} AS window_days,
                               {basis_case} AS basis
                        FROM tranchi.listings l2
                        LEFT JOIN tranchi.parcels p
                               ON p.parcel_number = l2.source_listing_id
                                  AND p.market = l2.market
                        WHERE l2.signal_type = $1
                          AND l2.market = $2
                          AND l2.confirmation_order_date IS NOT NULL
                          AND (l2.redemption_status IS NULL
                               OR l2.redemption_status = 'pending')
                    ) AS w
                    WHERE l.id = w.id
                    """,
                    rw["signal_type"],
                    market,
                )
                total += int(result.split()[-1]) if result else 0
            if total > 0:
                logger.info("Redemption windows: computed/refreshed %d tax-deed rows", total)
            return total
    except Exception as exc:
        logger.warning("Redemption-window computation failed: %s", exc)
        return 0


def _redemption_case_sql(rw: dict) -> tuple[str, str]:
    """Build (window_days_case, basis_case) SQL from a market's redemption_windows config.

    Tiers are evaluated top-down (first match wins): `gte` => '<col> >= N', `not_null` =>
    '<col> IS NOT NULL'. Reproduces the TCA-67-5-2701 CASE exactly from data.
    """
    col = rw["basis_column"]
    days_whens: list[str] = []
    basis_whens: list[str] = []
    for tier in rw["tiers"]:
        if "gte" in tier:
            cond = f"p.{col} >= {int(tier['gte'])}"
        elif tier.get("not_null"):
            cond = f"p.{col} IS NOT NULL"
        else:
            raise ValueError(f"redemption tier needs 'gte' or 'not_null': {tier}")
        days_whens.append(f"WHEN {cond} THEN {int(tier['days'])}")
        basis_whens.append(f"WHEN {cond} THEN '{tier['basis']}'")
    dflt = rw["default"]
    days_case = "CASE " + " ".join(days_whens) + f" ELSE {int(dflt['days'])} END"
    basis_case = "CASE " + " ".join(basis_whens) + f" ELSE '{dflt['basis']}' END"
    return days_case, basis_case


async def _finalize_expired_redemptions(pool: asyncpg.Pool) -> int:
    """Close out tax-deed rows whose redemption window has elapsed unredeemed.

    A 'pending' row past its redemption_ends with no redemption recorded means the
    statutory window closed and the buyer's deed is now final/clean — no longer a
    distress deal. Flip status -> 'final' (hidden from the active feed) and
    redemption_status -> 'final'. Redeemed parcels never reach here (they are set
    status='transferred'/redemption_status='redeemed' by the confirmation reader, and
    also caught by _mark_transferred_listings via parcels.last_sale_date).
    """
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE tranchi.listings
                SET status            = 'final',
                    redemption_status = 'final'
                WHERE signal_type = 'tax_deed'
                  AND redemption_status = 'pending'
                  AND redemption_ends < CURRENT_DATE
                """
            )
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info("Redemption finalize: %d tax-deed windows elapsed -> final", count)
            return count
    except Exception as exc:
        logger.warning("Redemption finalize failed: %s", exc)
        return 0


async def _compute_mi_redemption(pool: asyncpg.Pool) -> int:
    """Stamp the MI 6-month redemption window (MCL 600.3240) on Wayne mortgage-foreclosure rows.

    Display/lifecycle companion to the market-scoped carve-outs in _mark_expired/_mark_stale.
    MI redemption is a FLAT window keyed on sale_date (not TN's confirmation_order_date tiers):
    redemption_ends = sale_date + 180d; redemption_status='pending' once the sale has passed
    (the row is now IN redemption), else NULL (still pre-sale). We default ALL residential
    foreclosures to 6 months — the 1-month abandoned case (MCL 600.3241a) is not reliably
    detectable from the notice feed (Jayden 2026-06-16). Idempotent; touches only wayne rows.
    """
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE tranchi.listings
                SET redemption_window_days = 180,
                    redemption_basis       = 'mi_6mo',
                    redemption_ends        = sale_date + INTERVAL '180 days',
                    redemption_status      = CASE WHEN sale_date < CURRENT_DATE
                                                  THEN 'pending' ELSE NULL END,
                    redemption_checked_at  = now()
                WHERE market = 'wayne'
                  AND signal_type = 'mortgage_foreclosure'
                  -- include not_listed so a row that briefly fell off the notice feed still
                  -- gets its redemption_ends refreshed (no stale display when it flips back).
                  AND status IN ('active', 'not_listed')
                  AND sale_date IS NOT NULL
                """
            )
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info("MI redemption: stamped window on %d Wayne foreclosure rows", count)
            return count
    except Exception as exc:
        logger.warning("MI redemption computation failed: %s", exc)
        return 0


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tranchi Engine — run deal-sourcing scrapers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--site",
        choices=list(_SCRAPERS.keys()) if _SCRAPERS else [],
        default=None,
        help="Run only this scraper (default: run all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and filter listings but skip DB writes",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force a FULL re-pull for delta scrapers (code_violations status refresh) "
             "instead of the incremental window. Use with --site.",
    )
    args = parser.parse_args()

    if not _SCRAPERS:
        logger.warning("No scrapers registered yet. Phase B scrapers not yet built.")
        return 0

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        try:
            from app.config import settings
            database_url = settings.DATABASE_URL
        except Exception:
            pass

    if not database_url:
        logger.error(
            "DATABASE_URL is not set. "
            "Create a .env file in backend/ with DATABASE_URL=..."
        )
        return 1

    logger.info("Connecting to database...")
    try:
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    except Exception as exc:
        logger.error("Failed to connect to database: %s", exc)
        return 1

    try:
        run_start = datetime.now(tz=timezone.utc)

        if args.site:
            scraper_keys = [args.site]
        else:
            # Heavy registry sweeps that change slowly are EXCLUDED from the every-3h
            # full run and scheduled on their own (weekly) cron — running a 353K-parcel
            # ReGIS sweep every 3h is wasteful and not "laying low". Listings still
            # refresh every 3h; _ensure_parcels_for_listings backfills stubs between
            # weekly spine sweeps so coverage stays 100%. Run explicitly via --site.
            # The skip set is derived from market_config (registry_in_full_run flag), so
            # a new heavy-registry market opts out in config, not here.
            _FULL_RUN_SKIP = full_run_skip_keys()
            scraper_keys = [k for k in _SCRAPERS.keys() if k not in _FULL_RUN_SKIP]

        if args.dry_run:
            logger.info("DRY RUN mode — no data will be written to the database.")

        results: list[ScrapeResult] = []
        for key in scraper_keys:
            result = await _run_scraper(key, pool, dry_run=args.dry_run, full=args.full)
            results.append(result)

        _print_results_table(results)

        if not args.dry_run and not args.site:
            delisted = await _mark_stale_listings(pool, results, run_start)
            expired = await _mark_expired_listings(pool)
            stubs = await _ensure_parcels_for_listings(pool)
            owner_bf = await _backfill_owner_from_delinquency(pool)
            # Surface pre-distress signal parcels as distress_stage='distress_signal' LEADS
            # BEFORE the transfer/dedup guards so leads get the same off-market + dedup
            # treatment as buy-now deals (migration 012).
            lead_stats = await surface_distress_leads(pool)
            # Stamp conviction tiers + raw drivers on the freshly-materialized Wayne blight
            # leads (per-parcel aggregation surface_distress can't do). No-op for other markets.
            blight_tiers = await tier_wayne_blight_leads(pool)
            no_num = await _flag_incomplete_addresses(pool)
            transferred = await _mark_transferred_listings(pool)
            redeem_win = await _compute_redemption_windows(pool)
            mi_redeem = await _compute_mi_redemption(pool)
            finalized = await _finalize_expired_redemptions(pool)
            dupes = await _dedup_cross_source_listings(pool)
            leads_ins = sum(s.get("inserted", 0) for s in lead_stats.values())
            leads_ret = sum(s.get("retired", 0) + s.get("retired_disabled", 0) for s in lead_stats.values())
            logger.info(
                "Post-run: %d delisted, %d expired, %d parcel-stubs, %d owner-backfill, "
                "%d distress-leads(+) %d distress-leads(-), %d no-number, %d transferred, "
                "%d redemption-windows, %d mi-redemption, %d finalized, %d dupes",
                delisted, expired, stubs, owner_bf, leads_ins, leads_ret, no_num, transferred,
                redeem_win, mi_redeem, finalized, dupes,
            )
            logger.info("Post-run: blight-tiers %s", blight_tiers.get("tier_split", blight_tiers))

            # Collapse tripwire: page immediately if any source's active count cratered
            # vs its previous run (the safety net missing on 2026-06-21 when blight went
            # 43,718 -> 6 and the once-daily audit missed it). Never raises.
            collapses = await check_and_alert_collapse(pool)
            if collapses:
                logger.warning("Post-run: collapse tripwire fired for %d source(s)", collapses)

        total_errors = sum(r.errors for r in results)
        return 1 if total_errors > 0 else 0

    finally:
        await pool.close()
        logger.info("Database pool closed.")


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
