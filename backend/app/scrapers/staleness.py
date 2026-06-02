"""
Per-source staleness policy for tranchi.listings retirement.

INVARIANT (do not remove — this prevents a silent data-corruption regression):
Retirement logic is NOT one-size-fits-all. Each source signals "this listing is
gone" differently, so applying time-not-seen retirement blindly corrupts
cursor-walk and archive sources:

  - FULL_RESCAN — the scraper re-fetches its ENTIRE live set every run (DLN feed,
    Land Bank list). A listing absent from this cycle is genuinely gone (sold /
    cancelled / removed) → safe to mark not_listed by "last_seen_at < run_start".

  - CURSOR — the scraper only fetches NEW records forward from a saved cursor and
    NEVER re-visits old ones (probate ID-walk). "Not seen this cycle" is ALWAYS
    true for every prior row, so time-not-seen retirement wrongly retires the
    entire back-catalog. THIS WAS THE MAY 2026 BUG: 6,446 probate cases wrongly
    flipped to not_listed. Cursor sources retire ONLY when a periodic status
    re-check finds the underlying case CLOSED (see probate case_status sweep).

  - ARCHIVE — historical records that never "go away" (past sheriff sales kept for
    lead recovery). Never stale.

New listing sources MUST be added to SOURCE_STALENESS below. Unmapped sources fall
back to DEFAULT_POLICY (FULL_RESCAN) — correct for the common full-feed case, but
a new CURSOR/ARCHIVE source left unmapped will be wrongly retired. Map it.
"""
from __future__ import annotations

from enum import Enum


class StalenessPolicy(str, Enum):
    FULL_RESCAN = "full_rescan"
    CURSOR = "cursor"
    ARCHIVE = "archive"


# Keyed by listing source_site (scraper.site_name as stored in tranchi.listings).
SOURCE_STALENESS: dict[str, StalenessPolicy] = {
    "Cuyahoga Sheriff Sale (DLN)": StalenessPolicy.FULL_RESCAN,
    "Cuyahoga Land Bank": StalenessPolicy.FULL_RESCAN,
    # Forfeited-land re-pulls the whole locator each run; a parcel that drops off
    # (redeemed/sold/new cycle) is genuinely gone → safe to retire by absence.
    "Cuyahoga Forfeited Land": StalenessPolicy.FULL_RESCAN,
    "Cuyahoga Probate Court": StalenessPolicy.CURSOR,
    "Cuyahoga Sheriff Sales": StalenessPolicy.ARCHIVE,
    # Shelby County, TN (Memphis) — all FULL_RESCAN: each re-pulls its whole live set
    # every run, so a row absent this cycle is genuinely resolved (paid / sold / removed).
    "Shelby County Tax Sale": StalenessPolicy.FULL_RESCAN,
    # Foreclosure re-pulls both readers' full current set each run; tnforeclosurenotices
    # also updates the PP (postponed) Sale Date in place, so absence = sold/cancelled.
    "Shelby County Foreclosure": StalenessPolicy.FULL_RESCAN,
    "Shelby County Land Bank": StalenessPolicy.FULL_RESCAN,
    "Memphis MMLBA": StalenessPolicy.FULL_RESCAN,
}

DEFAULT_POLICY = StalenessPolicy.FULL_RESCAN


def policy_for(source_site: str) -> StalenessPolicy:
    return SOURCE_STALENESS.get(source_site, DEFAULT_POLICY)


def full_rescan_sources(candidates: list[str]) -> list[str]:
    """Return the subset of candidate source_sites that use time-not-seen staling.

    Only FULL_RESCAN sources are safe to retire by "absent from this cycle".
    """
    return [s for s in candidates if policy_for(s) == StalenessPolicy.FULL_RESCAN]
