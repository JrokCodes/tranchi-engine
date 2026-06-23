"""025 lucas tax_delinquent -> full AREIS COLLECTION source.

Repoints the lucas tax_delinquent pre-distress LEAD from the partial TLN Treasurer-filed-cases
feed (lucas_tln_tfn, ~13 leads) to the FULL county-wide certified-delinquent roll from the
public AREIS COLLECTION table (lucas_areis_collection, ~11.7k RULE-#1-gated leads). The TLN feed
(lucas_delinquent_tax) and the vacant GIS feed remain as corroborating signals for HOT-stacking.

The market_config lucas distress_lead_rules['tax_delinquent'] gate (RULE #1: balance >= $2000
AND residential LUC 5xx) is unchanged — lucas_areis_delinquent emits the same payload keys
(delq_amount, luc).

Revision ID: 025
Revises: 024
"""
from typing import Union

from alembic import op

revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE tranchi.distress_lead_types "
        "SET signal_source = 'lucas_areis_collection' "
        "WHERE market = 'lucas' AND signal_type = 'tax_delinquent'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE tranchi.distress_lead_types "
        "SET signal_source = 'lucas_tln_tfn' "
        "WHERE market = 'lucas' AND signal_type = 'tax_delinquent'"
    )
