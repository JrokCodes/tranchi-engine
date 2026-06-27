"""
Montgomery County (OH / Dayton) Probate Court — case_status re-check / backfill.

WHY THIS EXISTS: dayton_probate is a CURSOR-walk scraper (see staleness.py). The main
run only fetches NEW EST-case-numbers forward and never re-visits old ones, so a Dayton
probate listing can never be retired by "not seen this cycle" — that would wrongly retire
the whole back-catalog (the May-2026 6,446-case bug class). Instead a probate case retires
when its court status flips to CLOSED/DISPOSED. This script re-walks a casenbr range for a
given year, re-fetches each case's status page, and writes the current case_status onto
every tranchi.listings row for that case_number, flipping CLOSED cases to status='expired'.

ACCESS: Plain httpx (no Playwright) — the ColdFusion backend has no anti-bot gate (confirmed
live 2026-06-27). A ColdFusion session (CFID/CFTOKEN/JSESSIONID) must be established first by
GET-ing the search form page; httpx.AsyncClient persists cookies automatically. The session
is periodically refreshed (every _RECLEAR_EVERY cases) so long backfill runs stay live.

Usage:
  python -m app.scrapers.dayton_probate_recheck --backfill               # 1..cursor (current year)
  python -m app.scrapers.dayton_probate_recheck --start 1 --end 100      # explicit casenbr range
  python -m app.scrapers.dayton_probate_recheck --recent 200             # last N ids before cursor
  python -m app.scrapers.dayton_probate_recheck --year 2025 --backfill   # historic year, 1..end
  python -m app.scrapers.dayton_probate_recheck --year 2025 --start 1 --end 500
  python -m app.scrapers.dayton_probate_recheck --dry-run --recent 10    # read-only test
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
logger = logging.getLogger("scrapers.dayton_probate_recheck")

import asyncpg  # noqa: E402
import httpx     # noqa: E402

from app.scrapers.dayton_probate import (  # noqa: E402
    SITE_NAME,
    _CLOSED_WORDS,
    _DETAIL_BASE,
    _REQ_DELAY_SEC,
    _SEARCH_URL,
    _SESSION_FORM_URL,
    _UA,
    _is_closed,
    _parse_detail,
    _parse_search_result,
    _read_cursor,
)

# Refresh the ColdFusion session every N cases (prevent 20-min CF session timeout on long runs).
_RECLEAR_EVERY: int = 150


async def _establish_session(client: httpx.AsyncClient) -> None:
    """GET the search form URL to obtain CFID/CFTOKEN/JSESSIONID cookies.

    Without these cookies the POST to casesearch_actionx.cfm returns 200 OK but
    empty results (no case rows). httpx.AsyncClient persists cookies across calls.
    """
    resp = await client.get(_SESSION_FORM_URL)
    resp.raise_for_status()
    logger.info(
        "DaytonProbateRecheck: CF session established (cookies: %s)",
        ", ".join(client.cookies.keys()),
    )


async def _update_case_status(
    pool: asyncpg.Pool,
    case_number: str,
    case_status: str,
    dry_run: bool,
) -> int:
    """Write case_status (and 'expired' if CLOSED) onto tranchi.listings rows.

    Scoped to SITE_NAME ('Montgomery County Probate'). Returns count of rows updated.
    """
    if dry_run:
        return 0
    if _is_closed(case_status):
        result = await pool.execute(
            """
            UPDATE tranchi.listings
               SET case_status = $2,
                   status      = 'expired'
             WHERE source_site  = $3
               AND case_number  = $1
               AND status IN ('active', 'not_listed')
            """,
            case_number, case_status, SITE_NAME,
        )
    else:
        result = await pool.execute(
            """
            UPDATE tranchi.listings
               SET case_status = $2
             WHERE source_site = $3
               AND case_number = $1
               AND status IN ('active', 'not_listed')
            """,
            case_number, case_status, SITE_NAME,
        )
    return int(result.split()[-1]) if result else 0


async def recheck(
    caseyear: int,
    start_nbr: int,
    end_nbr: int,
    dry_run: bool = False,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Walk casenbr range for caseyear and update case_status on matching DB rows."""
    seen = updated = closed = 0
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    logger.info(
        "DaytonProbateRecheck: caseyear=%d, casenbr %d..%d (%d ids), dry_run=%s",
        caseyear, start_nbr, end_nbr, end_nbr - start_nbr + 1, dry_run,
    )

    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=20.0,
    ) as client:
        await _establish_session(client)

        for nbr in range(start_nbr, end_nbr + 1):
            # Periodic session refresh to outlast the ~20-min CF session timeout.
            if nbr > start_nbr and (nbr - start_nbr) % _RECLEAR_EVERY == 0:
                try:
                    await _establish_session(client)
                except Exception as exc:
                    logger.warning(
                        "DaytonProbateRecheck: session refresh failed at casenbr=%d: %s", nbr, exc
                    )

            await asyncio.sleep(_REQ_DELAY_SEC)

            # ── Search POST ────────────────────────────────────────────────────
            try:
                resp = await client.post(
                    _SEARCH_URL,
                    data={"caseyear": str(caseyear), "casenbr": str(nbr), "SEARCH": "GO"},
                )
                resp.raise_for_status()
                search_html = resp.text
            except Exception as exc:
                logger.debug("DaytonProbateRecheck: casenbr=%d search failed: %s", nbr, exc)
                continue

            parsed_search = _parse_search_result(search_html)
            if parsed_search is None:
                # No case at this casenbr (gap or beyond frontier) — skip silently.
                continue

            case_number = parsed_search.get("case_number", "")
            detail_href = parsed_search.get("detail_href", "")
            if not detail_href or not case_number:
                continue

            # ── Detail fetch ───────────────────────────────────────────────────
            await asyncio.sleep(_REQ_DELAY_SEC)
            try:
                detail_resp = await client.get(_DETAIL_BASE + detail_href)
                detail_resp.raise_for_status()
                detail_html = detail_resp.text
            except Exception as exc:
                logger.debug(
                    "DaytonProbateRecheck: casenbr=%d detail fetch failed: %s", nbr, exc
                )
                continue

            detail = _parse_detail(detail_html)
            if not detail:
                continue

            case_status = detail.get("case_status")
            if not case_status:
                continue

            seen += 1
            n_rows = await _update_case_status(pool, case_number, case_status, dry_run) if pool else 0
            updated += n_rows

            if _is_closed(case_status):
                closed += 1
                logger.info(
                    "CLOSED: casenbr=%d case=%s status=%r (%d listing rows)",
                    nbr, case_number, case_status, n_rows,
                )

            if seen % 50 == 0:
                logger.info(
                    "...progress: %d cases checked, %d rows updated, %d closed (casenbr=%d)",
                    seen, updated, closed, nbr,
                )

    logger.info(
        "DaytonProbateRecheck done: %d cases seen, %d rows updated, %d CLOSED.",
        seen, updated, closed,
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="Dayton probate case_status re-check / backfill")
    parser.add_argument("--year", type=int, default=None,
                        help="Caseyear to walk (default: current year from cursor table)")
    parser.add_argument("--start", type=int, default=None,
                        help="First casenbr to check (inclusive)")
    parser.add_argument("--end", type=int, default=None,
                        help="Last casenbr to check (inclusive)")
    parser.add_argument("--backfill", action="store_true",
                        help="Walk 1..last_casenbr (from cursor) for the given year")
    parser.add_argument("--recent", type=int, default=None,
                        help="Walk last N ids before the cursor position")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and parse but skip DB writes")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        from app.config import settings
        database_url = settings.DATABASE_URL

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    try:
        # Read the cursor to get current caseyear + last_casenbr.
        cursor_year, cursor_nbr = await _read_cursor(pool)

        # --year overrides the caseyear; --backfill / --recent / explicit range set the span.
        caseyear = args.year if args.year is not None else cursor_year

        if args.start is not None and args.end is not None:
            start_nbr, end_nbr = args.start, args.end
        elif args.backfill:
            # For the current year use the cursor. For historic years default to 1..2000
            # (adjust via --start/--end for the actual frontier of that year).
            start_nbr = 1
            end_nbr = cursor_nbr if caseyear == cursor_year else 2000
        elif args.recent:
            ref = cursor_nbr if caseyear == cursor_year else 2000
            start_nbr = max(1, ref - args.recent)
            end_nbr = ref
        else:
            # Default: walk 1..cursor for the resolved caseyear.
            start_nbr = 1
            end_nbr = cursor_nbr if caseyear == cursor_year else 2000

        await recheck(caseyear, start_nbr, end_nbr, dry_run=args.dry_run, pool=pool)
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
