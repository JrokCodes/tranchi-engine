"""
Tranchi Engine — Health Snapshot (self-verify in one command)

A read-only "is the data still valid right now?" snapshot. Designed for Jayden
to run any time without needing to remember which SQL to write. Pairs with the
nightly quality_audit (broader, alert-driven) and the /tranchi-verify skill
(per-listing sampling with Redfin/Zillow links).

INVARIANTS:
- READ-ONLY. No INSERT/UPDATE/DELETE.
- All counts come from tranchi.* live state, not cached numbers.

Usage:
  python scripts/health_snapshot.py            # full snapshot
  python scripts/health_snapshot.py --json     # machine-readable
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
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


_CHECKS = [
    # (label, SQL returning a single scalar; OK_IF describes the green condition)
    ("active_total",                "SELECT count(*) FROM tranchi.listings WHERE status='active'"),
    ("active_canonical (visible)",  "SELECT count(*) FROM tranchi.listings WHERE status='active' AND duplicate_of IS NULL"),
    ("duplicates_flagged_active",   "SELECT count(*) FROM tranchi.listings WHERE status='active' AND duplicate_of IS NOT NULL"),
    ("expired_total",               "SELECT count(*) FROM tranchi.listings WHERE status='expired'"),
    ("probate_active",              "SELECT count(*) FROM tranchi.listings WHERE status='active' AND signal_type='probate'"),
    ("probate_active_open",         "SELECT count(*) FROM tranchi.listings WHERE status='active' AND signal_type='probate' AND case_status='OPEN'"),
    ("probate_active_NOT_open",     "SELECT count(*) FROM tranchi.listings WHERE status='active' AND signal_type='probate' AND COALESCE(case_status,'') <> 'OPEN'"),
    ("tax_deed_active",             "SELECT count(*) FROM tranchi.listings WHERE status='active' AND signal_type='tax_delinquent_foreclosure'"),
    ("mortgage_active",             "SELECT count(*) FROM tranchi.listings WHERE status='active' AND signal_type='mortgage_foreclosure'"),
    ("landbank_active",             "SELECT count(*) FROM tranchi.listings WHERE status='active' AND signal_type='land_bank_inventory'"),
    ("no_street_number_flagged",    "SELECT count(*) FROM tranchi.listings WHERE status='active' AND address_status='no_street_number'"),
    ("parcels_total",               "SELECT count(*) FROM tranchi.parcels"),
    ("parcel_coverage_missing",     "SELECT count(*) FROM tranchi.listings l WHERE l.status='active' AND l.source_listing_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM tranchi.parcels p WHERE p.parcel_number=l.source_listing_id)"),
    ("signals_total",               "SELECT count(*) FROM tranchi.signals"),
]


async def run(args) -> int:
    url = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(url)
    try:
        snap: dict[str, object] = {"ts": datetime.now(timezone.utc).isoformat()}
        for label, sql in _CHECKS:
            snap[label] = await conn.fetchval(sql)

        # Last scrape per source
        runs = await conn.fetch(
            """
            SELECT source_site, MAX(started_at) AS last_run
            FROM tranchi.scrape_runs
            WHERE status='success'
            GROUP BY source_site
            ORDER BY source_site
            """
        )
        snap["last_run_by_source"] = {r["source_site"]: r["last_run"].isoformat() if r["last_run"] else None for r in runs}

        # Match-confidence distribution (probate)
        mc = await conn.fetch(
            """
            SELECT COALESCE(match_confidence,'(null)') AS tier, count(*) AS n
            FROM tranchi.listings
            WHERE status='active' AND signal_type='probate'
            GROUP BY match_confidence ORDER BY n DESC
            """
        )
        snap["probate_match_tiers"] = {r["tier"]: r["n"] for r in mc}
    finally:
        await conn.close()

    if args.json:
        print(json.dumps(snap, indent=2, default=str))
        return 0

    # Human-readable summary with traffic-light interpretation
    print(f"\n=== TRANCHI HEALTH SNAPSHOT — {snap['ts']} ===\n")
    print(f"  {'metric':<30} {'value'}")
    print(f"  {'-'*30} {'-'*40}")
    for label, _ in _CHECKS:
        v = snap[label]
        print(f"  {label:<30} {v}")

    print(f"\n  match-confidence tiers (probate active): {snap['probate_match_tiers']}")
    print(f"\n  Last successful scrape per source:")
    for src, ts in snap["last_run_by_source"].items():
        print(f"    {src:<35} {ts}")

    # Quick verdicts
    print("\n  Verdicts:")
    cov_missing = snap["parcel_coverage_missing"]
    pn_not_open = snap["probate_active_NOT_open"]
    print(f"    parcel coverage:        {'OK (100%)' if cov_missing == 0 else f'GAP ({cov_missing} active listings missing parcel)'}")
    print(f"    probate open-only rule: {'OK' if pn_not_open == 0 else f'LEAK ({pn_not_open} active probate NOT OPEN)'}")
    print(f"    duplicates filtered:    {snap['duplicates_flagged_active']} flagged (hidden by read API)")
    print(f"    no-street-number tag:   {snap['no_street_number_flagged']} flagged (verify by parcel #, not address)")
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Tranchi health snapshot (read-only)")
    ap.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
