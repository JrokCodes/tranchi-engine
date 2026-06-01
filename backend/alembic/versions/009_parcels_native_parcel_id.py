"""Add native_parcel_id to tranchi.parcels for Shelby County Trustee links

Revision ID: 009
Revises: 008
Create Date: 2026-06-01

Adds tranchi.parcels.native_parcel_id (TEXT, nullable).

WHY: The Shelby County Trustee parcel-lookup URL requires the county's native
SPACED parcel form (e.g. '042035  00007'), URL-encoded as '042035%20%2000007'.
Our canonical 14-char parcel_number ('04203500000070') cannot be reliably
reversed to the spaced form because alpha-qualified MAP codes (e.g. 'D02170')
are lossy in the canonical encoding direction. ReGIS returns the spaced form
directly as PARCELID; we store it here verbatim (spaces preserved) so
verify_listings.py can build one-click Trustee URLs for any Shelby parcel.

NULL for all non-Shelby parcels (Cuyahoga hits won't have this key).
Backfill: re-run shelby_parcels spine after applying this migration.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.parcels
            ADD COLUMN IF NOT EXISTS native_parcel_id TEXT
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.parcels
            DROP COLUMN IF EXISTS native_parcel_id
    """)
