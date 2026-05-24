"""Add auction outcome + bid/appraised fields to tranchi.listings

Revision ID: 004
Revises: 003
Create Date: 2026-05-24

Adds four columns to tranchi.listings:
  - auction_status        TEXT    — raw upstream auction outcome/state, preserved
                                    verbatim (scheduled / sold / forfeit to state /
                                    withdrawn / cancelled / bankruptcy / vacated …).
                                    Distinct from the coarse `status` (active/expired)
                                    that drives the deal view. WHY: sheriff.py used to
                                    collapse the outcome into status='expired' and DROP
                                    the detail — but forfeit-to-state (acquirable) and
                                    withdrawn (motivated owner) are real leads while
                                    'sold' is not. Without this column that lead-vs-noise
                                    distinction is unrecoverable.
  - opening_bid_usd        NUMERIC — minimum/opening bid (DLN min_bid; tax = taxes+cost).
  - appraised_value_usd    NUMERIC — appraised value (DLN appr_value).
  - sec_sale_date          DATE    — second-offer / re-offer date when the first sale
                                     gets no sufficient bid (DLN sec_sale_date).

All nullable; existing rows get NULL. No backfill — scrapers populate going forward.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.listings
            ADD COLUMN IF NOT EXISTS auction_status     TEXT,
            ADD COLUMN IF NOT EXISTS opening_bid_usd     NUMERIC,
            ADD COLUMN IF NOT EXISTS appraised_value_usd NUMERIC,
            ADD COLUMN IF NOT EXISTS sec_sale_date       DATE
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.listings
            DROP COLUMN IF EXISTS auction_status,
            DROP COLUMN IF EXISTS opening_bid_usd,
            DROP COLUMN IF EXISTS appraised_value_usd,
            DROP COLUMN IF EXISTS sec_sale_date
    """)
