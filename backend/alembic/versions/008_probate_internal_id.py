"""Persist ProWare internal case id: listings.probate_internal_id

Revision ID: 008
Revises: 007
Create Date: 2026-05-31

Adds tranchi.listings.probate_internal_id (BIGINT, nullable, probate only).

WHY: probate cases are fetched from ProWare by an internal integer id
(CaseSummary.aspx?q=base64(id)), but that id was never stored — so re-fetching a
case later required a fuzzy Case Search by number. Persisting the id lets any future
re-check / decedent re-fetch hit the case in ONE request by id. NULL for non-probate
and for legacy rows (those are recovered via Case Search in scripts/backfill_probate_decedent.py).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.listings
            ADD COLUMN IF NOT EXISTS probate_internal_id BIGINT
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.listings
            DROP COLUMN IF EXISTS probate_internal_id
    """)
