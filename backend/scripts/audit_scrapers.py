"""
Tranchi Engine — Live-vs-DB Scraper Coverage Audit

For each FULL_RESCAN listing source, compares what the source currently
exposes (live count) against the latest scrape_runs snapshot and the current
DB active count. Alerts if drift exceeds _THRESHOLD.

For CURSOR/ARCHIVE sources (Probate, Sheriff Sales history), live fetches are
not meaningful (cursor walk never re-visits old records; archive is historical).
Those sources are coverage-checked via scrape_run run-to-run collapse detection
instead (a sudden drop in `found` signals the source went down or changed).

Usage:
  python scripts/audit_scrapers.py            # table + Telegram on drift
  python scripts/audit_scrapers.py --json     # JSON output for monitoring
  python scripts/audit_scrapers.py --no-alert # skip Telegram

Cron (proposed):
  0 8 * * 1-5   ... audit_scrapers.py   (daily 8:00 AM UTC, Mon-Fri)

INVARIANTS:
- This script is READ-ONLY against tranchi.* — no INSERT/UPDATE/DELETE.
- Live probes for FULL_RESCAN sources call lightweight count-only endpoints
  (DLN: paged REST API; Land Bank: single HTML GET).
- CURSOR/ARCHIVE sources are audited via run-to-run collapse only (no live
  fetch) — flagging collapse ≥50% in `found` across the last 3 completed runs.
- Telegram is optional/non-fatal. Missing secret file → log-only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap: allow `python scripts/audit_scrapers.py` from backend/
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
import httpx    # noqa: E402

from app.scrapers.staleness import StalenessPolicy, SOURCE_STALENESS, policy_for  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] audit_scrapers: %(message)s",
)
logger = logging.getLogger("audit_scrapers")

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

import os
DATABASE_URL: str = os.environ["DATABASE_URL"]

# Telegram — same TODO as quality_audit.py: wire once the secret file exists.
# TODO: create /home/ubuntu/.secrets/tranchi/telegram-bot-token to enable alerts.
_TELEGRAM_TOKEN_PATH = Path("/home/ubuntu/.secrets/tranchi/telegram-bot-token")
_TELEGRAM_CHAT_ID = "8360510944"  # @intelleq_monitor_bot
_COOLDOWN_FILE = Path("/tmp/tranchi-audit-scrapers-last-alert")
_COOLDOWN_SECONDS = 1800  # 30-minute cooldown between repeated alerts

_THRESHOLD = 0.10          # 10% live-vs-DB drift threshold for FULL_RESCAN sources (default)
_COLLAPSE_THRESHOLD = 0.50 # 50% drop in scrape_runs.found → collapse flag for CURSOR/ARCHIVE

# Per-source thresholds for sites with high intra-day churn (e.g., a feed gains/loses
# listings faster than the global gate tolerates). Default is empty — Tranchi's gov't
# sources have been stable, but the mechanism is here so we can tighten/loosen per
# source without code churn. Mirror of Gotham's audit_scrapers.py pattern.
# Verified via Playwright when populating: load the live page, confirm count matches
# httpx, then decide if observed drift is real source churn vs a parser bug.
_PER_SOURCE_THRESHOLD: dict[str, float] = {
    # "Cuyahoga Sheriff Sale (DLN)": 0.15,  # example: if DLN churns intra-day
    # "Cuyahoga Land Bank":          0.05,  # example: tighter when source is stable
}

# Sources where DB count is expected to be <= live for known reasons. Anything in
# this set is gated OK regardless of drift (Gotham parallels Tidewater's "weeks 2-5
# upstream issue" carve-out). Empty for Tranchi today.
_KNOWN_UNDERCOUNT: set[str] = set()

# DLN API — same endpoint the dln.py scraper uses; no auth required
_DLN_API_URL = "https://www.dln.com/wp-json/dln/v1/data-table"
_DLN_PER_PAGE = 100
_DLN_MAX_PAGES = 70
_DLN_TIMEOUT = 30.0
_DLN_TYPES = ["delinquent-tax", "sheriff-sales"]

# Land Bank — single HTML page, all rows preloaded (no JS rendering needed)
_LANDBANK_LIST_URL = "https://cuyahogalandbank.org/all-available-properties/"
_LANDBANK_TIMEOUT = 30.0

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ─────────────────────────────────────────────────────────────────────────────
# Live count probes — FULL_RESCAN sources only
# ─────────────────────────────────────────────────────────────────────────────

async def _probe_dln(client: httpx.AsyncClient) -> tuple[int | None, str]:
    """
    Count all listings across both DLN feed types (delinquent-tax +
    sheriff-sales) using the API's total_posts field.

    The DLN REST API response envelope uses "data" (not "posts") for the row
    array, and "total_posts" for the full count across all pages. We read
    total_posts from page 1 of each feed type to avoid paginating 39 pages.

    Returns (total_count, note). Returns (None, error) on failure.
    """
    total = 0
    try:
        for feed_type in _DLN_TYPES:
            params = {
                "type": feed_type,
                "per_page": str(_DLN_PER_PAGE),
                "page": "1",
                "orderby": "meta_value",
                "meta_key": "sale_date",
                "order": "ASC",
            }
            resp = await client.get(_DLN_API_URL, params=params, timeout=_DLN_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            # API envelope: {"total_pages": N, "total_posts": M, "data": [...]}
            feed_total = data.get("total_posts", 0) or 0
            total += feed_total
            await asyncio.sleep(0.6)
        return total, "delinquent-tax + sheriff-sales via total_posts"
    except Exception as exc:
        logger.error("DLN probe failed: %s", exc)
        return None, f"probe error: {exc}"


async def _probe_landbank(client: httpx.AsyncClient) -> tuple[int | None, str]:
    """
    Count available Land Bank properties from the single preloaded HTML table.
    Mirrors landbank.py's single-GET approach (no JS, no pagination).

    Returns (row_count, note). Returns (None, error) on failure.
    """
    try:
        from bs4 import BeautifulSoup

        resp = await client.get(_LANDBANK_LIST_URL, timeout=_LANDBANK_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        # The Land Bank table DataTables preloads all rows in <tbody>
        table = soup.find("table", {"id": "property-table"}) or soup.find("table")
        if table is None:
            return None, "table not found in page"
        rows = table.find("tbody").find_all("tr") if table.find("tbody") else []
        # Filter out empty/header rows
        data_rows = [r for r in rows if r.find("td")]
        return len(data_rows), "single HTML GET"
    except Exception as exc:
        logger.error("Land Bank probe failed: %s", exc)
        return None, f"probe error: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# Run-to-run collapse detection — CURSOR / ARCHIVE sources
# ─────────────────────────────────────────────────────────────────────────────

async def _check_run_collapse(
    conn: asyncpg.Connection,
    source_site: str,
) -> dict[str, Any]:
    """
    For sources where live fetching isn't meaningful (CURSOR, ARCHIVE),
    compare completed scrape_runs.found values to detect a sudden collapse.

    CURSOR sources (Probate) are a walk-forward cursor — they emit found=0
    when the cursor catches up and there are no new IDs this cycle. found=0
    is a normal heartbeat, NOT a failure. To avoid false positives we:
      - For CURSOR: fetch the last 5 runs, exclude found=0 runs from the
        baseline, and only flag collapse if the latest NON-ZERO run's found
        is far below the median of prior non-zero runs AND there have been
        ≥3 consecutive zero-found runs (the cursor is truly stuck).
      - For ARCHIVE: collapse means the constant found count suddenly drops
        (archives don't grow; a drop = the source truncated or went down).
    """
    policy = policy_for(source_site)

    if policy == StalenessPolicy.CURSOR:
        # Pull more runs to find non-zero baselines
        rows = await conn.fetch("""
            SELECT found, started_at
            FROM tranchi.scrape_runs
            WHERE source_site = $1
              AND status = 'success'
            ORDER BY started_at DESC
            LIMIT 10
        """, source_site)

        if not rows:
            return {
                "source": source_site,
                "policy": policy.value,
                "method": "run_collapse",
                "runs_checked": 0,
                "collapse_detected": False,
                "note": "no run history",
            }

        all_found = [r["found"] or 0 for r in rows]
        # Count consecutive zero-found runs from the most recent
        consecutive_zeros = 0
        for f in all_found:
            if f == 0:
                consecutive_zeros += 1
            else:
                break

        non_zero = [f for f in all_found if f > 0]
        if len(non_zero) < 2:
            return {
                "source": source_site,
                "policy": policy.value,
                "method": "run_collapse",
                "found_history": all_found[:5],
                "consecutive_zero_runs": consecutive_zeros,
                "collapse_detected": False,
                "note": "insufficient non-zero run history — cursor may be catching up",
            }

        latest_nonzero = non_zero[0]
        prior_median = sorted(non_zero[1:])[len(non_zero[1:]) // 2]
        drop = (prior_median - latest_nonzero) / prior_median if prior_median > 0 else 0.0
        # Collapse = non-zero runs dropped AND stuck on zero for many cycles
        collapse = drop >= _COLLAPSE_THRESHOLD and consecutive_zeros >= 3
        note = (
            f"latest_nonzero={latest_nonzero} prior_median={prior_median} "
            f"drop={drop*100:.1f}% consecutive_zeros={consecutive_zeros}"
        )
    else:
        # ARCHIVE: constant found count; any large drop is a real signal
        rows = await conn.fetch("""
            SELECT found, started_at
            FROM tranchi.scrape_runs
            WHERE source_site = $1
              AND status = 'success'
            ORDER BY started_at DESC
            LIMIT 3
        """, source_site)

        if len(rows) < 2:
            return {
                "source": source_site,
                "policy": policy.value,
                "method": "run_collapse",
                "runs_checked": len(rows),
                "collapse_detected": False,
                "note": "insufficient run history (<2 runs)",
            }

        all_found = [r["found"] or 0 for r in rows]
        latest = all_found[0]
        prior_median = sorted(all_found[1:])[len(all_found[1:]) // 2]
        if prior_median == 0:
            collapse = False
            note = "prior median=0 — archive always zero-found"
        else:
            drop = (prior_median - latest) / prior_median
            collapse = drop >= _COLLAPSE_THRESHOLD
            note = f"latest={latest} prior_median={prior_median} drop={drop*100:.1f}%"
        consecutive_zeros = 0

    return {
        "source": source_site,
        "policy": policy.value,
        "method": "run_collapse",
        "found_history": [r["found"] or 0 for r in rows[:5]],
        "collapse_detected": collapse,
        "note": note,
        "last_run_at": rows[0]["started_at"].isoformat() if rows[0]["started_at"] else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# DB snapshot helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _get_db_state(conn: asyncpg.Connection) -> dict[str, dict[str, Any]]:
    """Return per-source DB active count + latest scrape_run snapshot."""
    run_rows = await conn.fetch("""
        SELECT DISTINCT ON (source_site)
            source_site,
            found        AS run_found,
            active       AS run_active,
            started_at
        FROM tranchi.scrape_runs
        ORDER BY source_site, started_at DESC
    """)
    db_rows = await conn.fetch("""
        SELECT source_site, COUNT(*) AS db_active
        FROM tranchi.listings
        WHERE status = 'active' AND duplicate_of IS NULL
        GROUP BY source_site
    """)
    db_active_map = {r["source_site"]: r["db_active"] for r in db_rows}

    state: dict[str, dict[str, Any]] = {}
    for r in run_rows:
        state[r["source_site"]] = {
            "run_found": r["run_found"] or 0,
            "run_active": r["run_active"] or 0,
            "db_active": db_active_map.get(r["source_site"], 0),
            "last_run_at": r["started_at"].isoformat() if r["started_at"] else None,
        }
    return state


# ─────────────────────────────────────────────────────────────────────────────
# Main audit
# ─────────────────────────────────────────────────────────────────────────────

async def audit() -> dict[str, Any]:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        db_state = await _get_db_state(conn)

        # Collapse checks for CURSOR / ARCHIVE sources
        collapse_results: list[dict[str, Any]] = []
        for site, policy in SOURCE_STALENESS.items():
            if policy in (StalenessPolicy.CURSOR, StalenessPolicy.ARCHIVE):
                result = await _check_run_collapse(conn, site)
                collapse_results.append(result)
    finally:
        await conn.close()

    # Live probes for FULL_RESCAN sources
    live_results: list[dict[str, Any]] = []
    headers = {"User-Agent": _USER_AGENT}

    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        # DLN
        # The DLN API total_posts includes ALL historical records (both feed types, all
        # dates), while scrape_runs.found is filtered to upcoming sale dates only — the
        # two numbers are not directly comparable for drift. Instead:
        #   - Use live total_posts as an API availability check: if 0 → source is down.
        #   - Use run_found vs db_active as the drift metric (both are filtered the same way).
        dln_live, dln_note = await _probe_dln(client)
        dln_state = db_state.get("Cuyahoga Sheriff Sale (DLN)", {})
        dln_db = dln_state.get("db_active", 0)
        dln_run = dln_state.get("run_found", 0)

        # API down = live is None or 0; drift = run_found vs db_active
        dln_api_ok = dln_live is not None and dln_live > 0
        if dln_run == 0 and dln_db == 0:
            dln_drift = 0.0
        elif dln_run == 0:
            dln_drift = float("inf")
        else:
            dln_drift = abs(dln_run - dln_db) / dln_run
        dln_threshold = _PER_SOURCE_THRESHOLD.get("Cuyahoga Sheriff Sale (DLN)", _THRESHOLD)
        dln_ok = dln_api_ok and (
            "Cuyahoga Sheriff Sale (DLN)" in _KNOWN_UNDERCOUNT or dln_drift <= dln_threshold
        )
        dln_drift_display = round(dln_drift * 100, 1) if dln_drift != float("inf") else None

        live_results.append({
            "source": "Cuyahoga Sheriff Sale (DLN)",
            "policy": StalenessPolicy.FULL_RESCAN.value,
            "method": "live_api",
            "live_total_posts": dln_live,  # raw API universe (all dates, both feeds)
            "run_found": dln_run,           # scraper's filtered upcoming count
            "db_active": dln_db,            # current DB active count
            "drift_pct": dln_drift_display, # run_found vs db_active
            "threshold_pct": round(dln_threshold * 100, 1),
            "api_reachable": dln_api_ok,
            "ok": dln_ok,
            "note": dln_note,
            "last_run_at": dln_state.get("last_run_at"),
        })

        # Land Bank
        lb_live, lb_note = await _probe_landbank(client)
        lb_state = db_state.get("Cuyahoga Land Bank", {})
        lb_db = lb_state.get("db_active", 0)
        lb_run = lb_state.get("run_found", 0)

        if lb_live is None:
            lb_drift = None
            lb_ok = False
        elif lb_live == 0 and lb_db == 0:
            lb_drift = 0.0
            lb_ok = True
        elif lb_live == 0:
            lb_drift = float("inf")
            lb_ok = False
        else:
            lb_drift = abs(lb_live - lb_db) / lb_live
            lb_threshold = _PER_SOURCE_THRESHOLD.get("Cuyahoga Land Bank", _THRESHOLD)
            lb_ok = "Cuyahoga Land Bank" in _KNOWN_UNDERCOUNT or lb_drift <= lb_threshold

        live_results.append({
            "source": "Cuyahoga Land Bank",
            "policy": StalenessPolicy.FULL_RESCAN.value,
            "method": "live_html",
            "live": lb_live,
            "db_active": lb_db,
            "run_found": lb_run,
            "drift_pct": round(lb_drift * 100, 1) if lb_drift is not None and lb_drift != float("inf") else None,
            "threshold_pct": round(_PER_SOURCE_THRESHOLD.get("Cuyahoga Land Bank", _THRESHOLD) * 100, 1),
            "ok": lb_ok,
            "note": lb_note,
            "last_run_at": lb_state.get("last_run_at"),
        })

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "threshold_pct": _THRESHOLD * 100,
        "collapse_threshold_pct": _COLLAPSE_THRESHOLD * 100,
        "live_results": live_results,
        "collapse_results": collapse_results,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Output formatting
# ─────────────────────────────────────────────────────────────────────────────

def _print_table(result: dict[str, Any]) -> None:
    print()
    print(f"Tranchi Scraper Coverage Audit — {result['checked_at']}")
    print(f"Threshold: {result['threshold_pct']:.0f}% (live probes) | "
          f"{result['collapse_threshold_pct']:.0f}% (collapse detection)")
    print()

    # Live-probe table (FULL_RESCAN)
    print(f"  {'SOURCE':<32} {'API_LIVE':>8} {'RUN_FND':>7} {'DB_ACT':>6} {'DRIFT':>7}  {'STATUS':<8} NOTE")
    print("  " + "-" * 90)
    for r in result["live_results"]:
        # DLN has live_total_posts (raw API); Land Bank has live (filtered)
        api_live_s = str(r.get("live_total_posts", r.get("live", "—")) or "—")
        run_fnd_s = str(r.get("run_found", "—"))
        drift_s = (
            "—" if r["drift_pct"] is None
            else f"{r['drift_pct']:.1f}%"
        )
        status = "OK" if r["ok"] else "DRIFT"
        reachable = "" if r.get("api_reachable", True) else " [API DOWN]"
        print(
            f"  {r['source']:<32} {api_live_s:>8} {run_fnd_s:>7} {r['db_active']:>6} "
            f"{drift_s:>7}  {status:<8} {r['note']}{reachable}"
        )

    print()
    # Collapse-detection table (CURSOR / ARCHIVE)
    print(f"  {'SOURCE':<32} {'POLICY':<10} {'RUNS':>5}  {'STATUS':<10} NOTE")
    print("  " + "-" * 78)
    for r in result["collapse_results"]:
        status = "COLLAPSE" if r["collapse_detected"] else "OK"
        runs_s = str(len(r.get("found_history", [])))
        print(
            f"  {r['source']:<32} {r['policy']:<10} {runs_s:>5}  "
            f"{status:<10} {r['note']}"
        )
    print()

    all_ok = (
        all(r["ok"] for r in result["live_results"])
        and not any(r["collapse_detected"] for r in result["collapse_results"])
    )
    print(f"  Overall: {'ALL OK' if all_ok else 'ISSUES DETECTED'}")
    print()


def _format_alert(result: dict[str, Any]) -> str:
    drift_sources = [r for r in result["live_results"] if not r["ok"]]
    collapse_sources = [r for r in result["collapse_results"] if r["collapse_detected"]]

    lines = ["Tranchi scraper audit — issues detected", ""]
    if drift_sources:
        lines.append("Live-vs-DB drift (FULL_RESCAN):")
        for r in drift_sources:
            drift_s = f"{r['drift_pct']:.1f}%" if r["drift_pct"] is not None else "err"
            api_reachable = r.get("api_reachable", True)
            live_display = r.get("live_total_posts", r.get("live", "—"))
            run_found = r.get("run_found", r.get("db_active", "—"))
            if not api_reachable:
                lines.append(f"  - {r['source']}: API UNREACHABLE (live={live_display})")
            else:
                lines.append(
                    f"  - {r['source']}: run_found={run_found} db={r['db_active']} ({drift_s} off)"
                )
    if collapse_sources:
        lines.append("Run collapse detected (CURSOR/ARCHIVE):")
        for r in collapse_sources:
            lines.append(f"  - {r['source']}: {r['note']}")

    lines.append("")
    lines.append(
        f"threshold={result['threshold_pct']:.0f}% | "
        f"checked={result['checked_at']}"
    )
    return "\n".join(lines)


def _send_telegram(message: str) -> None:
    if not _TELEGRAM_TOKEN_PATH.exists():
        # TODO: provision /home/ubuntu/.secrets/tranchi/telegram-bot-token to enable alerts.
        logger.warning(
            "Telegram token not found at %s — alert logged only. "
            "Create the file with the bot token to enable Telegram alerts.",
            _TELEGRAM_TOKEN_PATH,
        )
        return
    try:
        if _COOLDOWN_FILE.exists():
            last = float(_COOLDOWN_FILE.read_text().strip())
            if time.time() - last < _COOLDOWN_SECONDS:
                logger.info("Telegram cooldown active — skipping alert.")
                return
    except (ValueError, OSError):
        pass
    try:
        token = _TELEGRAM_TOKEN_PATH.read_text().strip()
        with httpx.Client(timeout=10.0) as c:
            r = c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": _TELEGRAM_CHAT_ID, "text": message},
            )
            r.raise_for_status()
        _COOLDOWN_FILE.write_text(str(time.time()))
        logger.info("Telegram alert sent.")
    except Exception as exc:
        logger.error("Telegram send failed (non-fatal): %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> int:
    parser = argparse.ArgumentParser(description="Tranchi scraper coverage audit")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument("--no-alert", action="store_true", help="Skip Telegram alert")
    args = parser.parse_args()

    result = await audit()

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_table(result)

    drift_sources = [r for r in result["live_results"] if not r["ok"]]
    collapse_sources = [r for r in result["collapse_results"] if r["collapse_detected"]]
    issues = drift_sources or collapse_sources

    if issues and not args.no_alert:
        _send_telegram(_format_alert(result))

    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
