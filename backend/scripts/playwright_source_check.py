"""
Tranchi — Live source cross-verify (Track 7).

For each sampled active listing, visit the live source and confirm presence.
The committed-and-cron-able version of "open the source URL and check by hand"
that the /tranchi-verify skill walks Jayden through.

Per-signal logic:
  probate                       — confirm case_status=OPEN and last_seen recent.
                                  (ProWare doesn't deep-link by case_number; the
                                  search-form Playwright path is slow. We rely on
                                  the always-on read API + weekly recheck cron;
                                  this script confirms the stored state is fresh.)
  tax_delinquent_foreclosure    — re-hit DLN REST API, confirm case_number still
                                  appears in the upcoming feed (sale_date >= today).
  mortgage_foreclosure          — same as above, different feed type.
  land_bank_inventory           — re-fetch the Land Bank inventory HTML, parse
                                  table for parcel match.
  ALL (cross-cut)               — re-fetch MyPlace parcel deep-link, confirm
                                  owner_name still matches what's stored.

Output: per-listing PASS / FAIL / ERROR + a top-line summary.

Run:
  python scripts/playwright_source_check.py --sample 10
  python scripts/playwright_source_check.py --signal probate --limit 20
  python scripts/playwright_source_check.py --parcels 203-28-051 --json
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
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
import httpx  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("source_check")

# Reuse known endpoints from the production scrapers (no imports — keep this script standalone)
_DLN_API = "https://www.dln.com/wp-json/dln/v1/data-table"
_DLN_PER_PAGE = 100
_DLN_MAX_PAGES = 70
_LANDBANK_URL = "https://cuyahogalandbank.org/all-available-properties/"
_MYPLACE_BASE = "https://myplace.cuyahogacounty.gov"
_PROBATE_FRESHNESS_MAX_DAYS = 14  # case_status considered "fresh" if seen within this window


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _myplace_url(parcel: str) -> str:
    return f"{_MYPLACE_BASE}/{_b64(parcel)}?city={_b64('99')}&searchBy={_b64('Parcel')}"


# ─────────────────────────────────────────────────────────────────────────────
# DLN verifier — re-hit the REST API and check case_no presence
# ─────────────────────────────────────────────────────────────────────────────

_DLN_CACHE: dict[str, set[str]] = {}  # feed_type -> set of case_no strings


async def _load_dln_feed(client: httpx.AsyncClient, feed_type: str) -> set[str]:
    """Return set of all case_no strings currently in the DLN upcoming feed.
    Cached per process invocation to avoid re-paginating for each listing.
    """
    if feed_type in _DLN_CACHE:
        return _DLN_CACHE[feed_type]
    cases: set[str] = set()
    for page in range(1, _DLN_MAX_PAGES + 1):
        params = {
            "page": page, "per_page": _DLN_PER_PAGE,
            "type": feed_type, "orderby": "case_no",
        }
        try:
            r = await client.get(_DLN_API, params=params, timeout=20)
            if r.status_code != 200:
                break
            data = r.json()
            rows = data.get("data") or []
            if not rows:
                break
            for rec in rows:
                acf = rec.get("acf") or {}
                cno = (acf.get("case_no") or "").strip()
                if cno:
                    cases.add(cno)
            total_pages = int(data.get("total_pages") or 1)
            if page >= total_pages:
                break
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning("DLN fetch failed (page=%d type=%s): %s", page, feed_type, e)
            break
    _DLN_CACHE[feed_type] = cases
    logger.info("DLN %s cache: %d cases", feed_type, len(cases))
    return cases


async def verify_dln(client: httpx.AsyncClient, row: dict) -> dict:
    case_no = (row.get("case_number") or "").strip()
    if not case_no:
        return {"verdict": "FAIL", "evidence": "no case_number stored"}
    feed_type = "delinquent-tax" if row["signal_type"] == "tax_delinquent_foreclosure" else "sheriff-sales"
    cases = await _load_dln_feed(client, feed_type)
    if case_no in cases:
        return {"verdict": "PASS", "evidence": f"case {case_no} in live DLN feed ({feed_type})"}
    return {"verdict": "FAIL", "evidence": f"case {case_no} NOT in live DLN {feed_type} feed (size={len(cases)})"}


# ─────────────────────────────────────────────────────────────────────────────
# Land Bank verifier — fetch inventory page once, check parcel present
# ─────────────────────────────────────────────────────────────────────────────

_LANDBANK_CACHE: set[str] | None = None


async def _load_landbank(client: httpx.AsyncClient) -> set[str]:
    global _LANDBANK_CACHE
    if _LANDBANK_CACHE is not None:
        return _LANDBANK_CACHE
    parcels: set[str] = set()
    try:
        r = await client.get(_LANDBANK_URL, timeout=30)
        if r.status_code == 200:
            for m in re.finditer(r"\b(\d{3}-\d{2}-\d{3})\b", r.text):
                parcels.add(m.group(1))
    except Exception as e:
        logger.warning("Land Bank fetch failed: %s", e)
    _LANDBANK_CACHE = parcels
    logger.info("Land Bank cache: %d parcels", len(parcels))
    return parcels


async def verify_landbank(client: httpx.AsyncClient, row: dict) -> dict:
    parcel = (row.get("source_listing_id") or "").strip()
    parcels = await _load_landbank(client)
    if parcel in parcels:
        return {"verdict": "PASS", "evidence": f"parcel {parcel} in Land Bank inventory"}
    return {"verdict": "FAIL", "evidence": f"parcel {parcel} NOT in Land Bank live inventory ({len(parcels)} listed)"}


# ─────────────────────────────────────────────────────────────────────────────
# Probate verifier — stored case_status freshness (no live ProWare visit)
# ─────────────────────────────────────────────────────────────────────────────

async def verify_probate(row: dict) -> dict:
    """Honest scope note: a live ProWare visit per case is ~3-5s via Playwright form-fill
    (ProWare has no deep-link by case_number — only by internal int id we don't store).
    Instead we trust the always-on read-API gate (case_status NOT IN {closed/disposed/
    terminated/dismissed}) plus the weekly probate_recheck cron, and we confirm the
    stored state is FRESH (last_seen within _PROBATE_FRESHNESS_MAX_DAYS).
    """
    cs = (row.get("case_status") or "").strip()
    last_seen = row.get("last_seen_at")
    if not cs:
        return {"verdict": "FAIL", "evidence": "no case_status stored"}
    if cs != "OPEN":
        return {"verdict": "FAIL", "evidence": f"case_status={cs} (not OPEN)"}
    if last_seen:
        age = (datetime.now(timezone.utc) - last_seen).days
        if age > _PROBATE_FRESHNESS_MAX_DAYS:
            return {"verdict": "FAIL", "evidence": f"case_status=OPEN but stale (last_seen {age}d ago)"}
        return {"verdict": "PASS", "evidence": f"case_status=OPEN, last_seen {age}d ago"}
    return {"verdict": "PASS", "evidence": "case_status=OPEN (no last_seen)"}


# ─────────────────────────────────────────────────────────────────────────────
# MyPlace verifier — fetch parcel deep-link and confirm owner_name appears
# ─────────────────────────────────────────────────────────────────────────────

async def verify_myplace(client: httpx.AsyncClient, row: dict) -> dict:
    parcel = (row.get("source_listing_id") or "").strip()
    expected_owner = (row.get("owner_name") or "").strip()
    if not parcel:
        return {"verdict": "FAIL", "evidence": "no parcel"}
    try:
        r = await client.get(_myplace_url(parcel), timeout=20, follow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return {"verdict": "FAIL", "evidence": f"MyPlace status={r.status_code}"}
        html = r.text
        if expected_owner:
            # Compare the leading token of the owner string (last name) to the page
            first_token = expected_owner.split(",")[0].strip().split(" ")[0].upper()
            if first_token and first_token in html.upper():
                return {"verdict": "PASS", "evidence": f"MyPlace shows owner containing '{first_token}'"}
            return {"verdict": "FAIL", "evidence": f"MyPlace loaded but owner token '{first_token}' not on page"}
        # No expected owner stored — check parcel string itself
        if parcel in html:
            return {"verdict": "PASS", "evidence": f"MyPlace shows parcel {parcel}"}
        return {"verdict": "FAIL", "evidence": "MyPlace loaded but parcel string not visible"}
    except Exception as e:
        return {"verdict": "ERROR", "evidence": f"MyPlace fetch error: {str(e)[:80]}"}


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

async def verify_one(row: dict, client: httpx.AsyncClient) -> dict:
    sig = row["signal_type"]
    parcel = row["source_listing_id"]

    # Source-specific verifier
    if sig == "probate":
        src_result = await verify_probate(row)
    elif sig in ("tax_delinquent_foreclosure", "mortgage_foreclosure"):
        src_result = await verify_dln(client, row)
    elif sig == "land_bank_inventory":
        src_result = await verify_landbank(client, row)
    else:
        src_result = {"verdict": "ERROR", "evidence": f"unknown signal_type={sig}"}

    # Cross-cut MyPlace check (always)
    mp_result = await verify_myplace(client, row)

    # Combined verdict: PASS only if BOTH pass
    combined = "PASS"
    if src_result["verdict"] != "PASS" or mp_result["verdict"] != "PASS":
        if src_result["verdict"] == "ERROR" or mp_result["verdict"] == "ERROR":
            combined = "ERROR"
        else:
            combined = "FAIL"

    return {
        "id": str(row["id"]),
        "signal_type": sig,
        "parcel": parcel,
        "address": f"{row['property_address']}, {row['property_city']}",
        "case_number": row.get("case_number"),
        "source_verdict": src_result["verdict"],
        "source_evidence": src_result["evidence"],
        "myplace_verdict": mp_result["verdict"],
        "myplace_evidence": mp_result["evidence"],
        "combined": combined,
    }


async def _sample_rows(conn: asyncpg.Connection, *, sample: int, signal: str | None,
                       limit: int | None, parcels: list[str] | None) -> list[dict]:
    if parcels:
        rows = await conn.fetch(
            """
            SELECT l.id, l.signal_type, l.source_listing_id, l.property_address, l.property_city,
                   l.case_number, l.case_status, l.last_seen_at, p.owner_name
            FROM tranchi.listings l LEFT JOIN tranchi.parcels p ON p.parcel_number = l.source_listing_id
            WHERE l.source_listing_id = ANY($1) AND l.status='active' AND l.duplicate_of IS NULL
            """,
            parcels,
        )
        return [dict(r) for r in rows]
    where = "l.status='active' AND l.duplicate_of IS NULL"
    params: list = []
    if signal:
        where += " AND l.signal_type = $1"
        params.append(signal)
    n = limit or sample or 10
    rows = await conn.fetch(
        f"""
        SELECT l.id, l.signal_type, l.source_listing_id, l.property_address, l.property_city,
               l.case_number, l.case_status, l.last_seen_at, p.owner_name
        FROM tranchi.listings l LEFT JOIN tranchi.parcels p ON p.parcel_number = l.source_listing_id
        WHERE {where}
        ORDER BY random() LIMIT {int(n)}
        """,
        *params,
    )
    return [dict(r) for r in rows]


async def run(args) -> int:
    url = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(url)
    try:
        rows = await _sample_rows(
            conn, sample=args.sample, signal=args.signal, limit=args.limit,
            parcels=args.parcels,
        )
        if not rows:
            print("No rows selected.")
            return 0

        t0 = time.time()
        async with httpx.AsyncClient() as client:
            # Concurrent (httpx + DB-stored probate are both quick)
            sem = asyncio.Semaphore(args.concurrency)

            async def _bounded(r):
                async with sem:
                    return await verify_one(r, client)
            results = await asyncio.gather(*[_bounded(r) for r in rows])
        elapsed = time.time() - t0

        if args.json:
            print(json.dumps({"elapsed_s": round(elapsed, 1), "results": results}, indent=2, default=str))
            return 0

        # Human-readable
        print(f"\n=== PLAYWRIGHT-CROSS-VERIFY — {len(results)} listings ({elapsed:.1f}s) ===\n")
        counts = {"PASS": 0, "FAIL": 0, "ERROR": 0}
        for i, r in enumerate(results, 1):
            counts[r["combined"]] = counts.get(r["combined"], 0) + 1
            print(f"[{i:>2}] {r['combined']:<5} {r['signal_type']:<26} {r['address']} ({r['parcel']})")
            print(f"      source:  [{r['source_verdict']}] {r['source_evidence']}")
            print(f"      MyPlace: [{r['myplace_verdict']}] {r['myplace_evidence']}")
        print("\n" + "=" * 70)
        print(f"  PASS={counts['PASS']}  FAIL={counts['FAIL']}  ERROR={counts['ERROR']}  (of {len(results)})")
        if counts["FAIL"]:
            print("\n  Failing listing IDs (for spot-check / dispute):")
            for r in results:
                if r["combined"] == "FAIL":
                    print(f"    {r['id']} -- {r['address']} ({r['parcel']}) — {r['source_evidence']}")
        print("=" * 70 + "\n")
        return 0
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Tranchi live-source cross-verify")
    ap.add_argument("--sample", type=int, default=10)
    ap.add_argument("--signal", type=str, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--parcels", nargs="*", default=None)
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
