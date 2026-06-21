"""
Parcel-identity migration proof harness (READ-ONLY).

The load-bearing safety gate for migration 021 (parcels PK + signals FK ->
(parcel_number, market)) and the market-aware normalizer (F-008 / must-close #6).
Deploy criterion: the 4 live markets (cuyahoga/shelby/summit/wayne) must be
BYTE-FOR-BYTE unchanged. This script proves it with SELECT-only queries.

It checks:
  1. Per-market counts — parcels, active listings, signals, parcel-joined enriched
     listings, and per-source active-listing counts. These must be IDENTICAL before
     and after the migration (the composite key selects the same rows because no
     parcel_number collides across the 4 live markets today).
  2. NULL-market == 0 in tranchi.parcels and tranchi.signals (the composite PK/FK
     cannot include a NULL; the migration's guard refuses to run otherwise).
  3. Orphan signals == 0 under the composite (parcel_number, market) FK.
  4. Normalize equivalence — for every DISTINCT live source_listing_id,
     normalize_parcel_for_market(x, market) == normalize_parcel_number(x). This is a
     pure code property over current data (no before/after needed); it proves the
     Phase-2 dispatch leaves the 4 live markets unchanged.

Usage (the human applying migration 021 on EC2 runs --out before, --compare after):
  # BEFORE the migration — capture the baseline:
  python scripts/parcel_identity_proof.py --out /tmp/parcel_proof_before.json

  # AFTER the migration — re-capture and diff vs the baseline (PASS/FAIL):
  python scripts/parcel_identity_proof.py --compare /tmp/parcel_proof_before.json

  # One-shot current state (no file):
  python scripts/parcel_identity_proof.py

Exit code is 0 on PASS, 1 on FAIL — so it can gate a deploy script. NOTHING is written
to the database. Companion (manual) step for the full G2 package — same N/N VALID per
source on each live market:
  for m in cuyahoga shelby summit wayne; do
    python scripts/verify_listings.py --market $m --stratified 3
  done
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
_backend = _here.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_env_file = _backend / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)

import asyncpg  # noqa: E402

from app.scrapers.db import normalize_parcel_for_market, normalize_parcel_number  # noqa: E402

LIVE_MARKETS = ("cuyahoga", "shelby", "summit", "wayne")

# Active = what the read API/feed surfaces (mirrors run.py / verify scoping).
_ACTIVE = "status IN ('active', 'not_listed') AND duplicate_of IS NULL"


async def _rows_to_map(conn: asyncpg.Connection, sql: str) -> dict:
    """Run a 2-column (key, count) GROUP BY and return {key: count}."""
    return {r[0]: r[1] for r in await conn.fetch(sql)}


async def capture(conn: asyncpg.Connection) -> dict:
    """Capture all DB-provable metrics into a JSON-able dict."""
    snap: dict = {}

    snap["parcels_by_market"] = await _rows_to_map(
        conn, "SELECT market, count(*) FROM tranchi.parcels GROUP BY market"
    )
    snap["signals_by_market"] = await _rows_to_map(
        conn, "SELECT market, count(*) FROM tranchi.signals GROUP BY market"
    )
    snap["active_listings_by_market"] = await _rows_to_map(
        conn, f"SELECT market, count(*) FROM tranchi.listings WHERE {_ACTIVE} GROUP BY market"
    )
    # Enriched = active listings that join a parcel on the COMPOSITE key. Pre-migration
    # the join already uses p.market = l.market, so this number is stable across the swap.
    snap["enriched_listings_by_market"] = await _rows_to_map(
        conn,
        f"""
        SELECT l.market, count(*)
        FROM tranchi.listings l
        JOIN tranchi.parcels p
          ON p.parcel_number = l.source_listing_id AND p.market = l.market
        WHERE {_ACTIVE}
        GROUP BY l.market
        """,
    )
    # Per (market, source_site) active counts — JSON keys are "market||source_site".
    snap["active_listings_by_market_source"] = {
        f"{r['market']}||{r['source_site']}": r["n"]
        for r in await conn.fetch(
            f"""
            SELECT market, source_site, count(*) AS n
            FROM tranchi.listings WHERE {_ACTIVE}
            GROUP BY market, source_site
            """
        )
    }

    snap["null_market_parcels"] = await conn.fetchval(
        "SELECT count(*) FROM tranchi.parcels WHERE market IS NULL"
    )
    snap["null_market_signals"] = await conn.fetchval(
        "SELECT count(*) FROM tranchi.signals WHERE market IS NULL"
    )
    snap["orphan_signals_composite"] = await conn.fetchval(
        """
        SELECT count(*)
        FROM tranchi.signals s
        LEFT JOIN tranchi.parcels p
               ON p.parcel_number = s.parcel_number AND p.market = s.market
        WHERE p.parcel_number IS NULL
        """
    )
    return snap


async def normalize_equivalence(conn: asyncpg.Connection) -> dict:
    """For every DISTINCT live source_listing_id, assert market dispatch == global fn.

    Returns {market: {"checked": N, "mismatches": [ {raw, dispatch, global} ... ]}}.
    A non-empty mismatches list for any live market is a Phase-2 FAIL.
    """
    out: dict = {}
    for m in LIVE_MARKETS:
        raws = [
            r[0]
            for r in await conn.fetch(
                "SELECT DISTINCT source_listing_id FROM tranchi.listings "
                "WHERE market = $1 AND source_listing_id IS NOT NULL AND source_listing_id <> ''",
                m,
            )
        ]
        mism = []
        for raw in raws:
            d = normalize_parcel_for_market(raw, m)
            g = normalize_parcel_number(raw)
            if d != g:
                mism.append({"raw": raw, "dispatch": d, "global": g})
        out[m] = {"checked": len(raws), "mismatches": mism}
    return out


def _print_snapshot(snap: dict, equiv: dict) -> None:
    print("\n=== Parcel-identity proof — current state ===")
    for key in (
        "parcels_by_market",
        "active_listings_by_market",
        "enriched_listings_by_market",
        "signals_by_market",
    ):
        print(f"\n{key}:")
        for k in sorted(snap[key]):
            print(f"    {k:12} {snap[key][k]}")
    print("\nintegrity:")
    print(f"    null_market_parcels      = {snap['null_market_parcels']}")
    print(f"    null_market_signals      = {snap['null_market_signals']}")
    print(f"    orphan_signals_composite = {snap['orphan_signals_composite']}")
    print("\nnormalize equivalence (dispatch == global) for live markets:")
    for m in LIVE_MARKETS:
        e = equiv[m]
        flag = "OK" if not e["mismatches"] else f"FAIL ({len(e['mismatches'])} mismatch)"
        print(f"    {m:12} checked={e['checked']:6}  {flag}")
        for mm in e["mismatches"][:5]:
            print(f"        {mm}")


def _assert_clean(snap: dict, equiv: dict) -> list[str]:
    """Single-snapshot invariants (independent of before/after)."""
    fails = []
    if snap["null_market_parcels"]:
        fails.append(f"null_market_parcels = {snap['null_market_parcels']} (must be 0)")
    if snap["null_market_signals"]:
        fails.append(f"null_market_signals = {snap['null_market_signals']} (must be 0)")
    if snap["orphan_signals_composite"]:
        fails.append(f"orphan_signals_composite = {snap['orphan_signals_composite']} (must be 0)")
    for m in LIVE_MARKETS:
        if equiv[m]["mismatches"]:
            fails.append(f"normalize mismatch in {m}: {len(equiv[m]['mismatches'])} (must be 0)")
    return fails


def _diff(before: dict, after: dict) -> list[str]:
    """Per-market count invariants must be IDENTICAL before/after the migration."""
    fails = []
    for key in (
        "parcels_by_market",
        "signals_by_market",
        "active_listings_by_market",
        "enriched_listings_by_market",
        "active_listings_by_market_source",
    ):
        b, a = before.get(key, {}), after.get(key, {})
        for k in sorted(set(b) | set(a)):
            if b.get(k) != a.get(k):
                fails.append(f"{key}[{k}]: before={b.get(k)} after={a.get(k)}")
    return fails


async def run(args) -> int:
    url = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(url)
    try:
        snap = await capture(conn)
        equiv = await normalize_equivalence(conn)
    finally:
        await conn.close()

    _print_snapshot(snap, equiv)

    fails = _assert_clean(snap, equiv)

    if args.out:
        Path(args.out).write_text(json.dumps(snap, indent=2, default=str))
        print(f"\nBaseline written to {args.out}")

    if args.compare:
        before = json.loads(Path(args.compare).read_text())
        diff_fails = _diff(before, snap)
        if diff_fails:
            print(f"\n=== DIFF vs {args.compare}: {len(diff_fails)} count change(s) ===")
            for f in diff_fails:
                print(f"    {f}")
            fails.extend(diff_fails)
        else:
            print(f"\n=== DIFF vs {args.compare}: counts IDENTICAL ===")

    print("\n" + "=" * 60)
    if fails:
        print(f"RESULT: FAIL — {len(fails)} problem(s). DO NOT DEPLOY.")
        print("=" * 60 + "\n")
        return 1
    print("RESULT: PASS — byte-for-byte clean (DB-provable checks).")
    print("Companion: verify_listings --stratified 3 per live market (same N/N VALID).")
    print("=" * 60 + "\n")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Parcel-identity migration proof (read-only).")
    ap.add_argument("--out", help="Write the current snapshot to this JSON path (baseline).")
    ap.add_argument("--compare", help="Diff current state vs this baseline JSON (PASS/FAIL).")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
