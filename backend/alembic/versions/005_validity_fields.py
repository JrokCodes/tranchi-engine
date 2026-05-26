"""Validity-enforcement fields: probate case status, match confidence, lien age, signal status

Revision ID: 005
Revises: 004
Create Date: 2026-05-26

Adds the columns that let Tranchi *enforce* (not just scrape) the validity rules
Marc named on the May-2026 call:

tranchi.listings
  - case_status        TEXT  — probate case status verbatim from the court page
                               (OPEN / PENDING / CLOSED / DISPOSED / ...). Parsed
                               since launch but previously DISCARDED. Marc's #1 rule
                               is "open cases only"; without storing this it was
                               unenforceable. NULL for non-probate sources.
  - case_status_date   DATE  — the court's "Status Date" for the above.
  - match_method       TEXT  — how the parcel was joined to the decedent:
                               'address_anchor' | 'name_match' | 'composite'.
  - match_confidence   TEXT  — tier driving display: 'confirmed' | 'probable' |
                               'unverified'. An unverified (name-only fuzzy) join is
                               the one real mis-join risk — it must never present as
                               confirmed. NULL for non-probate sources.
  - match_score        NUMERIC — raw 0..1 corroboration score behind the tier.

tranchi.parcels   (lien age — derived from MyPlace "Tax By Year" enrichment)
  - tax_years_delinquent  INTEGER — count of delinquent tax years (lien age proxy).
  - first_delinquent_year INTEGER — earliest unpaid tax year.
  - tax_status_flags      TEXT    — MyPlace flags (Foreclosure / Cert Pending /
                                    Cert Sold / Payment Plan), comma-joined.
  - tax_enriched_at       TIMESTAMPTZ — last LegacyTaxes enrichment time.

tranchi.signals
  - status  TEXT DEFAULT 'open' — code-violation lifecycle (open/closed/pending).
            A delta-by-date scrape misses later closures; this lets the audit expire
            resolved signals so they stop counting as live distress.

All nullable / defaulted; existing rows backfilled by separate jobs (probate
case_status re-check, fiscal_officer tax enrichment), not in this migration.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.listings
            ADD COLUMN IF NOT EXISTS case_status      TEXT,
            ADD COLUMN IF NOT EXISTS case_status_date DATE,
            ADD COLUMN IF NOT EXISTS match_method     TEXT,
            ADD COLUMN IF NOT EXISTS match_confidence TEXT,
            ADD COLUMN IF NOT EXISTS match_score      NUMERIC
    """)
    op.execute("""
        ALTER TABLE tranchi.parcels
            ADD COLUMN IF NOT EXISTS tax_years_delinquent  INTEGER,
            ADD COLUMN IF NOT EXISTS first_delinquent_year INTEGER,
            ADD COLUMN IF NOT EXISTS tax_status_flags      TEXT,
            ADD COLUMN IF NOT EXISTS tax_enriched_at       TIMESTAMPTZ
    """)
    op.execute("""
        ALTER TABLE tranchi.signals
            ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'open'
    """)
    # Index probate validity filters used by the read API.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_tranchi_listings_case_status
            ON tranchi.listings (case_status)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_tranchi_listings_match_conf
            ON tranchi.listings (match_confidence)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tranchi.idx_tranchi_listings_match_conf")
    op.execute("DROP INDEX IF EXISTS tranchi.idx_tranchi_listings_case_status")
    op.execute("""
        ALTER TABLE tranchi.signals
            DROP COLUMN IF EXISTS status
    """)
    op.execute("""
        ALTER TABLE tranchi.parcels
            DROP COLUMN IF EXISTS tax_years_delinquent,
            DROP COLUMN IF EXISTS first_delinquent_year,
            DROP COLUMN IF EXISTS tax_status_flags,
            DROP COLUMN IF EXISTS tax_enriched_at
    """)
    op.execute("""
        ALTER TABLE tranchi.listings
            DROP COLUMN IF EXISTS case_status,
            DROP COLUMN IF EXISTS case_status_date,
            DROP COLUMN IF EXISTS match_method,
            DROP COLUMN IF EXISTS match_confidence,
            DROP COLUMN IF EXISTS match_score
    """)
