"""
Shared Pydantic schemas for scraped data.

RawListing — deal-sourcing scrapers (sheriff, land bank, probate) that write
             to tranchi.listings. All scrapers return list[RawListing] before
             prefilter + DB upsert.

RawSignal  — signal scrapers (code violations, fiscal officer flags) that
             write to tranchi.signals. Signals tag parcels; they are NOT
             listings. No prefilter applied — every signal row is upserted.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

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
    # Raw upstream auction outcome/state, preserved verbatim and SEPARATE from the
    # coarse `status` (active/expired). Lets forfeit-to-state (acquirable) and
    # withdrawn (motivated owner) survive as leads instead of collapsing to 'expired'.
    auction_status: str | None = None
    # Minimum/opening bid (DLN min_bid; tax = taxes + cost). Appraised value (DLN appr_value).
    opening_bid_usd: float | None = None
    appraised_value_usd: float | None = None
    # Second-offer / re-offer date when the first sale gets no sufficient bid.
    sec_sale_date: date | None = None
    # Cross-source signal tagging — set by each scraper to categorize the
    # distress type (e.g. 'probate', 'tax_delinquent_foreclosure', 'land_bank_inventory').
    # Used downstream by the signal-stack join to count co-occurring distress signals.
    signal_type: str | None = None
    # Native source identifier (parcel #, case #, listing ID) preserved verbatim
    # from the upstream source. Stored alongside our internal case_number for
    # cross-referencing without normalization loss.
    source_listing_id: str | None = None


class RawSignal(BaseModel):
    """A per-parcel distress signal — lands in tranchi.signals, not tranchi.listings.

    Used by SignalScraper subclasses (code violations, fiscal officer distress flags).
    parcel_number is the raw value from the upstream source (8-digit for Cleveland
    violations, DDD-NN-NNN for cross-source joins). confidence is 0.0–1.0.

    payload holds all source-specific fields as JSONB. Keys should be stable
    across runs so the downstream signal-stack join can filter on payload fields.
    """
    parcel_number: str              # raw parcel ID from source
    signal_type: str                # e.g. 'code_violation'
    source: str                     # e.g. 'cleveland_open_data'
    observed_at: datetime           # when the signal was observed (violation file date)
    confidence: float = 1.0        # 0.0–1.0
    payload: dict[str, Any] = {}   # JSONB — source-specific fields


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
