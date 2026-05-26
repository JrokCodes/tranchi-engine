"""
Probate case_status re-check / backfill — the cursor-source "stale" mechanism.

WHY THIS EXISTS: probate is a CURSOR-walk scraper (see staleness.py). The main run
only fetches NEW case IDs forward and never re-visits old ones, so a probate listing
can never be retired by "not seen this cycle" — it would wrongly retire the whole
back-catalog (the May-2026 bug). Instead, a probate case retires when its court
status flips to CLOSED/DISPOSED. This script re-walks the ProWare internal-ID range
the scraper has already covered, re-fetches each CaseSummary, and writes the current
case_status + case_status_date onto every tranchi.listings row for that case_number.
The read API then drops closed cases from the deal view.

It reuses the proven fetch/parse helpers from probate.py — no new ASP.NET form
interaction, no extra ToS exposure (same 1 req/sec ProwareSession).

Usage:
  python -m app.scrapers.probate_recheck --backfill          # full covered range
  python -m app.scrapers.probate_recheck --start A --end B    # explicit ID range
  python -m app.scrapers.probate_recheck --recent N           # last N IDs (freshness)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_here = Path(__file__).resolve().parent
_backend = _here.parent.parent
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
logger = logging.getLogger("scrapers.probate_recheck")

import asyncpg  # noqa: E402

from app.scrapers.probate import (  # noqa: E402
    _BASE_URL,
    _TERMS_PATH,
    _AGREE_BUTTON,
    _SUMMARY_PATH,
    _int_to_q,
    _get_page,
    _parse_case_summary,
)
from app.scrapers.proware_client import ProwareSession  # noqa: E402

_PROBATE_SITE = "Cuyahoga Probate Court"
# Seed value from migration 002_probate_cursor.py — start of the covered ID space.
_SEED_ID = 2817655


async def _cursor(pool: asyncpg.Pool) -> int:
    return int(await pool.fetchval("SELECT last_id FROM tranchi.probate_cursor WHERE id = 1"))


_CLOSED_WORDS = ("closed", "disposed", "terminated", "dismissed")


def _is_closed(case_status: str) -> bool:
    s = case_status.lower()
    return any(w in s for w in _CLOSED_WORDS)


async def _update_case_status(
    pool: asyncpg.Pool, case_number: str, case_status: str, status_date, dry_run: bool
) -> int:
    """Write case_status onto every listing for this case, and RETIRE closed cases.

    A closed/disposed/terminated/dismissed estate is no longer a live lead — the
    property has transferred. We flip its listings to status='expired' (excluded
    from both the active deal view AND the cross-source dedup pool, so a closed row
    can't become the canonical for a parcel and hide an open one). Open cases keep
    their current status. Returns rows touched.
    """
    if dry_run:
        return 0
    if _is_closed(case_status):
        result = await pool.execute(
            """
            UPDATE tranchi.listings
            SET case_status = $2, case_status_date = $3, status = 'expired'
            WHERE source_site = $4 AND case_number = $1
              AND status IN ('active', 'not_listed')
            """,
            case_number, case_status, status_date, _PROBATE_SITE,
        )
    else:
        result = await pool.execute(
            """
            UPDATE tranchi.listings
            SET case_status = $2, case_status_date = $3
            WHERE source_site = $4 AND case_number = $1
            """,
            case_number, case_status, status_date, _PROBATE_SITE,
        )
    return int(result.split()[-1]) if result else 0


async def recheck(start_id: int, end_id: int, dry_run: bool = False) -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        from app.config import settings
        database_url = settings.DATABASE_URL

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    cases_seen = 0
    rows_updated = 0
    closed_found = 0
    try:
        logger.info("Probate re-check: IDs %d..%d (%d ids)", start_id, end_id, end_id - start_id + 1)
        async with ProwareSession(_BASE_URL, rate_limit_sec=1.0) as session:
            await session.accept_agreement(path=_TERMS_PATH, agree_button_id=_AGREE_BUTTON)
            logger.info("T&C accepted, session established")

            for current_id in range(start_id, end_id + 1):
                q = _int_to_q(current_id)
                try:
                    html = await _get_page(session, _SUMMARY_PATH, q)
                    summary = _parse_case_summary(html)
                except Exception as exc:
                    logger.debug("id %d fetch error: %s", current_id, exc)
                    continue
                if not summary:
                    continue  # non-EST or empty ID
                case_number = summary["case_number"]
                case_status = (summary.get("case_status") or "").strip()
                status_date = summary.get("status_date")
                if not case_status:
                    continue
                cases_seen += 1
                n = await _update_case_status(pool, case_number, case_status, status_date, dry_run)
                rows_updated += n
                if any(w in case_status.lower() for w in ("closed", "disposed", "terminated", "dismissed")):
                    closed_found += 1
                    logger.info("CLOSED: %s status=%r (%d listing rows)", case_number, case_status, n)
                if cases_seen % 50 == 0:
                    logger.info(
                        "...progress: %d EST cases re-checked, %d rows updated, %d closed so far (id=%d)",
                        cases_seen, rows_updated, closed_found, current_id,
                    )
        logger.info(
            "Probate re-check complete: %d EST cases, %d listing rows updated, %d CLOSED cases found.",
            cases_seen, rows_updated, closed_found,
        )
    finally:
        await pool.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Probate case_status re-check / backfill")
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
    else:  # --backfill (default)
        start, end = _SEED_ID + 1, cursor

    await recheck(start, end, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
