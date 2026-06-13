"""Summit (Akron, OH) pre-distress lead type (DISABLED until buy-now is verified)

Revision ID: 017
Revises: 016
Create Date: 2026-06-13

WHY: register the Summit County tax-delinquent SIGNAL (SC720_DELQ certified tape,
~28.3k parcels, written to tranchi.signals by summit_delinquent_tax.py with
source='summit_fiscal_office') as a Pre-Distress LEAD type — the Memphis/Cuyahoga
pattern and Summit's lever to the ~5k bar (buy-now alone is ~1-1.5k).

The defensible-slice GATE (certified balance >= $2k AND residential land-use, Summit
LUC 5xx) lives in market_config.distress_lead_rules['tax_delinquent'].gate_sql and is
applied by surface_distress.py — NOT here. This row just declares the lead type + which
market signal_source feeds it. signal_source='summit_fiscal_office' scopes it to Summit
so its 'tax_delinquent' never mixes with Cuyahoga's (cuyahoga_fiscal_officer) or Shelby's
(shelby_county_trustee).

INSERTED DISABLED (enabled=false): per Jayden's G1 ruling (2026-06-11), pre-distress is
surfaced only AFTER buy-now is verified. After buy-now G3 sign-off, run
`surface_distress --dry-run`, sanity-check the slice, then flip enabled=true
(UPDATE tranchi.distress_lead_types SET enabled=true WHERE market='summit').

NON-BREAKING: distress_lead_types is config. Nothing surfaces while enabled=false.
The (market, signal_type) PK + UNIQUE(source_site) from migration 016 already exist.

Applied directly as postgres on EC2 + alembic_version bumped manually (UPDATE
alembic_version SET version_num='017'); this file exists for repo parity + local dev.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO tranchi.distress_lead_types
            (signal_type, enabled, label, signal_source, source_site, market)
        VALUES
            ('tax_delinquent', false, 'Tax Delinquent (Certified)', 'summit_fiscal_office',
             'Summit Tax Delinquent (Lead)', 'summit')
        ON CONFLICT (market, signal_type) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM tranchi.distress_lead_types WHERE market = 'summit'")
