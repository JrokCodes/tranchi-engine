"""Add tranchi.shelby_probate_cursor + trigram index for owner-name probate join

Revision ID: 010
Revises: 009
Create Date: 2026-06-02

Two additive changes for the Shelby (TN) probate scraper:

1. tranchi.shelby_probate_cursor — single-row (id=1) PR-case-number high-water mark,
   separate from Cuyahoga's tranchi.probate_cursor (different id namespace: PR######).
   The Shelby probate scraper walks PR-numbers forward from last_id. The PR space is
   dense + monotonic by filing date, so the walk is efficient.

   SEED: 32500 (≈ early-2025 filings) so the first backfill captures currently-OPEN
   estates without walking the full multi-year history. Adjust SHELBY_PROBATE_SEED
   before the first run, or bump the row directly, if a different floor is wanted.

2. pg_trgm GIN index on tranchi.parcels.owner_name — the precision-first name join
   (shelby_probate._resolve_by_owner_name) fetches candidates via
   `owner_name ILIKE '%surname%' AND owner_name ILIKE '%given%'`. Without a trigram
   index that is a seq scan of ~353K rows per case; the index makes it sub-ms.

INVARIANT: shelby_probate_cursor always has exactly one row (id=1). Never DELETE it.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS tranchi.shelby_probate_cursor (
            id          INT PRIMARY KEY CHECK (id = 1),
            last_id     INT NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        INSERT INTO tranchi.shelby_probate_cursor (id, last_id, updated_at)
        VALUES (1, 32500, NOW())
        ON CONFLICT (id) DO NOTHING
    """)

    # Trigram index for the owner_name ILIKE candidate fetch (Path B).
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_parcels_owner_name_trgm
        ON tranchi.parcels USING gin (owner_name gin_trgm_ops)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tranchi.idx_parcels_owner_name_trgm")
    op.execute("DROP TABLE IF EXISTS tranchi.shelby_probate_cursor")
