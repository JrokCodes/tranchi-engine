"""Probate decedent identity on the listing row: decedent_name, case_title, decedent_dod

Revision ID: 007
Revises: 006
Create Date: 2026-05-31

Adds tranchi.listings.{decedent_name, case_title, decedent_dod} (probate only).

WHY: the probate scraper parsed the decedent's name/case title/date-of-death but wrote
them ONLY to tranchi.signals.payload (jsonb). When the signal's FK to tranchi.parcels
gated (parcel not yet in the registry), the row was silently dropped — so the verifier
had no decedent name to compare against the parcel's current owner and fell back to
"candidate decedent = parcel owner", which is exactly WRONG on a mis-join. With the
name now on the listing row, every probate card can compare decedent vs current owner
directly (the precision-first join fix + this denormalization are paired — see
probate.py INVARIANT header and Babel reference/JOIN-PRECISION.md).

Values: NULL for all non-probate sources. Written by probate.py:_build_listing going
forward; backfilled once from signals.payload by scripts/reresolve_probate.py.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.listings
            ADD COLUMN IF NOT EXISTS decedent_name TEXT,
            ADD COLUMN IF NOT EXISTS case_title    TEXT,
            ADD COLUMN IF NOT EXISTS decedent_dod  DATE
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.listings
            DROP COLUMN IF EXISTS decedent_name,
            DROP COLUMN IF EXISTS case_title,
            DROP COLUMN IF EXISTS decedent_dod
    """)
