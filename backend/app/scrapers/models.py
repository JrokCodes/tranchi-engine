"""
Shared Pydantic schema for raw listings scraped from deal-sourcing sites.
All scrapers return list[RawListing] before prefilter + DB upsert.
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class RawListing(BaseModel):
    source_site: str
    property_address: str
    property_city: str | None = None
    property_county: str | None = None
    property_state: str = "OH"
    property_zip: str | None = None
    sale_date: date | None = None
    sale_time: str | None = None
    deposit_usd: float | None = None
    case_number: str | None = None
    trustee_name: str | None = None
    sale_location: str | None = None
    status: str = "active"
    # Cross-source signal tagging — set by each scraper to categorize the
    # distress type (e.g. 'probate', 'tax_delinquent_foreclosure', 'land_bank_inventory').
    # Used downstream by the signal-stack join to count co-occurring distress signals.
    signal_type: str | None = None
    # Native source identifier (parcel #, case #, listing ID) preserved verbatim
    # from the upstream source. Stored alongside our internal case_number for
    # cross-referencing without normalization loss.
    source_listing_id: str | None = None


class ScrapeResult(BaseModel):
    source_site: str
    # found    = raw rows extracted from source before any filtering.
    # passed   = after prefilter (eligible for upsert).
    # active   = currently active in DB after dedup/stale-detection.
    # filtered = rejected by prefilter.
    # dupes    = flagged as duplicate_of another listing.
    # delisted = flipped to not_listed (seen before, now gone).
    # expired  = sale_date past, marked expired.
    # new_today = first_seen_at == today (new listings this run).
    found: int = 0
    passed: int = 0
    active: int = 0
    filtered: int = 0
    dupes: int = 0
    delisted: int = 0
    expired: int = 0
    new_today: int = 0
    new_inserted: int = 0
    updated: int = 0
    errors: int = 0
    error_message: str | None = None
