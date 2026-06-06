"""Distress-stage axis + surfaced pre-distress leads

Revision ID: 012
Revises: 011
Create Date: 2026-06-06

WHY: Tranchi sources two fundamentally different things and users want to filter
between them. Until now the feed only showed "buy now" deals (tax sale, land bank,
foreclosure, probate, county-owned) — ~4.2k active in Shelby. But the engine already
holds ~22k *signal* parcels (tax_delinquent lawsuit list 16k, eviction 6k) that are
genuine off-market distressed LEADS the owner has not listed — exactly the inventory
Marc described wanting ("source properties with an open tax lien for >= 12 months";
"$20k owed on a $100k house"). Those signals were invisible (HOT-stacking fuel only).

This migration adds a SEPARATE orthogonal axis — distress_stage — so we can surface
the signal parcels as listings WITHOUT polluting the default buy-now feed:
  - buy_now         = an actively-acquirable deal (current behavior, all existing rows)
  - distress_signal = a pre-distress LEAD materialized from a signal (NEW)

DESIGN (mirrors migration 011's "separate axis, not overloaded status" precedent):
distress_stage is its own column, NOT a new listings.status value. status stays the
coarse liveness axis (active/expired/not_listed/transferred/...) every post-pass + read
gate keys on. The read API defaults to distress_stage='buy_now' so existing consumers
and the default UI feed are unchanged; the "Pre-Distress" toggle requests the leads.
Column DEFAULTs 'buy_now' so every existing AND future buy-now source is correct with
no code change — only the _surface_distress_leads post-pass writes 'distress_signal'.

The leads are materialized into tranchi.listings (NOT a read-side view) so they inherit
the full validity stack for free: parcel-keyed cross-source dedup (a lead auto-hides via
duplicate_of if a real buy-now listing later appears on the same parcel), the
no-transfer off-market guard (_mark_transferred_listings — the parcel's last_sale_date
proxy that keeps the feed ~off-market), HOT stacking, the read gates, and the daily
verification sample. They are retired by the surfacing pass itself when their underlying
signal disappears (parcel redeemed / no longer delinquent / evicted-and-sold) — see
_surface_distress_leads. Their source_site is NOT in SOURCE_STALENESS on purpose: the
generic _mark_stale guard only acts on FULL_RESCAN scraper sources with a scrape_run,
and these have neither — the pass owns their lifecycle.

distress_lead_types is the per-type kill switch Marc curates: flip enabled=false and the
next pass retires that whole type (revert toward the clean ~4.2k buy-now view) with no
redeploy. Seeded with the two live signal types, both enabled.

Applied directly as postgres on EC2 + alembic_version bumped manually (UPDATE
alembic_version SET version_num='012'); this file exists for repo parity + local dev.
"""

from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE tranchi.listings
            ADD COLUMN IF NOT EXISTS distress_stage TEXT NOT NULL DEFAULT 'buy_now'
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_tranchi_listings_distress_stage
            ON tranchi.listings (distress_stage, status)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS tranchi.distress_lead_types (
            signal_type   TEXT PRIMARY KEY,   -- signals.signal_type to surface
            enabled       BOOLEAN NOT NULL DEFAULT true,   -- Marc's per-type kill switch
            label         TEXT,               -- human label for the chip/catalog
            signal_source TEXT NOT NULL,      -- signals.source filter (scopes to a market)
            source_site   TEXT NOT NULL,      -- the lead listing's source_site (display + dedup)
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        INSERT INTO tranchi.distress_lead_types (signal_type, enabled, label, signal_source, source_site)
        VALUES
            ('tax_delinquent', true, 'Tax Delinquent (Lawsuit)', 'shelby_county_trustee', 'Shelby Tax Delinquent (Lead)'),
            ('eviction',       true, 'Eviction (Tired Landlord)', 'shelby_general_sessions', 'Shelby Eviction (Lead)')
        ON CONFLICT (signal_type) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tranchi.distress_lead_types")
    op.execute("DROP INDEX IF EXISTS tranchi.idx_tranchi_listings_distress_stage")
    op.execute("ALTER TABLE tranchi.listings DROP COLUMN IF EXISTS distress_stage")
