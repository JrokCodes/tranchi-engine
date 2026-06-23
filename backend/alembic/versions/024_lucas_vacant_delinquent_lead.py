"""024 lucas vacant-delinquent lead — distress_lead_types row (DISABLED).

Adds the Lucas vacant+tax-delinquent pre-distress LEAD config row. Signal feed:
lucas_vacant_delinquent.py queries the Auditor's public hosted ArcGIS layer
(Hosted/Vacant_Delinquent___100) and emits `vacant_delinquent` signals (parcel + balance
+ luc). The shared RULE #1 gate (balance >= $2000 AND residential LUC 5xx) lives in
market_config lucas distress_lead_rules['vacant_delinquent'].

INSERTED DISABLED (enabled=false): flip enabled=true after surface_distress --dry-run:
    UPDATE tranchi.distress_lead_types SET enabled=true
    WHERE market='lucas' AND signal_type='vacant_delinquent';

NON-BREAKING: config row only.

Revision ID: 024
Revises: 023
"""
from typing import Union

from alembic import op

revision: str = "024"
down_revision: Union[str, None] = "023"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO tranchi.distress_lead_types
            (signal_type, enabled, label, signal_source, source_site, market)
        VALUES
            ('vacant_delinquent', false, 'Vacant + Tax Delinquent', 'lucas_areis_vacant_delinquent',
             'Lucas Vacant Delinquent (Lead)', 'lucas')
        ON CONFLICT (market, signal_type) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM tranchi.distress_lead_types "
        "WHERE market = 'lucas' AND signal_type = 'vacant_delinquent'"
    )
