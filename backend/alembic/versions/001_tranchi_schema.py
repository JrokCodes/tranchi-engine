"""Initial tranchi schema

Revision ID: 001
Revises:
Create Date: 2026-05-23

Tables:
  tranchi.listings    — core listing registry (sheriff, land bank, probate)
  tranchi.parcels     — Cuyahoga parcel identity spine (from Fiscal Officer)
  tranchi.signals     — cross-source distressed-property signals
  tranchi.scrape_runs — per-source run stats for Sources dashboard

INVARIANT: tranchi.signals.parcel_number must reference a row in tranchi.parcels
before the signal can be inserted. The FK is enforced by the schema. Scrapers
that don't have a parcel_number yet (e.g. land_bank before parcel join) should
write to tranchi.listings only and leave signals empty until the join runs.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS tranchi")

    # ── tranchi.listings ──────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS tranchi.listings (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_site         TEXT NOT NULL,
            case_number         TEXT,
            property_address    TEXT NOT NULL,
            property_city       TEXT,
            property_county     TEXT,
            property_state      TEXT NOT NULL DEFAULT 'OH',
            property_zip        TEXT,
            sale_date           DATE,
            sale_time           TEXT,
            sale_location       TEXT,
            deposit_usd         NUMERIC,
            trustee_name        TEXT,
            status              TEXT DEFAULT 'active',
            pipeline_status     TEXT DEFAULT 'new',
            first_seen_at       TIMESTAMPTZ DEFAULT NOW(),
            last_seen_at        TIMESTAMPTZ DEFAULT NOW(),
            normalized_address  TEXT,
            duplicate_of        UUID REFERENCES tranchi.listings(id),
            signal_type         TEXT,
            source_listing_id   TEXT
        )
    """)

    # ── tranchi.parcels ───────────────────────────────────────────────────────
    # Identity spine populated by fiscal_officer.py. Probate scraper joins
    # decedent names here to find owned parcels. One parcel_number = one land record.
    op.execute("""
        CREATE TABLE IF NOT EXISTS tranchi.parcels (
            parcel_number           TEXT PRIMARY KEY,
            owner_name              TEXT,
            owner_mailing_address   TEXT,
            situs_address           TEXT,
            property_class          TEXT,
            land_use_code           TEXT,
            acreage                 NUMERIC,
            year_built              INTEGER,
            sq_ft                   INTEGER,
            beds                    INTEGER,
            baths                   NUMERIC,
            last_sale_date          DATE,
            last_sale_price         NUMERIC,
            current_market_value    NUMERIC,
            taxable_value           NUMERIC,
            current_tax_balance     NUMERIC,
            delinquent_flag         BOOLEAN DEFAULT FALSE,
            school_district         TEXT,
            neighborhood            TEXT,
            ward                    TEXT,
            first_seen_at           TIMESTAMPTZ DEFAULT NOW(),
            last_seen_at            TIMESTAMPTZ DEFAULT NOW(),
            source_url              TEXT
        )
    """)

    # ── tranchi.signals ───────────────────────────────────────────────────────
    # Cross-source distressed-property signal store.
    # Each scraper that detects distress writes a signal row. The signal-stack
    # join aggregates (parcel → N signals) to score deal heat.
    # confidence: 0.0–1.0 — how certain we are this signal is valid for this parcel.
    op.execute("""
        CREATE TABLE IF NOT EXISTS tranchi.signals (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            parcel_number   TEXT NOT NULL REFERENCES tranchi.parcels(parcel_number) ON DELETE CASCADE,
            signal_type     TEXT NOT NULL,
            source          TEXT NOT NULL,
            observed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            confidence      FLOAT CHECK (confidence >= 0.0 AND confidence <= 1.0),
            payload         JSONB DEFAULT '{}',
            first_seen_at   TIMESTAMPTZ DEFAULT NOW(),
            last_seen_at    TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # ── tranchi.scrape_runs ───────────────────────────────────────────────────
    # One row per scraper per run. stat shape mirrors Gotham Sources dashboard.
    # found     = raw rows from source before prefilter
    # passed    = after prefilter (eligible for upsert)
    # active    = currently active in DB (deduped, not stale)
    # filtered  = rejected by prefilter
    # dupes     = flagged duplicate_of in cross-source dedup pass
    # delisted  = flipped to not_listed (seen before, gone now)
    # expired   = sale_date past, marked expired
    # new_today = first_seen_at == today ET
    op.execute("""
        CREATE TABLE IF NOT EXISTS tranchi.scrape_runs (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_site     TEXT NOT NULL,
            started_at      TIMESTAMPTZ NOT NULL,
            completed_at    TIMESTAMPTZ,
            status          TEXT NOT NULL DEFAULT 'running',
            found           INT DEFAULT 0,
            passed          INT DEFAULT 0,
            active          INT DEFAULT 0,
            filtered        INT DEFAULT 0,
            dupes           INT DEFAULT 0,
            delisted        INT DEFAULT 0,
            expired         INT DEFAULT 0,
            new_today       INT DEFAULT 0,
            error_message   TEXT
        )
    """)

    # ── Indexes ───────────────────────────────────────────────────────────────

    # listings — primary access patterns
    op.execute("CREATE INDEX IF NOT EXISTS idx_tranchi_listings_norm_addr    ON tranchi.listings(normalized_address)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_tranchi_listings_source_case  ON tranchi.listings(source_site, case_number)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_tranchi_listings_status       ON tranchi.listings(status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_tranchi_listings_sale_date    ON tranchi.listings(sale_date)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_tranchi_listings_first_seen   ON tranchi.listings(first_seen_at DESC)")

    # parcels — owner-name search (fiscal officer + probate name join)
    op.execute("CREATE INDEX IF NOT EXISTS idx_tranchi_parcels_owner_name    ON tranchi.parcels(owner_name)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_tranchi_parcels_situs_addr    ON tranchi.parcels(situs_address)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_tranchi_parcels_delinquent    ON tranchi.parcels(delinquent_flag) WHERE delinquent_flag = TRUE")

    # signals — parcel roll-up query (N signals per parcel)
    op.execute("CREATE INDEX IF NOT EXISTS idx_tranchi_signals_parcel        ON tranchi.signals(parcel_number)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_tranchi_signals_type          ON tranchi.signals(signal_type)")

    # scrape_runs — Sources dashboard latest-run lookup
    op.execute("CREATE INDEX IF NOT EXISTS idx_tranchi_scrape_runs_src_date  ON tranchi.scrape_runs(source_site, started_at DESC)")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS tranchi CASCADE")
