"""
Shelby probate case_status re-check / backfill — the CURSOR-source "stale" mechanism.

WHY THIS EXISTS: shelby_probate is a CURSOR-walk scraper (see staleness.py). The main
run only fetches NEW PR-case-numbers forward and never re-visits old ones, so a Shelby
probate listing can never be retired by "not seen this cycle" — that would wrongly
retire the whole back-catalog (the May-2026 Cuyahoga bug). Instead a probate case
retires when its court status flips to CLOSED/DISPOSED. This script re-walks the
PR-number range the scraper has already covered, re-fetches each docket report, and
writes the current case_status onto every tranchi.listings row for that case_number,
flipping CLOSED cases to status='expired' (out of the deal view AND the dedup pool).

Reuses shelby_probate's parser + Playwright Cloudflare-clearance pattern.

Usage:
  python -m app.scrapers.shelby_probate_recheck --backfill        # seed..cursor
  python -m app.scrapers.shelby_probate_recheck --start A --end B  # explicit PR range
  python -m app.scrapers.shelby_probate_recheck --recent N         # last N ids (freshness)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
_backend = _here.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

_env_file = _backend / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("scrapers.shelby_probate_recheck")

import asyncpg  # noqa: E402

from app.scrapers.shelby_probate import (  # noqa: E402
    SITE_NAME,
    _CLOSED_WORDS,
    _DOCKET_URL,
    _ENTRY_URL,
    _PAGE_TIMEOUT_MS,
    _RECLEAR_EVERY,
    _REQ_DELAY_SEC,
    _fmt_case_id,
    _parse_docket,
)

# Seed floor — keep in sync with migration 010's INSERT.
_SEED_ID = int(os.environ.get("SHELBY_PROBATE_SEED", "32500"))


def _is_closed(case_status: str) -> bool:
    s = (case_status or "").lower()
    return any(w in s for w in _CLOSED_WORDS)


async def _cursor(pool: asyncpg.Pool) -> int:
    val = await pool.fetchval("SELECT last_id FROM tranchi.shelby_probate_cursor WHERE id = 1")
    if val is None:
        raise RuntimeError("tranchi.shelby_probate_cursor has no row — run migration 010 first")
    return int(val)


async def _update_case_status(
    pool: asyncpg.Pool, case_id: str, case_status: str, dry_run: bool,
    filing_date=None,
) -> int:
    # filing_date is COALESCE-set (never clobbers an existing value) so this re-check also
    # BACKFILLS the column onto existing Shelby probate rows ingested before migration 015 —
    # the cursor-walk scraper never revisits them, so this is the only path that populates
    # filing_date for the back-catalog, which the filing_date auto-transfer rule depends on.
    if dry_run:
        return 0
    if _is_closed(case_status):
        result = await pool.execute(
            """
            UPDATE tranchi.listings
            SET case_status = $2, status = 'expired', filing_date = COALESCE($4, filing_date)
            WHERE source_site = $3 AND case_number = $1 AND status IN ('active', 'not_listed')
            """,
            case_id, case_status, SITE_NAME, filing_date,
        )
    else:
        result = await pool.execute(
            "UPDATE tranchi.listings SET case_status = $2, filing_date = COALESCE($4, filing_date) "
            "WHERE source_site = $3 AND case_number = $1",
            case_id, case_status, SITE_NAME, filing_date,
        )
    return int(result.split()[-1]) if result else 0


async def recheck(start_id: int, end_id: int, dry_run: bool = False) -> None:
    from playwright.async_api import async_playwright

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        from app.config import settings
        database_url = settings.DATABASE_URL

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    seen = updated = closed = 0
    try:
        logger.info("Shelby probate re-check: PR%06d..PR%06d (%d ids)", start_id, end_id, end_id - start_id + 1)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
                viewport={"width": 1280, "height": 900},
            )
            page = await ctx.new_page()

            async def _clear() -> None:
                await page.goto(_ENTRY_URL, wait_until="domcontentloaded", timeout=_PAGE_TIMEOUT_MS)
                await asyncio.sleep(2.0)

            await _clear()
            try:
                walked = 0
                cf_retries = 0
                for n in range(start_id, end_id + 1):
                    case_id = _fmt_case_id(n)
                    try:
                        html = await page.evaluate(
                            """async (u) => { const r = await fetch(u, {credentials:'include'}); return await r.text(); }""",
                            _DOCKET_URL.format(case_id=case_id),
                        )
                        parsed = _parse_docket(html)
                    except Exception as exc:
                        logger.debug("id %s fetch error: %s", case_id, exc)
                        parsed = None

                    if parsed and parsed.get("_challenge"):
                        cf_retries += 1
                        if cf_retries >= 5:
                            logger.error("Shelby probate re-check: cf challenge persisted — aborting at %s", case_id)
                            break
                        await asyncio.sleep(_REQ_DELAY_SEC)
                        await _clear()
                        continue
                    cf_retries = 0
                    await asyncio.sleep(_REQ_DELAY_SEC)
                    walked += 1
                    if walked % _RECLEAR_EVERY == 0:
                        await _clear()

                    if not parsed:
                        continue
                    case_status = parsed.get("case_status")
                    if not case_status:
                        continue
                    seen += 1
                    n_rows = await _update_case_status(
                        pool, case_id, case_status, dry_run,
                        filing_date=parsed.get("filing_date"),
                    )
                    updated += n_rows
                    if _is_closed(case_status):
                        closed += 1
                        logger.info("CLOSED: %s status=%r (%d listing rows)", case_id, case_status, n_rows)
                    if seen % 50 == 0:
                        logger.info("...progress: %d cases re-checked, %d rows updated, %d closed (id=%s)",
                                    seen, updated, closed, case_id)
            finally:
                await browser.close()
        logger.info("Shelby probate re-check complete: %d cases, %d rows updated, %d CLOSED.", seen, updated, closed)
    finally:
        await pool.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Shelby probate case_status re-check / backfill")
    parser.add_argument("--start", type=int, default=None)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--backfill", action="store_true", help="walk seed..cursor")
    parser.add_argument("--recent", type=int, default=None, help="walk last N ids before cursor")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        from app.config import settings
        database_url = settings.DATABASE_URL
    tmp = await asyncpg.create_pool(database_url, min_size=1, max_size=1)
    try:
        cursor = await _cursor(tmp)
    finally:
        await tmp.close()

    if args.start is not None and args.end is not None:
        start, end = args.start, args.end
    elif args.recent:
        start, end = max(_SEED_ID + 1, cursor - args.recent), cursor
    else:
        start, end = _SEED_ID + 1, cursor

    await recheck(start, end, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
