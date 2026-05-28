"""Address-quality tag: listings.address_status

Revision ID: 006
Revises: 005
Create Date: 2026-05-28

Adds tranchi.listings.address_status — a transparency tag for address quality.

WHY: ~135 active listings carry a street name with NO leading house number
("London Ave", "Coit Ave Unit:rear"). Investigation (2026-05-28) showed these
are NOT parse bugs and are NOT recoverable: the county's own MyPlace situs for
the same parcel also has no number, and the dominant land_use_code is 5000
(residential vacant land) with developer-LLC owners. They are real,
registry-confirmed VACANT-LAND / unnumbered-unit parcels — a vacant lot has no
structure, so the county assigns no street number. They stay in the feed (valid
land deals), but get flagged so the UI / verifier tell the user to confirm them
by PARCEL NUMBER on MyPlace, not by street address.

Values:
  NULL                — has a normal leading street number (the default; untagged).
  'no_street_number'  — address lacks a house number (vacant land / unnumbered unit).

Backfilled + maintained by run.py._flag_incomplete_addresses each scrape cycle.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.listings
            ADD COLUMN IF NOT EXISTS address_status TEXT
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_tranchi_listings_address_status
            ON tranchi.listings (address_status)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tranchi.idx_tranchi_listings_address_status")
    op.execute("""
        ALTER TABLE tranchi.listings
            DROP COLUMN IF EXISTS address_status
    """)
