"""Summit probate cursor table

Revision ID: 018
Revises: 017
Create Date: 2026-06-13

WHY: summit_probate.py (CourtView eServices / Wicket) walks OPEN Estate (ES) cases by a
rolling fileDateRange window. It persists the end of the last fully-fetched window in
tranchi.summit_probate_cursor so weekly runs pick up only new filings (mirrors
tranchi.shelby_probate_cursor / tranchi.probate_cursor). Single-row table (id=1 enforced
by CHECK). Bootstraps last_window_end to today-365 so the first run backfills a year.

NON-BREAKING: new table, no existing data touched. Probate staleness is CURSOR (retired
only by case_status re-check), so this cursor drives forward discovery, not retirement.

Applied directly as postgres on EC2 + alembic_version bumped manually (UPDATE
alembic_version SET version_num='018'); this file exists for repo parity + local dev.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tranchi.summit_probate_cursor (
            id              INTEGER PRIMARY KEY DEFAULT 1,
            last_window_end DATE NOT NULL,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (id = 1)
        )
        """
    )
    op.execute(
        """
        INSERT INTO tranchi.summit_probate_cursor (id, last_window_end)
        VALUES (1, (CURRENT_DATE - INTERVAL '365 days')::date)
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tranchi.summit_probate_cursor")
