"""TN tax-deed redemption lifecycle fields

Revision ID: 011
Revises: 010
Create Date: 2026-06-02

Tennessee is a REDEEMABLE tax-deed state (TCA 67-5-2701 et seq): a tax sale does NOT
end the deal. The buyer holds a deed SUBJECT TO REDEMPTION until a statutory window
closes — the former owner / heirs / lienholders can pay off and claw the property back.
The redemption clock starts at the Chancery Court "Order Confirming the Tax Sale"
(NOT the auction date — it can lag the sale by up to ~45 business days), and the window
length depends on delinquency age: <=5yr -> 1 year; 5-7yr -> 180 days; 8yr+ -> 90 days;
vacant/abandoned -> 30 days; IRS lien -> 120 days from sale.

Until this lands, shelby_tax_sale.py is correct ONLY as a PRE-sale catalog. These fields
+ the run.py post-passes (_compute_redemption_windows / _finalize_expired_redemptions) +
the Chancery confirmation reader (shelby_tax_confirmation.py) make the source post-sale
correct so Marc is never shown a deal that has been redeemed.

DESIGN: redemption lives on a SEPARATE redemption_status axis, NOT new listings.status
values. listings.status is the coarse liveness axis every post-pass + the read filter key
on (active/expired/not_listed/transferred); overloading it would force edits to every
WHERE clause and risk a mid-redemption row being wrongly retired or excluded from dedup.
This mirrors the auction_status precedent (migration 004). So:
  - mid-redemption (SPECULATIVE deal) = status='active'   + redemption_status='pending'
  - redeemed (KILL)                   = status='transferred' + redemption_status='redeemed'
  - window elapsed, not redeemed      = status='final'    + redemption_status='final'

tranchi.listings (all nullable; populated going forward by the Chancery reader +
post-passes, no backfill in this migration — same convention as 004/005):
  - confirmation_order_date  DATE        — Chancery order date = redemption clock start
  - redemption_window_days   INTEGER     — resolved tier length (365/180/90/30/120)
  - redemption_ends          DATE        — confirmation_order_date + window (or sale_date
                                           + 120 for the IRS-lien tier)
  - redemption_status        TEXT        — NULL | pending | redeemed | final
  - redemption_basis         TEXT        — audit: how the window was derived
                                           (le_5yr | 5_to_7yr | 8yr_plus | vacant_abandoned
                                            | irs_lien | default_assumed)
  - sale_outcome             TEXT        — sold | struck_off | no_bid (from Chancery /
                                           absence-from-catalog inference)
  - redemption_checked_at    TIMESTAMPTZ — last time the Chancery reader touched this row

NOTE: EC2 alembic is broken (NoSuchModuleError on the EC2 interpreter). On EC2 the SQL
below is applied DIRECTLY as postgres and alembic_version bumped manually
(UPDATE alembic_version SET version_num='011'). This file exists for repo parity + local
history. Both paths run the identical idempotent ADD COLUMN IF NOT EXISTS statements.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.listings
            ADD COLUMN IF NOT EXISTS confirmation_order_date DATE,
            ADD COLUMN IF NOT EXISTS redemption_window_days  INTEGER,
            ADD COLUMN IF NOT EXISTS redemption_ends         DATE,
            ADD COLUMN IF NOT EXISTS redemption_status       TEXT,
            ADD COLUMN IF NOT EXISTS redemption_basis        TEXT,
            ADD COLUMN IF NOT EXISTS sale_outcome            TEXT,
            ADD COLUMN IF NOT EXISTS redemption_checked_at   TIMESTAMPTZ
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_tranchi_listings_redemption_status
            ON tranchi.listings (redemption_status)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_tranchi_listings_redemption_ends
            ON tranchi.listings (redemption_ends)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tranchi.idx_tranchi_listings_redemption_ends")
    op.execute("DROP INDEX IF EXISTS tranchi.idx_tranchi_listings_redemption_status")
    op.execute("""
        ALTER TABLE tranchi.listings
            DROP COLUMN IF EXISTS redemption_checked_at,
            DROP COLUMN IF EXISTS sale_outcome,
            DROP COLUMN IF EXISTS redemption_basis,
            DROP COLUMN IF EXISTS redemption_status,
            DROP COLUMN IF EXISTS redemption_ends,
            DROP COLUMN IF EXISTS redemption_window_days,
            DROP COLUMN IF EXISTS confirmation_order_date
    """)
