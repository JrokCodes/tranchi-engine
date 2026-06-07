"""Surface pre-distress SIGNAL parcels as filterable LEAD listings.

INVARIANT — these are distress_stage='distress_signal' rows. They are LEADS derived from
a signal (tax_delinquent lawsuit list, eviction), NOT buy-now deals. They exist so users
can filter the "Pre-Distress" view; the default feed (distress_stage='buy_now') is
unchanged. A lead is materialized into tranchi.listings (not a view) so it inherits the
full validity stack: cross-source dedup, the no-transfer off-market guard, HOT stacking,
read gates, and the daily verify sample.

INVARIANT — this pass OWNS the lifecycle of its lead rows. Their source_site is
deliberately absent from staleness.SOURCE_STALENESS (the generic _mark_stale guard only
touches FULL_RESCAN scraper sources that have a scrape_run; leads have neither). A lead is
retired here (status='not_listed') the moment its backing signal goes stale (parcel
redeemed / no longer on the delinquent or eviction list), its type is disabled by Marc, or
a real buy-now listing appears on the same parcel.

INVARIANT — config is data-driven from tranchi.distress_lead_types. enabled=false is Marc's
per-type kill switch: the next run retires that whole type. signal_source scopes each type
to ONE market (e.g. shelby_county_trustee) so Cuyahoga's like-named signals never leak in.

Run standalone:  python -m app.scrapers.surface_distress [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_env = _backend / ".env"
if _env.exists():
    from dotenv import load_dotenv
    load_dotenv(_env)

import asyncpg  # noqa: E402

from app.market_config import MARKETS  # noqa: E402

logger = logging.getLogger("surface_distress")

# A signal is "live" while its scraper has touched it within this window (scrapers run
# every 3h; a generous buffer avoids retiring leads on a single missed cycle).
_FRESH = "now() - interval '4 days'"

# Off-market guard: a parcel that SOLD within this window is no longer a distressed-owner
# lead (the durable, scriptable half of "off-market" — the MLS-listed half is the human
# /verify-market sample). Depends on last_sale_date enrichment (enrich_sales --all-spine);
# NULL last_sale_date is kept (unknown) and self-activates as coverage grows.
_SOLD_GUARD = "(p.last_sale_date IS NULL OR p.last_sale_date < (now() - interval '12 months')::date)"


def _lead_fragments(market: str, signal_type: str) -> dict | None:
    """Per-(market, signal_type) SQL fragments for building a lead from its signals.

    Driven by market_config `distress_lead_county` + `distress_lead_rules`. `address_source`:
    'spine' (Shelby — parcels.situs_address; payload location is street-only) vs 'payload'
    (Cuyahoga — the signal payload carries the full "STREET, CITY, ST, ZIP" situs; the parcel
    spine is too thin to rely on). `gate_sql` is the per-market defensible-slice filter (real
    distress only); None => surface every fresh signal (Shelby's current behavior). Returns
    None when the market/type isn't configured (caller skips, never crashes the whole pass).
    """
    cfg = MARKETS.get(market)
    if not cfg:
        return None
    rules = cfg.get("distress_lead_rules") or {}
    rule = rules.get(signal_type)
    if rule is None:
        return None
    state = cfg["state"]
    county = cfg["distress_lead_county"]
    src = rule["address_source"]
    owner_key = rule.get("owner_key")
    if src == "spine":
        addr = "COALESCE(NULLIF(p.situs_address,''), s.payload->>'property_location')"
        city = "initcap(substring(p.situs_address from ',\\s*([A-Za-z .]+?),\\s*" + state + "'))"
        zip_ = "substring(p.situs_address from '(\\d{5})(?:-\\d{4})?\\s*$')"
        addr_notnull = addr + " IS NOT NULL"
        owner = "COALESCE(p.owner_name, s.payload->>'" + owner_key + "')" if owner_key else "p.owner_name"
    elif src == "payload":
        akey = rule["address_key"]
        col = "s.payload->>'" + akey + "'"
        addr = col
        city = "initcap(substring(" + col + " from ',\\s*([A-Za-z .]+?),\\s*" + state + "'))"
        zip_ = "substring(" + col + " from '(\\d{5})(?:-\\d{4})?\\s*$')"
        addr_notnull = "NULLIF(" + col + ",'') IS NOT NULL"
        owner = "s.payload->>'" + owner_key + "'" if owner_key else "NULL"
    else:
        raise ValueError(f"unknown address_source {src!r} for {market}/{signal_type}")
    return {
        "addr": addr, "city": city, "zip": zip_, "owner": owner,
        "addr_notnull": addr_notnull, "state": state, "county": county,
        "market": market, "gate": rule.get("gate_sql") or "TRUE",
    }


async def _enabled_and_disabled(conn: asyncpg.Connection):
    rows = await conn.fetch(
        "SELECT signal_type, enabled, signal_source, source_site, label, market "
        "FROM tranchi.distress_lead_types ORDER BY market, signal_type"
    )
    return [r for r in rows if r["enabled"]], [r for r in rows if not r["enabled"]]


async def surface_distress_leads(pool: asyncpg.Pool, *, dry_run: bool = False) -> dict:
    """Materialize/refresh/retire pre-distress lead listings. Returns a per-type stat dict."""
    stats: dict[str, dict] = {}
    async with pool.acquire() as conn:
        try:
            enabled, disabled = await _enabled_and_disabled(conn)
        except asyncpg.UndefinedTableError:
            logger.warning("distress_lead_types missing — migration 012 not applied; skipping")
            return stats

        # Disabled types: retire every active lead row (Marc flipped the switch off).
        for r in disabled:
            ss = r["source_site"]
            n = await conn.fetchval(
                "SELECT count(*) FROM tranchi.listings WHERE source_site=$1 AND status='active'",
                ss,
            )
            if not dry_run:
                if n:
                    await conn.execute(
                        "UPDATE tranchi.listings SET status='not_listed' "
                        "WHERE source_site=$1 AND status='active'",
                        ss,
                    )
                await _record_run(conn, ss, found=0, active=0, new_today=0)
            stats[ss] = {"enabled": False, "retired_disabled": int(n or 0)}
            if n:
                logger.info("[%s] disabled — retired %d active leads%s", ss, n,
                            " [DRY]" if dry_run else "")

        # Enabled types: retire stale, insert new, refresh survivors. Each row is keyed to
        # ONE market (distress_lead_types.market); the SQL fragments (address source + the
        # defensible-slice gate) come from that market's config. Shelby's fragments reproduce
        # its prior behavior exactly (spine address, gate=TRUE, sold-guard a no-op while its
        # last_sale_date is NULL); Cuyahoga uses payload address + severity/freshness gates.
        for r in enabled:
            st, src, ss = r["signal_type"], r["signal_source"], r["source_site"]
            mkt = r["market"]
            f = _lead_fragments(mkt, st)
            if f is None:
                logger.warning("[%s] no distress_lead_rules for market=%s type=%s — skipping",
                               ss, mkt, st)
                continue
            gate = f["gate"]

            # 1) Retire leads whose backing signal went stale OR no longer passes the gate
            #    (e.g. a code violation closed, a balance dropped below threshold), or whose
            #    parcel now has a real buy-now listing (the deal supersedes the lead).
            retire_sql = f"""
                UPDATE tranchi.listings l SET status='not_listed'
                WHERE l.source_site=$1 AND l.status='active'
                  AND (
                    NOT EXISTS (SELECT 1 FROM tranchi.signals s
                       WHERE s.parcel_number=l.source_listing_id
                         AND s.source=$2 AND s.signal_type=$3
                         AND s.last_seen_at >= {_FRESH}
                         AND ({gate}))
                    OR EXISTS (SELECT 1 FROM tranchi.listings bl
                       WHERE bl.source_listing_id=l.source_listing_id
                         AND bl.status='active' AND bl.distress_stage='buy_now')
                  )
            """
            # 2) Insert leads for fresh, gate-passing, off-market signal parcels not already
            #    surfaced and with no active buy-now listing. One row per parcel; needs a
            #    usable address. p (LEFT JOIN) supplies the sold-guard's last_sale_date.
            insert_sql = f"""
                INSERT INTO tranchi.listings
                    (source_site, signal_type, distress_stage, status,
                     property_address, property_city, property_zip, property_state, property_county,
                     source_listing_id, trustee_name, market, first_seen_at, last_seen_at)
                SELECT DISTINCT ON (s.parcel_number)
                    $1, $3, 'distress_signal', 'active',
                    {f['addr']},
                    {f['city']},
                    {f['zip']},
                    '{f['state']}', '{f['county']}',
                    s.parcel_number,
                    {f['owner']},
                    '{f['market']}',
                    now(), now()
                FROM tranchi.signals s
                LEFT JOIN tranchi.parcels p
                  ON p.parcel_number = s.parcel_number AND p.market = s.market
                WHERE s.source=$2 AND s.signal_type=$3
                  AND s.last_seen_at >= {_FRESH}
                  AND {f['addr_notnull']}
                  AND ({gate})
                  AND {_SOLD_GUARD}
                  AND NOT EXISTS (SELECT 1 FROM tranchi.listings bl
                        WHERE bl.source_listing_id=s.parcel_number
                          AND bl.status='active' AND bl.distress_stage='buy_now')
                  AND NOT EXISTS (SELECT 1 FROM tranchi.listings el
                        WHERE el.source_site=$1 AND el.source_listing_id=s.parcel_number)
                ORDER BY s.parcel_number, s.observed_at DESC
            """
            refresh_sql = f"""
                UPDATE tranchi.listings l SET last_seen_at=now()
                WHERE l.source_site=$1 AND l.status='active'
                  AND EXISTS (SELECT 1 FROM tranchi.signals s
                       WHERE s.parcel_number=l.source_listing_id
                         AND s.source=$2 AND s.signal_type=$3
                         AND s.last_seen_at >= {_FRESH}
                         AND ({gate}))
            """

            if dry_run:
                would_insert = await conn.fetchval(f"""
                    SELECT count(*) FROM (
                        SELECT DISTINCT s.parcel_number
                        FROM tranchi.signals s
                        LEFT JOIN tranchi.parcels p
                          ON p.parcel_number=s.parcel_number AND p.market = s.market
                        WHERE s.source=$2 AND s.signal_type=$3
                          AND s.last_seen_at >= {_FRESH}
                          AND {f['addr_notnull']}
                          AND ({gate})
                          AND {_SOLD_GUARD}
                          AND NOT EXISTS (SELECT 1 FROM tranchi.listings bl
                                WHERE bl.source_listing_id=s.parcel_number
                                  AND bl.status='active' AND bl.distress_stage='buy_now')
                          AND NOT EXISTS (SELECT 1 FROM tranchi.listings el
                                WHERE el.source_site=$1 AND el.source_listing_id=s.parcel_number)
                    ) z
                """, ss, src, st)
                existing = await conn.fetchval(
                    "SELECT count(*) FROM tranchi.listings WHERE source_site=$1 AND status='active'", ss)
                stats[ss] = {"enabled": True, "market": mkt, "would_insert": int(would_insert or 0),
                             "existing_active": int(existing or 0)}
                logger.info("[%s/%s] DRY: would insert %s new leads (existing active=%s)",
                            mkt, ss, would_insert, existing)
                continue

            retired = _affected(await conn.execute(retire_sql, ss, src, st))
            inserted = _affected(await conn.execute(insert_sql, ss, src, st))
            await conn.execute(refresh_sql, ss, src, st)
            active = int(await conn.fetchval(
                "SELECT count(*) FROM tranchi.listings WHERE source_site=$1 AND status='active'", ss) or 0)
            # found = live UPSTREAM fresh-signal count (not =active) so audit_scrapers can detect
            # a signal-feed collapse: if the tax_delinquent/eviction scraper dies, this drops while
            # active lingers, surfacing the outage that active-only counts would hide.
            fresh_signals = int(await conn.fetchval(
                f"SELECT count(DISTINCT parcel_number) FROM tranchi.signals "
                f"WHERE source=$1 AND signal_type=$2 AND last_seen_at >= {_FRESH}", src, st) or 0)
            await _record_run(conn, ss, found=fresh_signals, active=active, new_today=inserted)
            stats[ss] = {"enabled": True, "market": mkt, "inserted": inserted, "retired": retired,
                         "active_total": active}
            logger.info("[%s/%s] inserted %d, retired %d, active now %d", mkt, ss, inserted, retired, active)

    return stats


async def _record_run(conn: asyncpg.Connection, source_site: str, *,
                       found: int, active: int, new_today: int) -> None:
    """Write a completed scrape_runs row so the lead source is first-class on the Sources
    page (counts + 'online' freshness) and populates the Pre-Distress source dropdown."""
    await conn.execute(
        """
        INSERT INTO tranchi.scrape_runs
            (source_site, started_at, completed_at, status, found, passed, active, new_today)
        VALUES ($1, now(), now(), 'success', $2, $2, $3, $4)
        """,
        source_site, found, active, new_today,
    )


def _affected(tag: str | None) -> int:
    try:
        return int((tag or "0").split()[-1])
    except (ValueError, IndexError):
        return 0


async def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        from app.config import settings
        dsn = settings.DATABASE_URL
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        stats = await surface_distress_leads(pool, dry_run=args.dry_run)
        logger.info("surface_distress done%s: %s", " [DRY RUN]" if args.dry_run else "", stats)
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
