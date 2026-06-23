"""023 lucas probate lead — distress_lead_types row (DISABLED).

Adds the Lucas (OH / Toledo) probate pre-distress LEAD config row so surface_distress.py
can materialize deceased-owner estates as distress_stage='distress_signal' leads:

  - probate   signal_source='lucas_tln_probate'
              source_site  ='Lucas Probate Court'
              (gate_sql=None — every matched deceased-owner parcel surfaces; market_config
               lucas distress_lead_rules['probate'] supplies the spine address fragments)

Signal feed: lucas_probate.py parses Toledo Legal News daily probate filing lists
(public; the county's own eAccess portal is dead and re:SearchOH is registration-gated),
keeps ESTATE openings, and joins decedent name -> Lucas AREIS owner (unique-match,
match_confidence='probable').

INSERTED DISABLED (enabled=false): same G1 discipline as 019/020/022 — flip enabled=true
after surface_distress --dry-run sanity-check:
    UPDATE tranchi.distress_lead_types SET enabled=true
    WHERE market='lucas' AND signal_type='probate';

NON-BREAKING: config row only; nothing surfaces while enabled=false.

Revision ID: 023
Revises: 022
"""
from typing import Union

from alembic import op

revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO tranchi.distress_lead_types
            (signal_type, enabled, label, signal_source, source_site, market)
        VALUES
            ('probate', false, 'Probate Estate (Deceased Owner)', 'lucas_tln_probate',
             'Lucas Probate Court', 'lucas')
        ON CONFLICT (market, signal_type) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM tranchi.distress_lead_types "
        "WHERE market = 'lucas' AND signal_type = 'probate'"
    )
