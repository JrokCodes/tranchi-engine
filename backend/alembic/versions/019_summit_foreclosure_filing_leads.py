"""Summit foreclosure-filing pre-distress lead type (DISABLED until buy-now verified)

Revision ID: 019
Revises: 018
Create Date: 2026-06-13

WHY: register the Summit foreclosure-FILING signal (Akron Legal News Common Pleas
foreclosure complaints, written to tranchi.signals by summit_foreclosure_filings.py
with source='summit_aln_foreclosures', signal_type='foreclosure_filing') as a
Pre-Distress LEAD type. This is the earliest distress-pipeline stage — a complaint
filed months before any sheriff sale — so it surfaces a motivated owner while still
reachable. The G1 "8th source" delta (Jayden approved 2026-06-13).

The lead rule (address from spine, gate_sql=None — every fresh filing surfaces, since a
filing is itself the escalation) lives in market_config.distress_lead_rules and is applied
by surface_distress.py — NOT here. signal_source='summit_aln_foreclosures' scopes it to
Summit.

INSERTED DISABLED (enabled=false): per Jayden's G1 buy-now-first discipline — surfaced only
AFTER buy-now is verified. Flip enabled=true (UPDATE tranchi.distress_lead_types SET
enabled=true WHERE market='summit' AND signal_type='foreclosure_filing') after the
surface_distress --dry-run check, alongside the tax_delinquent lead (migration 017).

NON-BREAKING: config row only; nothing surfaces while enabled=false. (market, signal_type)
PK + UNIQUE(source_site) from migration 016 already exist.

Applied directly as postgres on EC2 + alembic_version bumped manually (UPDATE
alembic_version SET version_num='019'); this file exists for repo parity + local dev.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO tranchi.distress_lead_types
            (signal_type, enabled, label, signal_source, source_site, market)
        VALUES
            ('foreclosure_filing', false, 'Foreclosure Filing (Pre-Sale)', 'summit_aln_foreclosures',
             'Summit Foreclosure Filing (Lead)', 'summit')
        ON CONFLICT (market, signal_type) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM tranchi.distress_lead_types "
        "WHERE market = 'summit' AND signal_type = 'foreclosure_filing'"
    )
