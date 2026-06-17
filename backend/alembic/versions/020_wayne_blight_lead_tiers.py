"""Wayne (Detroit, MI) blight pre-distress LEAD: tier columns + lead type (DISABLED)

Revision ID: 020
Revises: 019
Create Date: 2026-06-17

WHY: turn on Detroit blight-violation pre-distress LEADS, but instead of one hard
volume cutoff, surface EVERYTHING above a strict VALIDITY FLOOR and rank each lead by a
CONVICTION TIER so Marc filters in the dashboard (Jayden's direction 2026-06-17). The
floor lives in market_config.distress_lead_rules['blight_violation'].gate_sql (Responsible
disposition + In Collections + balance>0 + residential, ~43.8k floor parcels live-verified).
The tier + its raw drivers are computed per parcel by wayne_blight_tiering.py and stored on
the lead row by this migration's new columns:

  conviction_tier      A (2+ tickets & >=$2k & absentee) / B (2+ & >=$500) / C (rest)
  blight_ticket_count  count of floor-passing tickets on the parcel
  blight_total_balance sum of amt_balance_due across floor-passing tickets
  absentee_owner       owner-mailing city+zip != situs city+zip (motivated-seller signal)

These columns are generic NULL-able adds — buy-now rows keep them NULL, harmless. The
signals composite index makes the per-parcel aggregation (and the gate's residential
correlated subquery) an index scan, not a seq scan over ~204k blight rows.

INSERTED DISABLED (enabled=false): per the G1/G3 discipline, pre-distress surfaces only
AFTER the go-live verify pass. Then flip:
  UPDATE tranchi.distress_lead_types SET enabled=true WHERE market='wayne' AND signal_type='blight_violation';

NON-BREAKING: distress_lead_types is config; nothing surfaces while enabled=false. The
(market, signal_type) PK + UNIQUE(source_site) from migration 016 already exist.

Applied directly as postgres on EC2 + alembic_version bumped manually (UPDATE
alembic_version SET version_num='020'); this file exists for repo parity + local dev.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Lead tier columns (NULL on buy-now rows; stamped on blight leads by the tiering pass).
    op.execute(
        """
        ALTER TABLE tranchi.listings
            ADD COLUMN IF NOT EXISTS conviction_tier      text,
            ADD COLUMN IF NOT EXISTS blight_ticket_count  integer,
            ADD COLUMN IF NOT EXISTS blight_total_balance numeric,
            ADD COLUMN IF NOT EXISTS absentee_owner        boolean
        """
    )

    # 2) Partial index for the Pre-Distress tier filter (only distress_signal rows carry a tier).
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tranchi_listings_conviction_tier
            ON tranchi.listings (distress_stage, conviction_tier)
            WHERE distress_stage = 'distress_signal'
        """
    )

    # 3) Composite index so the per-parcel aggregation + gate residential correlated subquery
    #    are index scans, not seq scans over ~204k blight signal rows.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tranchi_signals_parcel_type_source
            ON tranchi.signals (parcel_number, signal_type, source)
        """
    )

    # 4) Register the blight LEAD type, DISABLED. signal_source='detroit_blight_tickets' scopes
    #    it to the Detroit blight signal feed; source_site matches market_config source_sites.
    op.execute(
        """
        INSERT INTO tranchi.distress_lead_types
            (signal_type, enabled, label, signal_source, source_site, market)
        VALUES
            ('blight_violation', false, 'Detroit Blight Violations', 'detroit_blight_tickets',
             'Wayne Blight (Lead)', 'wayne')
        ON CONFLICT (market, signal_type) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM tranchi.distress_lead_types WHERE market='wayne' AND signal_type='blight_violation'")
    op.execute("DROP INDEX IF EXISTS tranchi.idx_tranchi_signals_parcel_type_source")
    op.execute("DROP INDEX IF EXISTS tranchi.idx_tranchi_listings_conviction_tier")
    op.execute(
        """
        ALTER TABLE tranchi.listings
            DROP COLUMN IF EXISTS conviction_tier,
            DROP COLUMN IF EXISTS blight_ticket_count,
            DROP COLUMN IF EXISTS blight_total_balance,
            DROP COLUMN IF EXISTS absentee_owner
        """
    )
