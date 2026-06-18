"""Stamp CONVICTION TIERS + raw drivers on Detroit blight pre-distress LEADS.

Runs as a post-pass right AFTER surface_distress_leads (which materializes one lead row
per floor-passing parcel into tranchi.listings with source_site='Wayne Blight (Lead)').
surface_distress applies the strict VALIDITY FLOOR (the gate_sql in market_config) but is
generic + per-signal — it cannot do the per-PARCEL aggregation the tier needs. This pass
fills that gap: for each active blight lead it aggregates the parcel's floor-passing
tickets and writes:

  blight_ticket_count  count of floor-passing tickets on the parcel
  blight_total_balance sum of amt_balance_due across those tickets
  absentee_owner       owner-mailing zip5 != situs zip5 (motivated-seller signal)
  conviction_tier      A (2+ tickets AND >=$2k AND absentee)
                       B (2+ tickets AND >=$500)
                       C (otherwise — single floor-passing ticket / low balance)

Tiers are Marc's filter in the dashboard, NOT a kill — every C lead still cleared the
validity floor and is a real, verifiable distress lead. Re-runs every cycle so a parcel's
tier tracks new tickets / balance changes.

INVARIANT — _FLOOR_PER_TICKET below MUST stay in sync with the per-ticket predicates of
market_config _make_wayne_market().distress_lead_rules['blight_violation'].gate_sql. The
gate decides which parcels get a lead row; this pass must aggregate the SAME tickets or
the count/balance/tier would disagree with what was surfaced. (The gate's residential
correlated subquery is omitted here — a lead row only exists for a residential parcel, so
joining to the existing lead already implies it.)

Run standalone:  python -m app.scrapers.wayne_blight_tiering [--dry-run]
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

logger = logging.getLogger("wayne_blight_tiering")

# Lead identity (must match market_config source_sites + the blight signal feed).
_SOURCE_SITE = "Wayne Blight (Lead)"
_SIGNAL_SOURCE = "detroit_blight_tickets"
_SIGNAL_TYPE = "blight_violation"

# A signal is "live" within this window — same buffer surface_distress uses (scrapers run
# every 3h; a generous window avoids dropping a parcel on one missed cycle).
_FRESH = "now() - interval '4 days'"

# The per-ticket VALIDITY FLOOR — keep in sync with the gate_sql in market_config (see the
# module docstring INVARIANT). Residential is implied by the lead's existence, so it is not
# repeated here.
_FLOOR_PER_TICKET = (
    "s.payload->>'disposition' ILIKE 'Responsible%' "
    "AND s.payload->>'collection_status' = 'In Collections' "
    "AND s.payload->>'amt_balance_due' ~ '^[0-9.]+$' "
    "AND (s.payload->>'amt_balance_due')::numeric > 0"
)

# Tier cutoffs (Marc-tunable defaults, Jayden 2026-06-17).
_TIER_A_TICKETS, _TIER_A_BALANCE = 2, 2000
_TIER_B_TICKETS, _TIER_B_BALANCE = 2, 500

# absentee: owner-mailing zip5 differs from the situs zip5. zip is the robust discriminator
# (owner lives in a different zip than the property = likely absentee). NULL/unknown => false
# (conservative — unknown never inflates tier A). owner_mailing_address is the combined
# "addr, city, ST, zip" string; situs zip is payload->>'zip_code'.
_ABSENTEE_PER_TICKET = (
    "(regexp_match(s.payload->>'owner_mailing_address', '(\\d{5})(?:-\\d{4})?\\s*$'))[1] "
    "IS NOT NULL "
    "AND substring(s.payload->>'zip_code' from '\\d{5}') IS NOT NULL "
    "AND (regexp_match(s.payload->>'owner_mailing_address', '(\\d{5})(?:-\\d{4})?\\s*$'))[1] "
    "<> substring(s.payload->>'zip_code' from '\\d{5}')"
)


def _agg_cte() -> str:
    """Per-parcel aggregate of floor-passing, fresh blight tickets.

    AS MATERIALIZED is REQUIRED: this CTE is referenced once, so PG12+ would inline it into
    the UPDATE join and — because the JSONB gate makes the planner mis-estimate the row count
    as 1 — re-run the 204k-row signal seq scan PER listing row (~10-min hang). Materializing
    computes the aggregate ONCE (~few s), then the UPDATE index-joins on source_listing_id.
    """
    return f"""
        WITH agg AS MATERIALIZED (
            SELECT s.parcel_number,
                   count(*)                              AS tickets,
                   sum((s.payload->>'amt_balance_due')::numeric) AS total_bal,
                   bool_or({_ABSENTEE_PER_TICKET})       AS absentee
            FROM tranchi.signals s
            WHERE s.source = '{_SIGNAL_SOURCE}'
              AND s.signal_type = '{_SIGNAL_TYPE}'
              AND s.last_seen_at >= {_FRESH}
              AND ({_FLOOR_PER_TICKET})
            GROUP BY s.parcel_number
        )
    """


async def tier_wayne_blight_leads(pool: asyncpg.Pool, *, dry_run: bool = False) -> dict:
    """Stamp conviction_tier + raw drivers on active Wayne blight leads. Returns a stat dict."""
    async with pool.acquire() as conn:
        # No lead rows yet (type disabled / first run) => nothing to do; never crash the cron.
        n_leads = await conn.fetchval(
            "SELECT count(*) FROM tranchi.listings WHERE source_site=$1 AND status='active'",
            _SOURCE_SITE,
        )
        if not n_leads:
            logger.info("[wayne_blight_tiering] no active blight leads — skipping")
            return {"leads": 0, "tiered": 0}

        if dry_run:
            rows = await conn.fetch(
                _agg_cte() + """
                SELECT
                    CASE
                        WHEN tickets >= $1 AND total_bal >= $2 AND absentee THEN 'A'
                        WHEN tickets >= $3 AND total_bal >= $4 THEN 'B'
                        ELSE 'C'
                    END AS tier,
                    count(*) AS n
                FROM agg
                GROUP BY 1 ORDER BY 1
                """,
                _TIER_A_TICKETS, _TIER_A_BALANCE, _TIER_B_TICKETS, _TIER_B_BALANCE,
            )
            split = {r["tier"]: int(r["n"]) for r in rows}
            logger.info("[wayne_blight_tiering] DRY: %d active leads, tier split %s", n_leads, split)
            return {"leads": int(n_leads), "dry_run": True, "tier_split": split}

        result = await conn.execute(
            _agg_cte() + f"""
            UPDATE tranchi.listings l
            SET blight_ticket_count  = agg.tickets,
                blight_total_balance = agg.total_bal,
                absentee_owner       = agg.absentee,
                conviction_tier      = CASE
                    WHEN agg.tickets >= {_TIER_A_TICKETS} AND agg.total_bal >= {_TIER_A_BALANCE}
                         AND agg.absentee THEN 'A'
                    WHEN agg.tickets >= {_TIER_B_TICKETS} AND agg.total_bal >= {_TIER_B_BALANCE}
                         THEN 'B'
                    ELSE 'C'
                END
            FROM agg
            WHERE l.source_site = '{_SOURCE_SITE}'
              AND l.status = 'active'
              AND l.source_listing_id = agg.parcel_number
            """
        )
        tiered = int((result or "0").split()[-1]) if result else 0
        split = {
            r["conviction_tier"]: int(r["n"])
            for r in await conn.fetch(
                "SELECT conviction_tier, count(*) AS n FROM tranchi.listings "
                "WHERE source_site=$1 AND status='active' GROUP BY 1 ORDER BY 1",
                _SOURCE_SITE,
            )
        }
        logger.info("[wayne_blight_tiering] tiered %d/%d active leads, split %s",
                    tiered, n_leads, split)
        return {"leads": int(n_leads), "tiered": tiered, "tier_split": split}


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
        stats = await tier_wayne_blight_leads(pool, dry_run=args.dry_run)
        logger.info("wayne_blight_tiering done%s: %s", " [DRY RUN]" if args.dry_run else "", stats)
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
