"""Add market to tranchi.listings + tranchi.signals (market-isolation, ITEM-1 #10)

Revision ID: 014
Revises: 013
Create Date: 2026-06-07

WHY: migration 013 added `market` to tranchi.parcels. This extends the disambiguator to
the other two core tables so EVERY parcel join can be scoped `AND x.market = y.market`
instead of `property_state` (the state-level proxy). property_state fully disambiguates
the markets that exist TODAY (OH vs TN — formats can't even collide), so this is NOT a
live-bug fix; it future-proofs the engine for a SAME-STATE second county (e.g. a second
TN/OH county) where property_state would no longer distinguish two markets and per-county
parcel numbers CAN collide. ITEM-1 #10 of _FUTURE-ARCHITECTURE-HANDOFF.md.

DISAMBIGUATOR = MARKET (county-level), NOT STATE (see migration 013).

NON-BREAKING / ADDITIVE: both columns are nullable; the join CUTOVER (property_state ->
market at the call-sites) is a separate code change deployed alongside. This migration only
adds the columns, backfills, and indexes — verified independently (0 NULL) before any read
flips to `market`.

BACKFILL is deterministic (verified on live data 2026-06-07: 0 listings have NULL
property_state; 100% of signals reference a parcel that already carries a market):
  LISTINGS
    1. From the authoritative parcel: market = parcels.market for the listing's parcel,
       scoped by property_state so a same-number parcel in another state can't bleed in.
    2. From property_state for rows with no parcel row (expired/superseded leads, 1.7k):
       OH -> cuyahoga, TN -> shelby (the only markets today; the WRITER sets market on new
       rows going forward, so this state->market map is a one-time backfill, not a runtime
       assumption).
  SIGNALS
    From the parcel (signals.parcel_number FK -> parcels.market); 100% coverage.

Applied directly as postgres on EC2 + alembic_version bumped manually (UPDATE
alembic_version SET version_num='014'); this file exists for repo parity + local dev.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.listings ADD COLUMN IF NOT EXISTS market TEXT;
        ALTER TABLE tranchi.signals  ADD COLUMN IF NOT EXISTS market TEXT;
    """)

    # ── listings ──────────────────────────────────────────────────────────────
    # 1. Authoritative: the listing's parcel carries the market (scoped by state so a
    #    same parcel-number in another state cannot bleed across — defensive; formats
    #    can't collide today).
    op.execute("""
        UPDATE tranchi.listings l
            SET market = p.market
        FROM tranchi.parcels p
        WHERE l.source_listing_id = p.parcel_number
          AND l.property_state = p.property_state
          AND l.market IS NULL
          AND p.market IS NOT NULL
    """)
    # 2. Rows with no parcel row (expired/superseded; ~1.7k) -> from property_state.
    op.execute("""
        UPDATE tranchi.listings
            SET market = CASE property_state
                             WHEN 'OH' THEN 'cuyahoga'
                             WHEN 'TN' THEN 'shelby'
                         END
        WHERE market IS NULL
          AND property_state IN ('OH', 'TN')
    """)

    # ── signals ───────────────────────────────────────────────────────────────
    # From the parcel (FK guarantees the parcel exists; it already carries a market).
    op.execute("""
        UPDATE tranchi.signals s
            SET market = p.market
        FROM tranchi.parcels p
        WHERE s.parcel_number = p.parcel_number
          AND s.market IS NULL
          AND p.market IS NOT NULL
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_tranchi_listings_market ON tranchi.listings (market);
        CREATE INDEX IF NOT EXISTS idx_tranchi_signals_market  ON tranchi.signals  (market);
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tranchi.idx_tranchi_listings_market")
    op.execute("DROP INDEX IF EXISTS tranchi.idx_tranchi_signals_market")
    op.execute("ALTER TABLE tranchi.listings DROP COLUMN IF EXISTS market")
    op.execute("ALTER TABLE tranchi.signals  DROP COLUMN IF EXISTS market")
