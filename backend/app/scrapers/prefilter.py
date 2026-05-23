"""
Hard filter rules applied to every RawListing BEFORE database insert.

Rules (intentionally loose — Marc: "pull everything, regardless of price"):
  1. property_state must be "OH"
  2. property_address must be non-null and non-empty

All Gotham-specific filters have been dropped:
  - No deposit ceiling (Marc: users filter on the Tranchi side)
  - No county whitelist (Cuyahoga is the default geography but not enforced here)
  - No multi-unit exclusion (condos, apartments, all welcome)
  - No cancelled-status rejection (surface everything, status is informational)

Keep this file so the hook exists for future tightening if Marc changes direction.
"""
from __future__ import annotations

from app.scrapers.models import RawListing


def prefilter(listing: RawListing) -> tuple[bool, str | None]:
    """Return (passes, rejection_reason).

    passes=True means the listing cleared all hard filters and should proceed
    to the DB upsert. passes=False means it should be discarded silently.
    """
    # Rule 1: OH only
    if listing.property_state and listing.property_state.upper() != "OH":
        return False, f"state={listing.property_state!r} (not OH)"

    # Rule 2: Address must be present
    if not listing.property_address or not listing.property_address.strip():
        return False, "property_address is null or empty"

    return True, None
