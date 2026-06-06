"""
Tranchi Engine — Nightly Data-Quality Audit

Runs 7 rule-based checks against tranchi.listings + tranchi.signals +
tranchi.scrape_runs and sends a Telegram digest via @intelleq_monitor_bot.

Usage:
  python scripts/quality_audit.py            # print digest + Telegram
  python scripts/quality_audit.py --dry-run  # print digest only, no Telegram

Cron (proposed):
  55 7 * * 1-5   ... quality_audit.py   (nightly 7:55 AM UTC, Mon-Fri)

INVARIANTS:
- This script is READ-ONLY against tranchi.* — no INSERT/UPDATE/DELETE.
- Staleness checks for FULL_RESCAN sources only (DLN, Land Bank). CURSOR
  sources (Probate) must NOT be flagged for last_seen_at age — that was the
  May 2026 bug that wrongly retired 6,446 probate cases. Policy gated via
  app/scrapers/staleness.py::policy_for().
- Telegram is optional/non-fatal. Missing secret file → log-only + TODO note.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap: allow `python scripts/quality_audit.py` from backend/
# ─────────────────────────────────────────────────────────────────────────────
_here = Path(__file__).resolve().parent
_backend = _here.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

_env_file = _backend / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)

import asyncpg  # noqa: E402

from app.scrapers.staleness import StalenessPolicy, policy_for, SOURCE_STALENESS  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] quality_audit: %(message)s",
)
logger = logging.getLogger("quality_audit")

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

import os
DATABASE_URL: str = os.environ["DATABASE_URL"]

# Telegram — wire once /home/ubuntu/.secrets/tranchi/ is provisioned.
# TODO: create /home/ubuntu/.secrets/tranchi/telegram-bot-token on EC2 to
#       enable Telegram digests. Until then, this script logs-only.
_TELEGRAM_TOKEN_PATH = Path("/home/ubuntu/.secrets/tranchi/telegram-bot-token")
_TELEGRAM_CHAT_ID = "8360510944"  # @intelleq_monitor_bot

# Tunables
_STALE_ACTIVE_HOURS = 7       # FULL_RESCAN listings not seen in ≥2 scrape cycles (~3h each)
_SIGNAL_STALE_DAYS = 45       # code_violation signals unchecked for this long → re-check candidates
_COVERAGE_DRIFT_PCT = 0.15    # flag source if DB active deviates >15% from last scrape_runs.active

# Probate case statuses that mean "this case is closed — listing should not be active"
_CLOSED_CASE_STATUSES = {"CLOSED", "DISPOSED", "TERMINATED"}


# ─────────────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────────────

def _send_telegram(message: str) -> None:
    if not _TELEGRAM_TOKEN_PATH.exists():
        # TODO: provision /home/ubuntu/.secrets/tranchi/telegram-bot-token to enable alerts.
        logger.warning(
            "Telegram token not found at %s — digest logged only. "
            "Create the file with the bot token to enable Telegram alerts.",
            _TELEGRAM_TOKEN_PATH,
        )
        return
    try:
        import httpx
        token = _TELEGRAM_TOKEN_PATH.read_text().strip()
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": _TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
        r.raise_for_status()
        logger.info("Telegram digest sent.")
    except Exception as exc:
        logger.error("Telegram send failed (non-fatal): %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Checks — all read-only SELECTs
# ─────────────────────────────────────────────────────────────────────────────

async def check_same_source_dupes(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """
    Clusters where (source_site, source_listing_id) OR (source_site,
    normalized_address) has >1 canonical (duplicate_of IS NULL) active row.

    Catches cases where the upsert key collapsed two real listings and the
    dedup pass let them through. Expected count = 0 for source_listing_id
    clusters; normalized_address clusters may appear for Probate (same parcel,
    two open cases).
    """
    # (source_site, source_listing_id) clusters
    id_dupes = await conn.fetch("""
        SELECT
            source_site,
            source_listing_id,
            COUNT(*) AS n,
            array_agg(id::text ORDER BY first_seen_at) AS listing_ids,
            array_agg(property_address ORDER BY first_seen_at) AS addresses
        FROM tranchi.listings
        WHERE status = 'active'
          AND duplicate_of IS NULL
          AND source_listing_id IS NOT NULL
        GROUP BY source_site, source_listing_id
        HAVING COUNT(*) > 1
    """)

    # (source_site, normalized_address) clusters
    addr_dupes = await conn.fetch("""
        SELECT
            source_site,
            normalized_address,
            COUNT(*) AS n,
            array_agg(id::text ORDER BY first_seen_at) AS listing_ids,
            array_agg(property_address ORDER BY first_seen_at) AS addresses
        FROM tranchi.listings
        WHERE status = 'active'
          AND duplicate_of IS NULL
          AND normalized_address IS NOT NULL
        GROUP BY source_site, normalized_address
        HAVING COUNT(*) > 1
    """)

    findings: list[dict[str, Any]] = []
    for r in id_dupes:
        findings.append({
            "dupe_key": "source_listing_id",
            "source": r["source_site"],
            "key_value": r["source_listing_id"],
            "count": r["n"],
            "listing_ids": r["listing_ids"],
            "addresses": r["addresses"],
        })
    for r in addr_dupes:
        findings.append({
            "dupe_key": "normalized_address",
            "source": r["source_site"],
            "key_value": r["normalized_address"],
            "count": r["n"],
            "listing_ids": r["listing_ids"],
            "addresses": r["addresses"],
        })
    return findings


async def check_past_sale_not_expired(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """
    Active listings whose sale_date is in the past.

    The expiry sweep (run.py _mark_expired_listings) should flip these on the
    3h cron cycle. Any count > 0 means the expiry sweep stopped or regressed.
    """
    rows = await conn.fetch("""
        SELECT
            id::text,
            source_site,
            property_address,
            sale_date,
            status
        FROM tranchi.listings
        WHERE sale_date < CURRENT_DATE
          AND status = 'active'
          AND duplicate_of IS NULL
    """)
    return [{
        "listing_id": r["id"],
        "source": r["source_site"],
        "address": r["property_address"],
        "sale_date": r["sale_date"].isoformat() if r["sale_date"] else None,
        "status": r["status"],
    } for r in rows]


async def check_stale_active_leak(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """
    Active listings from FULL_RESCAN sources not touched in ≥2 scrape cycles
    (~7h). These should have been marked not_listed if truly absent from the
    live feed.

    CURSOR (Probate) and ARCHIVE (Sheriff Sales history) sources are explicitly
    excluded — staleness by time is not meaningful for them. See staleness.py.
    """
    # Market-aware: derive ALL FULL_RESCAN sources from staleness.py (both Cuyahoga + Shelby,
    # and any future source) instead of a hardcoded OH-only list.
    full_rescan_sources = [
        site for site, policy in SOURCE_STALENESS.items()
        if policy == StalenessPolicy.FULL_RESCAN
    ]
    if not full_rescan_sources:
        return []

    placeholders = ", ".join(f"${i+1}" for i in range(len(full_rescan_sources)))
    rows = await conn.fetch(f"""
        SELECT
            id::text,
            source_site,
            property_address,
            last_seen_at,
            sale_date
        FROM tranchi.listings
        WHERE status = 'active'
          AND duplicate_of IS NULL
          AND source_site IN ({placeholders})
          AND last_seen_at < NOW() - INTERVAL '{_STALE_ACTIVE_HOURS} hours'
          -- mirror run.py::_mark_stale_listings: a TN tax_deed parcel legitimately leaves
          -- the pre-sale catalog once it sells / enters redemption, so absence != stale leak.
          AND NOT (signal_type = 'tax_deed'
                   AND (redemption_status = 'pending' OR sale_date < CURRENT_DATE))
    """, *full_rescan_sources)
    return [{
        "listing_id": r["id"],
        "source": r["source_site"],
        "address": r["property_address"],
        "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
        "sale_date": r["sale_date"].isoformat() if r["sale_date"] else None,
    } for r in rows]


async def check_address_completeness(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """
    Active canonical listings missing property_address or property_city.

    A NULL or empty address blocks normalized_address generation and breaks
    cross-source dedup and parcel join.
    """
    rows = await conn.fetch("""
        SELECT
            id::text,
            source_site,
            property_address,
            property_city,
            property_zip
        FROM tranchi.listings
        WHERE status = 'active'
          AND duplicate_of IS NULL
          AND (
              property_address IS NULL OR trim(property_address) = ''
              OR property_city IS NULL OR trim(property_city) = ''
          )
    """)
    return [{
        "listing_id": r["id"],
        "source": r["source_site"],
        "address": r["property_address"],
        "city": r["property_city"],
        "zip": r["property_zip"],
        "reasons": (
            (["missing_address"] if not (r["property_address"] or "").strip() else [])
            + (["missing_city"] if not (r["property_city"] or "").strip() else [])
        ),
    } for r in rows]


async def check_probate_validity(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """
    Two sub-checks on active Probate Court listings:

    1. case_status is a closed-state value (CLOSED/DISPOSED/TERMINATED) but
       listing is still active — the probate status sweep should have retired
       these. Count > 0 = retirement pipeline broken.

    2. match_confidence tier distribution — unverified or NULL-confidence
       listings are higher-risk for false positives. Surfaced for visibility.
    """
    findings: list[dict[str, Any]] = []

    # Sub-check 1: closed cases still active
    closed_rows = await conn.fetch("""
        SELECT
            id::text,
            source_site,
            property_address,
            case_number,
            case_status,
            case_status_date
        FROM tranchi.listings
        WHERE signal_type = 'probate'
          AND status = 'active'
          AND duplicate_of IS NULL
          -- market-aware: Cuyahoga stores a bare code ('CLOSED'); Shelby stores
          -- 'CODE - LABEL' (e.g. 'CLOSED - CLOSED'). Match either side so both formats hit.
          AND (split_part(upper(case_status), ' - ', 1) = ANY($1::text[])
               OR split_part(upper(case_status), ' - ', 2) = ANY($1::text[]))
    """, list(_CLOSED_CASE_STATUSES))
    for r in closed_rows:
        findings.append({
            "sub_check": "closed_case_still_active",
            "source": r["source_site"],
            "listing_id": r["id"],
            "address": r["property_address"],
            "case_number": r["case_number"],
            "case_status": r["case_status"],
            "case_status_date": r["case_status_date"].isoformat() if r["case_status_date"] else None,
        })

    # Sub-check 2: match_confidence tier summary (informational — never an alert on its own)
    tier_rows = await conn.fetch("""
        SELECT
            source_site,
            COALESCE(match_confidence, 'NULL') AS tier,
            COUNT(*) AS n
        FROM tranchi.listings
        WHERE signal_type = 'probate'
          AND status = 'active'
          AND duplicate_of IS NULL
        GROUP BY source_site, tier
        ORDER BY source_site, n DESC
    """)
    tiers_by_source: dict[str, dict[str, int]] = {}
    for r in tier_rows:
        tiers_by_source.setdefault(r["source_site"], {})[r["tier"]] = r["n"]
    findings.append({
        "sub_check": "match_confidence_summary",
        "tiers_by_source": tiers_by_source,
    })
    return findings


async def check_probate_join_sanity(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """
    Detect a regression of the probate decedent->parcel mis-join bug (the one that
    attached one common surname to 775 parcels). See Babel reference/JOIN-PRECISION.md.

    Flags:
      1. "overmatched_case" — an active probate case attached to more than
         _OVERMATCH_CAP distinct parcels. After the precision-first matcher + the
         reresolve_probate cleanup this should be ~0; a reappearance means the matcher
         regressed (over-matching on name again). ALERTS.
      2. "shown_without_decedent" — count of probate listings SHOWN in the feed
         (match_confidence in confirmed/probable) that have NO decedent_name on the row,
         so the decedent-vs-owner check can't run. Informational (small number expected
         while the migration-007 backfill catches up).
    """
    _OVERMATCH_CAP = 8  # a real person rarely owns >8 parcels; explosions were 100s
    findings: list[dict[str, Any]] = []

    # Market-aware: covers both Cuyahoga + Shelby probate (group by source so a case_number
    # that collides across markets isn't merged). Catches the condo/multi-unit address-anchor
    # explosion in EITHER market (the 2026-06-06 fix prevents it; this is the daily tripwire).
    overmatched = await conn.fetch("""
        SELECT source_site, case_number, COUNT(DISTINCT source_listing_id) AS n
        FROM tranchi.listings
        WHERE signal_type = 'probate'
          AND status = 'active'
          AND duplicate_of IS NULL
        GROUP BY source_site, case_number
        HAVING COUNT(DISTINCT source_listing_id) > $1
        ORDER BY n DESC
    """, _OVERMATCH_CAP)
    for r in overmatched:
        findings.append({
            "type": "overmatched_case",
            "source": r["source_site"],
            "case_number": r["case_number"],
            "parcel_count": r["n"],
            "cap": _OVERMATCH_CAP,
        })

    shown_no_decedent = await conn.fetchval("""
        SELECT COUNT(*) FROM tranchi.listings
        WHERE signal_type = 'probate'
          AND status = 'active'
          AND duplicate_of IS NULL
          AND match_confidence IN ('confirmed', 'probable')
          AND decedent_name IS NULL
    """)
    findings.append({"type": "shown_without_decedent", "count": shown_no_decedent or 0})
    return findings


async def check_signal_freshness(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """
    code_violation signals with status='open' last observed more than
    _SIGNAL_STALE_DAYS ago — candidates for a closure re-check against the
    Cleveland violations API.

    These aren't necessarily bad data, but stale open violations that were
    resolved months ago inflate the signal quality score. Surface for triage.
    """
    rows = await conn.fetch(f"""
        SELECT
            id::text,
            parcel_number,
            source,
            observed_at,
            payload->>'address' AS address
        FROM tranchi.signals
        WHERE signal_type = 'code_violation'
          AND status = 'open'
          AND observed_at < NOW() - INTERVAL '{_SIGNAL_STALE_DAYS} days'
        ORDER BY observed_at ASC
        LIMIT 2000
    """)
    total = await conn.fetchval(f"""
        SELECT COUNT(*)
        FROM tranchi.signals
        WHERE signal_type = 'code_violation'
          AND status = 'open'
          AND observed_at < NOW() - INTERVAL '{_SIGNAL_STALE_DAYS} days'
    """)
    # Return aggregate + first few examples
    sample = [{
        "signal_id": r["id"],
        "parcel_number": r["parcel_number"],
        "source": r["source"],
        "observed_at": r["observed_at"].isoformat() if r["observed_at"] else None,
        "address": r["address"],
    } for r in rows[:5]]
    return [{
        "total_stale": total,
        "stale_threshold_days": _SIGNAL_STALE_DAYS,
        "sample": sample,
    }]


async def check_coverage_delta(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """
    For each source, compare the latest scrape_runs.active snapshot against the
    current DB active count. A large drop (>_COVERAGE_DRIFT_PCT) suggests the
    expiry sweep over-retired, a scraper died mid-run, or data was manually
    deleted.

    Only compares sources that appear in tranchi.listings (excludes signal-only
    sources like "Cleveland Code Violations" and "Cuyahoga Fiscal Officer").
    """
    # Latest scrape run per listing source
    run_rows = await conn.fetch("""
        SELECT DISTINCT ON (source_site)
            source_site,
            active  AS run_active,
            found   AS run_found,
            started_at
        FROM tranchi.scrape_runs
        WHERE source_site IN (
            SELECT DISTINCT source_site FROM tranchi.listings
        )
        ORDER BY source_site, started_at DESC
    """)
    # Current DB active count per source
    db_rows = await conn.fetch("""
        SELECT source_site, COUNT(*) AS db_active
        FROM tranchi.listings
        WHERE status = 'active' AND duplicate_of IS NULL
        GROUP BY source_site
    """)
    db_map = {r["source_site"]: r["db_active"] for r in db_rows}

    findings: list[dict[str, Any]] = []
    for r in run_rows:
        site = r["source_site"]
        run_active = r["run_active"] or 0
        db_active = db_map.get(site, 0)
        if run_active == 0:
            # Can't compute meaningful drift if last run logged 0 active
            continue
        drift = abs(db_active - run_active) / run_active
        flagged = drift > _COVERAGE_DRIFT_PCT
        findings.append({
            "source": site,
            "run_active": run_active,
            "db_active": db_active,
            "drift_pct": round(drift * 100, 1),
            "flagged": flagged,
            "last_run_at": r["started_at"].isoformat() if r["started_at"] else None,
        })
    return findings


# ─────────────────────────────────────────────────────────────────────────────
# Digest formatting
# ─────────────────────────────────────────────────────────────────────────────

def _format_digest(findings: dict[str, list[dict[str, Any]]]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [f"Tranchi Quality Audit — {now}"]
    lines.append("=" * 50)

    # 1. same_source_dupes
    dupes = findings["same_source_dupes"]
    id_dupes = [d for d in dupes if d["dupe_key"] == "source_listing_id"]
    addr_dupes = [d for d in dupes if d["dupe_key"] == "normalized_address"]
    lines.append(
        f"\n[same_source_dupes] "
        f"{len(id_dupes)} source_listing_id clusters, "
        f"{len(addr_dupes)} normalized_address clusters"
    )
    for d in (id_dupes + addr_dupes)[:3]:
        lines.append(
            f"  - [{d['source']}] {d['key_value']} x{d['count']}: "
            f"{', '.join((d['addresses'] or [])[:2])}"
        )
    if len(dupes) > 3:
        lines.append(f"  ...and {len(dupes) - 3} more")

    # 2. past_sale_not_expired
    pse = findings["past_sale_not_expired"]
    lines.append(f"\n[past_sale_not_expired] {len(pse)} listings (expected 0)")
    for r in pse[:3]:
        lines.append(f"  - [{r['source']}] {r['address']} sale={r['sale_date']} status={r['status']}")

    # 3. stale_active_leak
    stale = findings["stale_active_leak"]
    lines.append(f"\n[stale_active_leak] {len(stale)} FULL_RESCAN listings not seen in >7h (expected 0)")
    for r in stale[:3]:
        lines.append(f"  - [{r['source']}] {r['address']} last_seen={r['last_seen_at'][:16]}")
    if len(stale) > 3:
        lines.append(f"  ...and {len(stale) - 3} more")

    # 4. address_completeness
    addr = findings["address_completeness"]
    lines.append(f"\n[address_completeness] {len(addr)} active listings missing address/city")
    for r in addr[:3]:
        lines.append(f"  - [{r['source']}] id={r['listing_id'][:8]}... reasons={r['reasons']}")

    # 5. probate_validity
    pv = findings["probate_validity"]
    closed = [x for x in pv if x.get("sub_check") == "closed_case_still_active"]
    conf_summary = next((x for x in pv if x.get("sub_check") == "match_confidence_summary"), {})
    lines.append(f"\n[probate_validity] {len(closed)} closed-case listings still active (expected 0)")
    for r in closed[:3]:
        lines.append(
            f"  - {r['address']} case={r['case_number']} "
            f"status={r['case_status']} date={r['case_status_date']}"
        )
    tiers = conf_summary.get("tiers", {})
    if tiers:
        tier_str = "  confidence tiers: " + ", ".join(f"{k}={v}" for k, v in tiers.items())
        lines.append(tier_str)

    # 5b. probate_join_sanity
    pj = findings.get("probate_join_sanity", [])
    overmatched = [x for x in pj if x.get("type") == "overmatched_case"]
    no_dec = next((x for x in pj if x.get("type") == "shown_without_decedent"), {})
    lines.append(
        f"\n[probate_join_sanity] {len(overmatched)} over-matched case(s) "
        f"(>{overmatched[0]['cap'] if overmatched else 8} parcels — expected 0); "
        f"{no_dec.get('count', 0)} shown rows missing decedent_name"
    )
    for r in overmatched[:5]:
        lines.append(f"  - {r['case_number']}: {r['parcel_count']} parcels")

    # 6. signal_freshness
    sf = findings["signal_freshness"]
    if sf:
        rec = sf[0]
        lines.append(
            f"\n[signal_freshness] {rec['total_stale']} code_violation signals "
            f"open but not seen in >{rec['stale_threshold_days']}d "
            f"(re-check candidates)"
        )
        for s in rec.get("sample", [])[:2]:
            lines.append(
                f"  - parcel={s['parcel_number']} "
                f"last_observed={s['observed_at'][:10] if s['observed_at'] else 'N/A'}"
            )

    # 7. coverage_delta
    cd = findings["coverage_delta"]
    flagged_sources = [r for r in cd if r["flagged"]]
    lines.append(f"\n[coverage_delta] {len(flagged_sources)} source(s) with >15% drift")
    for r in cd:
        marker = "DRIFT" if r["flagged"] else "ok"
        lines.append(
            f"  - [{marker}] {r['source']}: "
            f"run_active={r['run_active']} db_active={r['db_active']} "
            f"drift={r['drift_pct']}% (run@{r['last_run_at'][:16]})"
        )

    lines.append("\n" + "=" * 50)
    return "\n".join(lines)


def _print_table(findings: dict[str, list[dict[str, Any]]], elapsed: float) -> None:
    checks = [
        ("same_source_dupes", findings["same_source_dupes"]),
        ("past_sale_not_expired", findings["past_sale_not_expired"]),
        ("stale_active_leak", findings["stale_active_leak"]),
        ("address_completeness", findings["address_completeness"]),
        ("probate_validity",
         [x for x in findings["probate_validity"] if x.get("sub_check") == "closed_case_still_active"]),
        ("probate_join_sanity",
         [x for x in findings.get("probate_join_sanity", []) if x.get("type") == "overmatched_case"]),
        ("signal_freshness",
         [{"total": findings["signal_freshness"][0]["total_stale"]}] if findings["signal_freshness"] else []),
        ("coverage_delta", [r for r in findings["coverage_delta"] if r["flagged"]]),
    ]
    print()
    print(f"  {'CHECK':<30} {'COUNT':>7}  RESULT")
    print("  " + "-" * 55)
    for name, items in checks:
        result = "OK" if not items else "FLAGGED"
        print(f"  {name:<30} {len(items):>7}  {result}")
    print("  " + "-" * 55)
    print(f"  Elapsed: {elapsed:.2f}s")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="Tranchi nightly data-quality audit")
    parser.add_argument("--dry-run", action="store_true", help="Print digest; skip Telegram")
    args = parser.parse_args()

    start = time.time()
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        findings: dict[str, list[dict[str, Any]]] = {
            "same_source_dupes":    await check_same_source_dupes(conn),
            "past_sale_not_expired": await check_past_sale_not_expired(conn),
            "stale_active_leak":    await check_stale_active_leak(conn),
            "address_completeness": await check_address_completeness(conn),
            "probate_validity":     await check_probate_validity(conn),
            "probate_join_sanity":  await check_probate_join_sanity(conn),
            "signal_freshness":     await check_signal_freshness(conn),
            "coverage_delta":       await check_coverage_delta(conn),
        }
    finally:
        await conn.close()

    elapsed = time.time() - start
    digest = _format_digest(findings)
    print(digest)
    _print_table(findings, elapsed)

    if args.dry_run:
        logger.info("[DRY RUN] No Telegram sent.")
        return

    # Only alert if something is actually flagged
    closed_probate = [
        x for x in findings["probate_validity"]
        if x.get("sub_check") == "closed_case_still_active"
    ]
    flagged_coverage = [r for r in findings["coverage_delta"] if r["flagged"]]
    overmatched_joins = [
        x for x in findings.get("probate_join_sanity", [])
        if x.get("type") == "overmatched_case"
    ]
    anything_flagged = (
        findings["same_source_dupes"]
        or findings["past_sale_not_expired"]
        or findings["stale_active_leak"]
        or findings["address_completeness"]
        or closed_probate
        or overmatched_joins
        or flagged_coverage
    )
    if anything_flagged:
        _send_telegram(digest)
    else:
        logger.info("All checks clean — no Telegram alert needed.")


if __name__ == "__main__":
    asyncio.run(main())
