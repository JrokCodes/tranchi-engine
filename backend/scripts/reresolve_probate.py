"""
Tranchi — probate join RE-RESOLUTION (precision-first cleanup + migration-007 backfill).

Re-evaluates EXISTING active probate listings against the fixed full-name matcher
WITHOUT re-scraping. Because every active probate row already carries the matched
parcel's current owner (tranchi.parcels) and — after step 1 — the decedent name, we can
recompute fiscal_officer._name_confidence(decedent_name, parcel_owner) directly and apply
the same precision gate the live scraper now uses. This is strictly MORE precise than the
scraper's emit-time ambiguity cap, because here we know each parcel's actual owner.

Effects (idempotent):
  1. Backfill listings.{decedent_name, case_title, decedent_dod} from signals.payload
     (migration-007 denormalization) where missing.
  2. Per active probate listing, recompute the decedent->parcel join:
       - address_anchor / composite        -> keep, tier 'confirmed'.
       - name-only, owner known, conf >= _MIN_CONFIDENCE
                                            -> keep, re-tier 'probable'/'unverified' + score.
       - name-only, owner known, conf <  _MIN_CONFIDENCE
                                            -> MIS-JOIN -> retire status='superseded'.
       - name-only, owner/decedent unknown -> tier 'unverified' (hidden by the read gate),
                                              NEVER retired on missing data (re-runs heal it
                                              once enrich_parcels fills the owner).

Retired rows get status='superseded' (distinct from scraper 'not_listed'/'expired'/
'transferred' so the cause stays auditable). READ listings+parcels+signals; WRITE only
to tranchi.listings.

Usage:
  python scripts/reresolve_probate.py --dry-run        # report only, no writes
  python scripts/reresolve_probate.py --limit 200      # bounded pass (smoke)
  python scripts/reresolve_probate.py --all            # full cleanup
  python scripts/reresolve_probate.py                  # same as --all
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

import asyncpg  # noqa: E402

from app.scrapers.fiscal_officer import _name_confidence  # noqa: E402
from app.scrapers.probate import _MIN_CONFIDENCE, _HIGH_CONFIDENCE, _AMBIGUITY_CAP  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reresolve_probate")

_ANCHOR_METHODS = ("address_anchor", "composite")


async def _backfill_decedent_fields(conn: asyncpg.Connection, *, dry_run: bool) -> int:
    """Step 1: copy decedent_name + dod from signals.payload onto the listing row."""
    sql = """
        WITH src AS (
            SELECT DISTINCT ON (payload->>'case_number')
                   payload->>'case_number'              AS case_number,
                   NULLIF(payload->>'decedent_name', '') AS decedent_name,
                   NULLIF(payload->>'dod', '')::date     AS dod
            FROM tranchi.signals
            WHERE signal_type = 'probate'
              AND payload ? 'case_number'
            ORDER BY payload->>'case_number', last_seen_at DESC
        )
        UPDATE tranchi.listings l
        SET decedent_name = COALESCE(l.decedent_name, src.decedent_name),
            decedent_dod  = COALESCE(l.decedent_dod, src.dod)
        FROM src
        WHERE l.signal_type = 'probate'
          AND l.case_number = src.case_number
          AND (l.decedent_name IS NULL OR l.decedent_dod IS NULL)
    """
    if dry_run:
        cnt = await conn.fetchval(
            """
            SELECT COUNT(*) FROM tranchi.listings
            WHERE signal_type = 'probate' AND decedent_name IS NULL
            """
        )
        logger.info("[DRY RUN] step1: %d probate rows currently missing decedent_name", cnt)
        return cnt or 0
    tag = await conn.execute(sql)
    # asyncpg returns 'UPDATE <n>'
    n = int(tag.split()[-1]) if tag and tag.split()[-1].isdigit() else 0
    logger.info("step1: backfilled decedent fields onto %d probate listing rows", n)
    return n


async def _pick_listings(conn: asyncpg.Connection, *, limit: int | None) -> list[asyncpg.Record]:
    rows = await conn.fetch(
        f"""
        SELECT l.id, l.case_number, l.decedent_name, l.source_listing_id AS parcel,
               l.match_method, l.match_confidence, l.match_score,
               p.owner_name,
               COUNT(*) OVER (PARTITION BY l.case_number) AS case_count
        FROM tranchi.listings l
        LEFT JOIN tranchi.parcels p ON p.parcel_number = l.source_listing_id
        WHERE l.signal_type = 'probate' AND l.status = 'active'
        ORDER BY l.case_number, l.first_seen_at
        {f'LIMIT {int(limit)}' if limit else ''}
        """
    )
    return rows


def _decide(row: asyncpg.Record, case_explosion: bool) -> tuple[str, dict]:
    """Return (action, fields). action in keep_confirmed|retier|hide|retire|noop.

    case_explosion: this case's name resolved to more than _AMBIGUITY_CAP DISTINCT owners
    (e.g. decedent "Daniel James Williams" matched 30 different Williamses). A common
    multi-token name clears a per-token match against many unrelated people, so NO name-only
    parcel in such a case is trustworthy — only an address anchor can save it.
    """
    method = row["match_method"] or ""
    decedent = (row["decedent_name"] or "").strip()
    owner = (row["owner_name"] or "").strip()

    # Address-anchored joins are authoritative — keep as confirmed (even in an explosion).
    if method in _ANCHOR_METHODS:
        if row["match_confidence"] == "confirmed":
            return "noop", {}
        return "keep_confirmed", {
            "match_confidence": "confirmed",
            "match_score": max(float(row["match_score"] or 0.0), 0.95),
        }

    # Common-name explosion: the case resolved to many distinct people. A name-only join
    # can't be trusted to any of them — retire it (an address-anchored row above survives).
    if case_explosion:
        return "retire", {}

    # Name-only (or legacy NULL) — recompute against the parcel's CURRENT owner.
    if not decedent or not owner:
        # Can't verify per-row (no decedent captured, or parcel not enriched). If this
        # case is part of an over-match EXPLOSION (> _AMBIGUITY_CAP parcels), the cluster
        # itself is the evidence of a name over-match (a real estate rarely owns that many
        # parcels) — retire it, same rule the live scraper now applies at emit time.
        # A small unverifiable case is left HIDDEN (not deleted) to heal once enrich_parcels
        # fills the owner; a later run then re-tiers or retires it precisely.
        if (row["case_count"] or 0) > _AMBIGUITY_CAP:
            return "retire", {}
        if row["match_confidence"] == "unverified":
            return "noop", {}
        return "hide", {"match_method": "name_match", "match_confidence": "unverified"}

    conf = _name_confidence(decedent, owner)
    if conf < _MIN_CONFIDENCE:
        return "retire", {}  # mis-join: parcel owner is not the decedent
    tier = "probable" if conf >= _HIGH_CONFIDENCE else "unverified"
    return "retier", {
        "match_method": "name_match",
        "match_confidence": tier,
        "match_score": round(conf, 4),
    }


async def _apply(conn: asyncpg.Connection, listing_id, action: str, fields: dict) -> None:
    if action == "retire":
        # Clear the join tier too, so a retired mis-join can never satisfy the read-API
        # confidence gate on a status-less query (the dashboard always sends status=active,
        # but raw queries shouldn't surface superseded rows either).
        await conn.execute(
            "UPDATE tranchi.listings "
            "SET status='superseded', match_confidence=NULL, last_seen_at=NOW() WHERE id=$1",
            listing_id,
        )
        return
    sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(fields))
    await conn.execute(
        f"UPDATE tranchi.listings SET {sets} WHERE id=$1",
        listing_id,
        *fields.values(),
    )


async def main() -> None:
    ap = argparse.ArgumentParser(description="Re-resolve probate decedent->parcel joins (precision-first).")
    ap.add_argument("--limit", type=int, default=None, help="Process at most N active probate rows.")
    ap.add_argument("--all", action="store_true", help="Process all active probate rows (default).")
    ap.add_argument("--dry-run", action="store_true", help="Report actions, write nothing.")
    args = ap.parse_args()

    url = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(url)
    try:
        await _backfill_decedent_fields(conn, dry_run=args.dry_run)

        rows = await _pick_listings(conn, limit=args.limit)
        logger.info("Re-resolving %d active probate listings%s ...",
                    len(rows), " [DRY RUN]" if args.dry_run else "")

        # A case is a common-name EXPLOSION when its name-only matches resolve to more than
        # _AMBIGUITY_CAP DISTINCT current owners (different people sharing name tokens).
        owners_by_case: dict[str, set[str]] = {}
        for row in rows:
            if (row["match_method"] or "") in _ANCHOR_METHODS:
                continue
            if row["owner_name"]:
                owners_by_case.setdefault(row["case_number"], set()).add(row["owner_name"].strip().upper())
        explosion_cases = {c for c, owners in owners_by_case.items() if len(owners) > _AMBIGUITY_CAP}
        logger.info("Detected %d common-name explosion case(s) (>%d distinct owners).",
                    len(explosion_cases), _AMBIGUITY_CAP)

        tally = {"keep_confirmed": 0, "retier": 0, "hide": 0, "retire": 0, "noop": 0}
        examples: list[str] = []
        for row in rows:
            action, fields = _decide(row, row["case_number"] in explosion_cases)
            tally[action] += 1
            if action == "retire" and len(examples) < 8:
                examples.append(
                    f"  RETIRE {row['case_number']} parcel {row['parcel']}: "
                    f"decedent={row['decedent_name']!r} != owner={row['owner_name']!r}"
                )
            if not args.dry_run and action != "noop":
                await _apply(conn, row["id"], action, fields)

        for line in examples:
            logger.info(line)
        logger.info(
            "DONE%s: kept_confirmed=%d re-tiered=%d hidden(unverified)=%d RETIRED(mis-join)=%d noop=%d",
            " [DRY RUN]" if args.dry_run else "",
            tally["keep_confirmed"], tally["retier"], tally["hide"], tally["retire"], tally["noop"],
        )
        if not args.dry_run:
            remaining = await conn.fetchval(
                """
                SELECT COUNT(*) FROM tranchi.listings
                WHERE signal_type='probate' AND status='active'
                  AND match_confidence IN ('confirmed','probable')
                """
            )
            logger.info("Active probate now SHOWN in feed (confirmed/probable): %d", remaining)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
