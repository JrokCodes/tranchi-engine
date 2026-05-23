"""Add unique index on tranchi.signals natural key

Revision ID: 003
Revises: 002
Create Date: 2026-05-23

Adds the unique index required for ON CONFLICT (parcel_number, signal_type,
source, (observed_at::date)) in code_violations.py's upsert_signals to
function correctly. Without this index, re-runs duplicate every signal row.

Index: uq_tranchi_signals_natural_key
  Columns: parcel_number, signal_type, source, (observed_at::date)
  The functional expression (observed_at::date) collapses multiple intraday
  inserts for the same parcel+type+source into one row.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOTE: (observed_at::date) is NOT immutable for a timestamptz column (the
    # cast depends on session TimeZone), and Postgres rejects non-IMMUTABLE
    # expressions in an index. (observed_at AT TIME ZONE 'UTC')::date IS
    # immutable — AT TIME ZONE on a timestamptz yields a plain timestamp
    # deterministically, and timestamp::date is immutable. Any ON CONFLICT
    # naming this index must use the identical expression.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tranchi_signals_natural_key
            ON tranchi.signals (parcel_number, signal_type, source, ((observed_at AT TIME ZONE 'UTC')::date))
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tranchi.uq_tranchi_signals_natural_key")
