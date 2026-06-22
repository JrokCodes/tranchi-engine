"""022 lucas distress leads — tax_delinquent + foreclosure_filing (DISABLED).

Adds the two Lucas (OH / Toledo) pre-distress LEAD config rows to
tranchi.distress_lead_types so surface_distress.py can materialize them as
distress_stage='distress_signal' leads:

  - tax_delinquent      signal_source='lucas_tln_tfn'
                        source_site ='Lucas Tax Delinquent (Lead)'
                        (gate = RULE #1: certified balance >= $2000 AND residential
                         LUC 5xx — already wired in market_config lucas distress_lead_rules)
  - foreclosure_filing  signal_source='lucas_tln_foreclosures'
                        source_site ='Lucas Foreclosure Filing (Lead)'
                        (gate_sql=None — every fresh filing surfaces; market_config wired)

INSERTED DISABLED (enabled=false): same G1 buy-now-first discipline as Summit (019) and
Wayne (020). A row only surfaces leads AFTER surface_distress --dry-run is sanity-checked
and enabled is flipped true:
    UPDATE tranchi.distress_lead_types SET enabled=true
    WHERE market='lucas' AND signal_type IN ('tax_delinquent','foreclosure_filing');

NON-BREAKING: distress_lead_types is config — nothing surfaces while enabled=false. The
(market, signal_type) PK + UNIQUE(source_site) from migration 016 already exist.

Revision ID: 022
Revises: 021
"""
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO tranchi.distress_lead_types
            (signal_type, enabled, label, signal_source, source_site, market)
        VALUES
            ('tax_delinquent', false, 'Tax Delinquent (Lawsuit)', 'lucas_tln_tfn',
             'Lucas Tax Delinquent (Lead)', 'lucas'),
            ('foreclosure_filing', false, 'Foreclosure Filing (Pre-Sale)', 'lucas_tln_foreclosures',
             'Lucas Foreclosure Filing (Lead)', 'lucas')
        ON CONFLICT (market, signal_type) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM tranchi.distress_lead_types "
        "WHERE market = 'lucas' AND signal_type IN ('tax_delinquent', 'foreclosure_filing')"
    )
