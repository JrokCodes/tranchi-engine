"""026 dayton probate cursor table.

Adds tranchi.dayton_probate_cursor — the single-row cursor that tracks the
caseyear + last_casenbr position for the Montgomery County (OH / Dayton) Probate
cursor-walk scraper (dayton_probate.py). The scraper uses this row to resume from
where the previous run left off so each 3h cycle only fetches NEW case numbers.

Schema mirrors tranchi.shelby_probate_cursor and tranchi.summit_probate_cursor.

Revision ID: 026
Revises: 025
"""
from typing import Union

from alembic import op

revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tranchi.dayton_probate_cursor (
            id            INTEGER PRIMARY KEY DEFAULT 1,
            caseyear      INTEGER NOT NULL DEFAULT 2026,
            last_casenbr  INTEGER NOT NULL DEFAULT 0,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (id = 1)
        )
        """
    )
    op.execute(
        """
        INSERT INTO tranchi.dayton_probate_cursor (id, caseyear, last_casenbr)
            VALUES (1, 2026, 0)
            ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tranchi.dayton_probate_cursor")
