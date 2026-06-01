"""
Tranchi — recover decedent name (+ address-anchor) for legacy probate rows.

The ~440 active probate cases whose `decedent_name` is NULL (their signal was FK-dropped,
so the court-side name was never persisted) can't be verified by the re-judge. This script
re-fetches each case from the ProWare Case Search (year/category/suffix), reads the DECEDENT
row straight off the results grid (name + address — no sub-page drill needed), and writes
`decedent_name` onto every listing for that case. If the decedent's address matches the
listing's own property_address, it ALSO sets match_method='address_anchor' /
match_confidence='confirmed' (the high-confidence win) — otherwise it leaves the tier for
`reresolve_probate.py` to judge by name vs the parcel owner.

READ probate court (1 req/sec ToS via ProwareSession); WRITE only tranchi.listings.
Usage:
  python scripts/backfill_probate_decedent.py --dry-run --limit 5     # smoke (eyeball)
  python scripts/backfill_probate_decedent.py --limit 50              # bounded
  python scripts/backfill_probate_decedent.py                         # all (~440 cases)
Then run: python scripts/reresolve_probate.py --all   (re-judge / retire / show)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
_backend = _here.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

_env = _backend / ".env"
if _env.exists():
    from dotenv import load_dotenv
    load_dotenv(_env)

import asyncpg            # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from app.scrapers.proware_client import ProwareSession  # noqa: E402
from app.scrapers.probate import _BASE_URL, _TERMS_PATH, _AGREE_BUTTON, _SEARCH_PATH  # noqa: E402
from app.scrapers.db import canonical_address, normalize_address  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_probate_decedent")

_GRID_ID = "mpContentPH_gvSearchResults"


def _split_case(case_number: str) -> tuple[str, str, str] | None:
    """'2026EST306363' -> ('2026','EST','306363'). None if unparseable."""
    c = (case_number or "").strip().upper()
    i = 0
    while i < len(c) and c[i].isdigit():
        i += 1
    year, rest = c[:i], c[i:]
    if len(year) != 4 or not rest:
        return None
    j = 0
    while j < len(rest) and rest[j].isalpha():
        j += 1
    cat, suffix = rest[:j], rest[j:]
    if not cat or not suffix.isdigit():
        return None
    return year, cat, suffix


def _norm(addr: str | None) -> str | None:
    if not addr:
        return None
    return normalize_address(canonical_address(addr) or "") or None


def _addr_match(a: str | None, b: str | None) -> bool:
    """Tolerant: normalized equal, or one is a prefix of the other (house# + street)."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    return na == nb or na.startswith(nb) or nb.startswith(na)


def _parse_decedent_from_grid(html: str) -> tuple[str | None, str | None]:
    """Read the DECEDENT row off the Case Search results grid: (name, address)."""
    soup = BeautifulSoup(html, "lxml")
    grid = soup.find("table", id=_GRID_ID)
    if grid is None:
        return None, None
    for tr in grid.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        # Columns: Name | Case Number | Address | Role | Alias
        if len(cells) >= 4 and "DECEDENT" in cells[3].upper():
            name = cells[0].title() if cells[0] else None
            addr = cells[2] or None
            return name, addr
    return None, None


async def _search(session: ProwareSession, year: str, cat: str, suffix: str) -> str:
    st = await session.fetch_form_state(_SEARCH_PATH)
    html, _ = await session.post_back(_SEARCH_PATH, extra_fields={
        "ctl00$mpContentPH$txtCaseYear": year,
        "ctl00$mpContentPH$ddlCaseCat": cat,
        "ctl00$mpContentPH$txtCaseNum": suffix,
        "ctl00$mpContentPH$btnSearchByCase": "Search",
    }, viewstate=st)
    return html


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Max distinct cases to process.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    rows = await conn.fetch(
        "SELECT id, case_number, property_address "
        "FROM tranchi.listings "
        "WHERE signal_type='probate' AND status='active' AND decedent_name IS NULL "
        "  AND case_number IS NOT NULL "
        "ORDER BY case_number"
    )
    # group rows by case
    by_case: dict[str, list] = {}
    for r in rows:
        by_case.setdefault(r["case_number"], []).append(r)
    cases = list(by_case.keys())
    if args.limit:
        cases = cases[:args.limit]
    logger.info("%d rows across %d cases; processing %d cases%s",
                len(rows), len(by_case), len(cases), " [DRY RUN]" if args.dry_run else "")

    recovered = no_result = unparseable = anchored = name_only = 0
    async with ProwareSession(_BASE_URL, rate_limit_sec=1.0) as session:
        await session.accept_agreement(path=_TERMS_PATH, agree_button_id=_AGREE_BUTTON)
        for ci, case in enumerate(cases, 1):
            parts = _split_case(case)
            if not parts:
                unparseable += 1
                continue
            try:
                html = await _search(session, *parts)
                dec_name, dec_addr = _parse_decedent_from_grid(html)
            except Exception as exc:
                logger.warning("case %s search error: %s", case, exc)
                no_result += 1
                continue
            if not dec_name:
                no_result += 1
                continue
            recovered += 1
            for r in by_case[case]:
                is_anchor = _addr_match(dec_addr, r["property_address"])
                if is_anchor:
                    anchored += 1
                else:
                    name_only += 1
                if args.dry_run:
                    if ci <= 6:
                        logger.info("  %s | decedent=%r addr=%r | listing_addr=%r | %s",
                                    case, dec_name, dec_addr, r["property_address"],
                                    "ADDRESS-ANCHOR->confirmed" if is_anchor else "name-only (reresolve will judge)")
                    continue
                if is_anchor:
                    await conn.execute(
                        "UPDATE tranchi.listings SET decedent_name=$2, "
                        "match_method='address_anchor', match_confidence='confirmed', "
                        "match_score=GREATEST(COALESCE(match_score,0),0.96) WHERE id=$1",
                        r["id"], dec_name,
                    )
                else:
                    await conn.execute(
                        "UPDATE tranchi.listings SET decedent_name=$2 WHERE id=$1",
                        r["id"], dec_name,
                    )
            if ci % 25 == 0:
                logger.info("  ... %d/%d cases (recovered=%d, no_result=%d)", ci, len(cases), recovered, no_result)

    await conn.close()
    logger.info(
        "DONE%s: cases=%d recovered=%d no_result=%d unparseable=%d | rows anchored=%d name_only=%d",
        " [DRY RUN]" if args.dry_run else "", len(cases), recovered, no_result, unparseable, anchored, name_only,
    )
    if not args.dry_run:
        logger.info("Next: python scripts/reresolve_probate.py --all  (re-judge: show address-anchored + name-matches, retire the rest)")


if __name__ == "__main__":
    asyncio.run(main())
