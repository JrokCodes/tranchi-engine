"""Add tranchi.probate_cursor for ID-enumeration state

Revision ID: 002
Revises: 001
Create Date: 2026-05-23

Table: tranchi.probate_cursor
  Single-row table (enforced by check constraint). Holds the highest
  internal probate court int ID that has been successfully ingested.
  The probate scraper reads last_id on each cron run and resumes from
  last_id + 1, walking forward until N consecutive 404s/non-estate results.

INVARIANT: exactly one row exists (id=1). Bootstrap INSERT is idempotent
(ON CONFLICT DO NOTHING). The scraper must never DELETE this row.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS tranchi.probate_cursor (
            id          INT PRIMARY KEY CHECK (id = 1),
            last_id     INT NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # Seed with a known-good starting point: internal ID 2818155 is the
    # sample case (2026EST305113) discovered in the Playwright probe.
    # We back up ~500 IDs so the first run catches any cases filed just
    # before the probe date. Adjust PROBATE_CURSOR_SEED env var to override.
    op.execute("""
        INSERT INTO tranchi.probate_cursor (id, last_id, updated_at)
        VALUES (1, 2817655, NOW())
        ON CONFLICT (id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tranchi.probate_cursor")
