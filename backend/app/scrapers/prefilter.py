"""
Hard filter rules applied to every RawListing BEFORE database insert.

Rules (intentionally loose — Marc: "pull everything, regardless of price"):
  1. property_state must be in ALLOWED_STATES (active markets)
  2. property_address must be non-null and non-empty

ALLOWED_STATES is an allowlist of the states we have live markets in. A new
market in a new state MUST be added here or every one of its listings is
silently rejected (passed=0) before the DB write — there is no error, just an
empty feed. (This exact rule dropped 100% of the Shelby/TN listings until TN
was added — see scrape_runs.filtered.)

All Gotham-specific filters have been dropped:
  - No deposit ceiling (Marc: users filter on the Tranchi side)
  - No county whitelist (Cuyahoga is the default geography but not enforced here)
  - No multi-unit exclusion (condos, apartments, all welcome)
  - No cancelled-status rejection (surface everything, status is informational)

Keep this file so the hook exists for future tightening if Marc changes direction.
"""
from __future__ import annotations

from app.market_config import all_states
from app.scrapers.models import RawListing

# Active-market states — derived from the market registry (app/market_config.py) so a
# new market's state is allowed automatically when its MarketConfig is added (no second
# edit here to forget). Currently {"OH", "TN"}.
ALLOWED_STATES = all_states()


def prefilter(listing: RawListing) -> tuple[bool, str | None]:
    """Return (passes, rejection_reason).

    passes=True means the listing cleared all hard filters and should proceed
    to the DB upsert. passes=False means it should be discarded silently.
    """
    # Rule 1: must be an active-market state (allowlist)
    if listing.property_state and listing.property_state.upper() not in ALLOWED_STATES:
        return False, f"state={listing.property_state!r} (not in {sorted(ALLOWED_STATES)})"

    # Rule 2: Address must be present
    if not listing.property_address or not listing.property_address.strip():
        return False, "property_address is null or empty"

    return True, None
