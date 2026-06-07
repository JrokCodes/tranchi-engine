"""Add filing_date to tranchi.listings (Shelby probate auto-transfer)

Revision ID: 015
Revises: 014
Create Date: 2026-06-07

WHY: Shelby probate case numbers (PR#####) carry no filing year, so the Cuyahoga-style
`filing_year_substr` auto-transfer rule (run.py::_transfer_predicate) can't apply — that
was the tracked ITEM-1 deferral ("Shelby probate auto-transfer rule"). shelby_probate.py
already PARSES the court filing_date but discarded it; we now persist it onto the listing
and the new `filing_date` mode in the transfer predicate compares it directly:
a probate parcel that SOLD at/after its filing date is no longer an estate asset.

NON-BREAKING: nullable column; only shelby_probate.py writes it. Cuyahoga probate keeps
the year-substring rule (its case numbers encode the year). The shared upsert_listings
path writes it via COALESCE so a NULL from any non-probate scraper never clobbers.

Applied directly as postgres on EC2 + alembic_version bumped manually (UPDATE
alembic_version SET version_num='015'); this file exists for repo parity + local dev.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.listings
            ADD COLUMN IF NOT EXISTS filing_date DATE
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.listings
            DROP COLUMN IF EXISTS filing_date
    """)
