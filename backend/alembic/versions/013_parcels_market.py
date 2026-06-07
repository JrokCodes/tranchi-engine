"""Add market + property_state to tranchi.parcels (market-isolation foundation)

Revision ID: 013
Revises: 012
Create Date: 2026-06-06

WHY: tranchi.parcels had NO market/state column, so any parcel/owner join was
market-blind — the root of the cross-market bug class (the 2026-06-06 audit confirmed
no LIVE contamination, but the structure was vulnerable, and adding a 3rd market made it
worse). This is ITEM 1 of Clients/Marc/tranchi/markets/_FUTURE-ARCHITECTURE-HANDOFF.md.

DISAMBIGUATOR = MARKET (county-level), NOT STATE. Parcel numbers are assigned per-county,
so two same-state counties (e.g. Cuyahoga + a future second OH county) can share a
parcel-number string; only `market` disambiguates them. `property_state` is kept as a
useful attribute for state-level UI/filtering. Cross-market joins become
`... AND p.market = l.market` (added at call-sites in the follow-up session).

NON-BREAKING: both columns are nullable; no existing read/write path references them yet
(the call-site migration that reads `market` is the next focused session). This migration
only adds the columns, backfills existing rows, and indexes `market`.

BACKFILL is deterministic — OH and TN parcel formats are mutually exclusive (verified on
live data 2026-06-06: 55,870 OH dashed `DDD-NN-NNN` + 353,722 TN 14-char + 56 stubs;
353,429 carry native_parcel_id which ONLY the shelby_parcels spine sets):
  1. TN/shelby  = native_parcel_id present OR parcel_number is the 14-char canonical form.
  2. OH/cuyahoga = everything else (OH dashed/variant formats + numeric stubs; OH is the
     original/bulk market).
  3. Corrective: a parcel referenced only by TN listings is shelby (catches odd stubs like
     'EPP-TEST'). Formats can't collide, so a parcel maps to exactly one market.

NOTE FOR THE FOLLOW-UP SESSION: parcel WRITERS (fiscal_officer / shelby_parcels spine,
run.py ensure-parcels, code_violations / dln stub upserts) do NOT yet set `market`, so
parcels created after this migration land with market=NULL until those upserts are wired
to set it (call-site work). Re-run this backfill (idempotent) or wire the writers then.

Applied directly as postgres on EC2 + alembic_version bumped manually (UPDATE
alembic_version SET version_num='013'); this file exists for repo parity + local dev.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.parcels
            ADD COLUMN IF NOT EXISTS market TEXT,
            ADD COLUMN IF NOT EXISTS property_state TEXT
    """)

    # 1. TN/shelby — definitive markers (native_parcel_id is set ONLY by the shelby spine;
    #    the 14-char alnum canonical form is TN-only and never matches OH's DDD-NN-NNN).
    op.execute("""
        UPDATE tranchi.parcels
            SET market = 'shelby', property_state = 'TN'
        WHERE market IS NULL
          AND (native_parcel_id IS NOT NULL OR parcel_number ~ '^[0-9A-Za-z]{14}$')
    """)

    # 2. Everything else defaults to OH/cuyahoga (the original/bulk market + OH formats + stubs).
    op.execute("""
        UPDATE tranchi.parcels
            SET market = 'cuyahoga', property_state = 'OH'
        WHERE market IS NULL
    """)

    # 3. Corrective — a parcel referenced ONLY by TN listings is shelby (catches odd stubs
    #    like 'EPP-TEST' that fell through to the OH default). Formats can't collide, so a
    #    parcel resolves to exactly one market; the OH-direction needs no symmetric fix.
    op.execute("""
        UPDATE tranchi.parcels p
            SET market = 'shelby', property_state = 'TN'
        WHERE p.market = 'cuyahoga'
          AND EXISTS (SELECT 1 FROM tranchi.listings l
                        WHERE l.source_listing_id = p.parcel_number AND l.property_state = 'TN')
          AND NOT EXISTS (SELECT 1 FROM tranchi.listings l
                        WHERE l.source_listing_id = p.parcel_number AND l.property_state = 'OH')
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_tranchi_parcels_market
            ON tranchi.parcels (market)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tranchi.idx_tranchi_parcels_market")
    op.execute("""
        ALTER TABLE tranchi.parcels
            DROP COLUMN IF EXISTS market,
            DROP COLUMN IF EXISTS property_state
    """)
