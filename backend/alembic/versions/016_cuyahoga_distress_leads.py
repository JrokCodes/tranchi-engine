"""Cuyahoga pre-distress lead types + market-scope the distress_lead_types PK

Revision ID: 016
Revises: 015
Create Date: 2026-06-07

WHY: surface Cuyahoga's existing distress SIGNALS (tax_delinquent ~27k, code_violation
~29.6k — already in tranchi.signals) as Pre-Distress LEADS, the Memphis pattern. The
config table tranchi.distress_lead_types (migration 012) was keyed by signal_type ALONE,
which can't hold two markets that share a signal_type (both Cuyahoga + Shelby have
'tax_delinquent'). Repoint the PK to (market, signal_type), add the market column, backfill
the existing rows as Shelby, then add the two Cuyahoga rows (enabled).

The defensible-slice GATES (tax balance >= $2k OR foreclosure; code_violation OPEN + cited
<=24mo) live in market_config.distress_lead_rules and are applied by surface_distress.py —
not here. These rows just declare the lead type + which market signal_source feeds it.
source_site is the per-lead-source key used across the engine, so it's also made UNIQUE.

NON-BREAKING: distress_lead_types is config (2 rows pre-migration). Nothing surfaces until
surface_distress.py runs with these rows enabled. enabled=false is Marc's per-type kill
switch (next run retires that whole type).

Applied directly as postgres on EC2 + alembic_version bumped manually (UPDATE
alembic_version SET version_num='016'); this file exists for repo parity + local dev.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add market; backfill the two existing rows (Shelby) before NOT NULL.
    op.execute("ALTER TABLE tranchi.distress_lead_types ADD COLUMN IF NOT EXISTS market TEXT")
    op.execute("UPDATE tranchi.distress_lead_types SET market = 'shelby' WHERE market IS NULL")
    op.execute("ALTER TABLE tranchi.distress_lead_types ALTER COLUMN market SET NOT NULL")

    # 2. Repoint PK signal_type -> (market, signal_type) so two markets can share a type.
    op.execute("ALTER TABLE tranchi.distress_lead_types DROP CONSTRAINT IF EXISTS distress_lead_types_pkey")
    op.execute("ALTER TABLE tranchi.distress_lead_types ADD PRIMARY KEY (market, signal_type)")

    # 3. source_site is the engine-wide per-lead-source key (scrape_runs, listings, verify) — unique.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_distress_lead_types_source_site "
        "ON tranchi.distress_lead_types (source_site)"
    )

    # 4. Cuyahoga lead types. signal_source scopes each to OH signals so Cuyahoga's like-named
    #    'tax_delinquent' never mixes with Shelby's (shelby_county_trustee).
    #    - tax_delinquent ENABLED: ~14.3k defensible leads (balance >= $2k OR foreclosure), fresh,
    #      clears the 5k target on its own.
    #    - code_violation inserted DISABLED: the cleveland_open_data feed is a DELTA pull, so a
    #      status-refresh cron (`run.py --site code_violations --full`, every ~3 days) must re-pull
    #      live Open/Closed status + freshness BEFORE surfacing. After the first refresh validates a
    #      sane Open+<=24mo slice, flip enabled=true (UPDATE ... SET enabled=true). Gate lives in
    #      market_config.distress_lead_rules.code_violation.
    op.execute(
        """
        INSERT INTO tranchi.distress_lead_types
            (signal_type, enabled, label, signal_source, source_site, market)
        VALUES
            ('tax_delinquent', true, 'Tax Delinquent (Lawsuit)', 'cuyahoga_fiscal_officer',
             'Cuyahoga Tax Delinquent (Lead)', 'cuyahoga'),
            ('code_violation', false, 'Code Violation (Open)', 'cleveland_open_data',
             'Cuyahoga Code Violation (Lead)', 'cuyahoga')
        ON CONFLICT (market, signal_type) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM tranchi.distress_lead_types WHERE market = 'cuyahoga'")
    op.execute("DROP INDEX IF EXISTS tranchi.uq_distress_lead_types_source_site")
    op.execute("ALTER TABLE tranchi.distress_lead_types DROP CONSTRAINT IF EXISTS distress_lead_types_pkey")
    op.execute("ALTER TABLE tranchi.distress_lead_types ADD PRIMARY KEY (signal_type)")
    op.execute("ALTER TABLE tranchi.distress_lead_types DROP COLUMN IF EXISTS market")
