"""Composite parcel identity: parcels PK + signals FK -> (parcel_number, market)

Revision ID: 021
Revises: 020
Create Date: 2026-06-21

WHY (must-close #6 / F-008, lucas-toledo-build/PARCEL-IDENTITY-FIX-PLAN.md):
tranchi.parcels PK = `parcel_number` ALONE. Lucas's canonical deal parcel is PARID =
7-digit zero-padded ('1210314'; RealAuction '12-10314' = dash-stripped) — FORM-IDENTICAL
to Summit. The market-blind normalize_parcel_number() routes any 7-digit numeric to the
Summit branch, so Lucas PARIDs land in Summit's parcel namespace. Because the parcel
upserts use `ON CONFLICT (parcel_number) DO NOTHING`, when a Lucas PARID equals an existing
Summit parcel the second-inserted market's row is silently DROPPED (first-insert wins) =
silent data LOSS. Measured live: 8,529 Lucas∩Summit collisions, 0 vs cuyahoga/shelby/wayne
(192,691 PARID ∩ 260,778 summit). Composite identity (parcel_number, market) makes two
counties' same-numbered parcels coexist; market then disambiguates the join.

ZERO-BEHAVIOR-CHANGE for the 4 live markets: their parcel_number strings do not collide
across markets today (mig 013 backfill note: OH dashed / TN 14-char / Wayne verbatim are
mutually exclusive; Summit 7-digit only collides with the not-yet-loaded Lucas), so the
composite key selects exactly the same rows. Proof harness:
scripts/parcel_identity_proof.py (per-market parcel/listing/signal counts identical
before/after; 0 NULL-market; 0 orphan signals; verify_listings N/N VALID on all 4).

PAIRED CODE EDITS (same commit) — every parcel upsert/read is market-scoped:
  - fiscal_officer.py: market-scoped existence probe + UPDATE WHERE; ON CONFLICT
    (parcel_number, market).
  - code_violations.py: stub ON CONFLICT (parcel_number, market). (The signals upsert
    conflicts on the natural key (parcel_number, signal_type, source, date) — left alone.)
  - run.py _ensure_parcels_for_listings: DISTINCT ON (source_listing_id, market), JOIN
    `AND p.market = l.market`, ON CONFLICT (parcel_number, market).
All runtime read paths (listings router, surface_distress, run.py mark-transferred,
verify_listings, quality_audit) already join `p.market = l.market`.

NOT APPLIED to the live EC2 DB by this build (operator decision: STOP at a clean proof,
hand the flip to a human). When applied: run inside ONE transaction with the version bump
(UPDATE tranchi.alembic_version SET version_num='021'), off-cron — steps 5-6 take an
ACCESS EXCLUSIVE lock on tranchi.parcels.

ROLLBACK NOTE: downgrade() restores the single-column PK and is clean ONLY before any Lucas
data exists. After Lucas ingest the 8,529 Lucas∩Summit collisions make the single-column PK
un-restorable — go-live on Lucas is the one-way commit point. Reverting the paired ON CONFLICT
edits while the composite PK exists is INVALID (`ON CONFLICT (parcel_number)` needs a matching
single-column unique constraint), so treat migration + code edits as one pre-Lucas rollback unit.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Guard: the composite PK/FK cannot include a NULL market. mig 013/014 backfilled
    #    existing rows and the writers now stamp market on every insert, so this MUST be 0.
    #    Abort loudly (re-run the mig-013 backfill UPDATEs) rather than silently proceed.
    op.execute("""
        DO $$
        DECLARE p_null int; s_null int;
        BEGIN
            SELECT count(*) INTO p_null FROM tranchi.parcels WHERE market IS NULL;
            SELECT count(*) INTO s_null FROM tranchi.signals WHERE market IS NULL;
            IF p_null > 0 OR s_null > 0 THEN
                RAISE EXCEPTION
                    'mig 021 abort: % parcels / % signals have NULL market — backfill (mig 013 logic) first',
                    p_null, s_null;
            END IF;
        END $$;
    """)

    # 2. Pre-flight: every signal must have a matching (parcel_number, market) parcel or the
    #    composite FK ADD will fail. Must be 0 (the single-col FK already guaranteed the
    #    parcel_number side; market is consistent because signals.market is copied from the
    #    parcel at write time). Abort with the count if not.
    op.execute("""
        DO $$
        DECLARE orphans int;
        BEGIN
            SELECT count(*) INTO orphans
            FROM tranchi.signals s
            LEFT JOIN tranchi.parcels p
                   ON p.parcel_number = s.parcel_number AND p.market = s.market
            WHERE p.parcel_number IS NULL;
            IF orphans > 0 THEN
                RAISE EXCEPTION
                    'mig 021 abort: % signals have no (parcel_number, market) parent — reconcile first',
                    orphans;
            END IF;
        END $$;
    """)

    # 3. market is now part of identity — enforce NOT NULL (idempotent; guard above proved 0 NULLs).
    op.execute("ALTER TABLE tranchi.parcels ALTER COLUMN market SET NOT NULL")
    op.execute("ALTER TABLE tranchi.signals ALTER COLUMN market SET NOT NULL")

    # 4. Drop the single-column FK before touching the referenced PK.
    op.execute("ALTER TABLE tranchi.signals DROP CONSTRAINT IF EXISTS signals_parcel_number_fkey")

    # 5. Swap the parcels PK: parcel_number -> (parcel_number, market). ACCESS EXCLUSIVE.
    op.execute("ALTER TABLE tranchi.parcels DROP CONSTRAINT IF EXISTS parcels_pkey")
    op.execute("ALTER TABLE tranchi.parcels ADD PRIMARY KEY (parcel_number, market)")

    # 6. Re-add the FK on the composite key. ON DELETE CASCADE preserved from mig 001.
    op.execute("""
        ALTER TABLE tranchi.signals
            ADD CONSTRAINT signals_parcel_number_market_fkey
            FOREIGN KEY (parcel_number, market)
            REFERENCES tranchi.parcels (parcel_number, market)
            ON DELETE CASCADE
    """)


def downgrade() -> None:
    # Reverse order. SAFE ONLY before Lucas data exists (see ROLLBACK NOTE in the docstring):
    # ADD PRIMARY KEY (parcel_number) fails once Lucas∩Summit collisions are present.
    op.execute("ALTER TABLE tranchi.signals DROP CONSTRAINT IF EXISTS signals_parcel_number_market_fkey")
    op.execute("ALTER TABLE tranchi.parcels DROP CONSTRAINT IF EXISTS parcels_pkey")
    op.execute("ALTER TABLE tranchi.parcels ADD PRIMARY KEY (parcel_number)")
    op.execute("""
        ALTER TABLE tranchi.signals
            ADD CONSTRAINT signals_parcel_number_fkey
            FOREIGN KEY (parcel_number)
            REFERENCES tranchi.parcels (parcel_number)
            ON DELETE CASCADE
    """)
    # market NOT NULL is left in place (harmless; columns predate this migration as nullable
    # but every live row is non-NULL). Drop explicitly only if a true full revert is needed:
    #   ALTER TABLE tranchi.parcels ALTER COLUMN market DROP NOT NULL;
    #   ALTER TABLE tranchi.signals ALTER COLUMN market DROP NOT NULL;
