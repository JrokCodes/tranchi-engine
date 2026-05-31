"""
Tranchi Engine — Listings router
GET /api/v1/listings          — paginated, filterable listing table with signal stacking
GET /api/v1/listings/{id}     — full detail: listing + parcel + signals

No auth required (Cloudflare gates the public hostname).

INVARIANT — signal join key: listings.source_listing_id = signals.parcel_number = parcels.parcel_number
All three carry the display-format parcel number (e.g. "541-12-123"). listings has NO parcel_number column.
INVARIANT — is_hot = (# distinct distress DIMENSIONS) >= 2. The listing's own source is one
dimension; stacked signals (code violations, tax flags, probate) add more. Multiple records of
the same type (e.g. 3 code-violation notices) count as ONE dimension. Computed in Python from the
per-parcel type_counts aggregate (see _build_signal_types).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.database import get_db
from app.services.streetview import build_street_view_url

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)
logger = logging.getLogger(__name__)

_VALID_SORT = {"first_seen_at", "signal_count", "sale_date", "address"}
_VALID_ORDER = {"asc", "desc"}

# ─────────────────────────────────────────────────────────────────────────────
# Pydantic response models
# ─────────────────────────────────────────────────────────────────────────────


class SignalTypeChip(BaseModel):
    """One distinct distress dimension on a listing's parcel (e.g. {"Code Violation", 3})."""
    label: str
    count: int


class ListingItem(BaseModel):
    id: UUID
    source_site: str
    signal_type: str | None
    property_address: str
    property_city: str | None
    property_county: str | None
    property_state: str
    property_zip: str | None
    status: str | None
    pipeline_status: str | None
    sale_date: date | None
    sec_sale_date: date | None
    deposit_usd: float | None
    opening_bid_usd: float | None
    appraised_value_usd: float | None
    auction_status: str | None
    trustee_name: str | None
    case_number: str | None
    source_listing_id: str | None
    # Probate validity + parcel→decedent join quality (null for non-probate).
    case_status: str | None
    case_status_date: date | None
    match_method: str | None
    match_confidence: str | None
    match_score: float | None
    # 'no_street_number' = real registry-confirmed parcel (usu. vacant land) the county
    # lists without a house number; verify by parcel #, not street address. NULL = normal.
    address_status: str | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    signal_count: int
    signal_types: list[SignalTypeChip]
    signal_type_count: int
    is_hot: bool
    # Parcel fields (null if no parcel match)
    owner_name: str | None
    situs_address: str | None
    current_market_value: float | None
    current_tax_balance: float | None
    delinquent_flag: bool
    # Google Street View Static API URL, built on the fly from the address.
    # None when GOOGLE_MAPS_API_KEY is unset → frontend shows a placeholder.
    street_view_url: str | None = None


class ListingPage(BaseModel):
    items: list[ListingItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class ParcelDetail(BaseModel):
    parcel_number: str
    owner_name: str | None
    situs_address: str | None
    owner_mailing_address: str | None
    current_market_value: float | None
    taxable_value: float | None
    current_tax_balance: float | None
    delinquent_flag: bool
    year_built: int | None
    sq_ft: int | None
    beds: int | None
    baths: float | None
    last_sale_date: date | None
    last_sale_price: float | None
    source_url: str | None


class SignalItem(BaseModel):
    signal_type: str
    source: str | None
    observed_at: datetime | None
    confidence: float | None
    payload: dict


class ListingDetailResponse(BaseModel):
    listing: ListingItem
    parcel: ParcelDetail | None
    signals: list[SignalItem]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _to_float(v: Decimal | float | int | None) -> float | None:
    if v is None:
        return None
    return float(v)


# Maps a raw signal_type (from listings.signal_type OR signals.signal_type) to a
# human distress DIMENSION label. Same-dimension records collapse (e.g. all tax
# flags → "Tax Distress"; the probate signal on a probate listing → "Probate").
_DIM_MAP: dict[str, str] = {
    "land_bank_inventory": "Land Bank",
    "tax_delinquent_foreclosure": "Tax Foreclosure",
    "forfeited_land": "Tax Deed (Forfeited Land)",
    "mortgage_foreclosure": "Foreclosure (Mortgage)",
    "probate": "Probate",
    "code_violation": "Code Violation",
    "code_violation_task": "Code Violation",
    "tax_foreclosure": "Tax Distress",
    "cert_pending": "Tax Distress",
    "cert_sold": "Tax Distress",
    "tax_payment_plan": "Tax Distress",
    "tax_delinquent": "Tax Distress",
}


def _dimension(raw: str | None) -> str | None:
    if not raw:
        return None
    return _DIM_MAP.get(raw, raw.replace("_", " ").title())


def _build_signal_types(
    listing_signal_type: str | None, type_counts: dict
) -> tuple[list[SignalTypeChip], int]:
    """Merge the listing's own source dimension with the per-parcel stacked
    signal types into distinct dimensions. Returns (chips, distinct_count)."""
    order: list[str] = []
    counts: dict[str, int] = {}

    base = _dimension(listing_signal_type)
    if base:
        counts[base] = 1
        order.append(base)

    for raw, cnt in (type_counts or {}).items():
        label = _dimension(raw)
        if not label or label in counts:
            # unknown, or same dimension already represented (no double-count)
            continue
        counts[label] = int(cnt)
        order.append(label)

    chips = [SignalTypeChip(label=lbl, count=counts[lbl]) for lbl in order]
    return chips, len(order)


def _row_to_item(r: asyncpg.Record) -> ListingItem:
    """Map a DB row (from the main listing query) to a ListingItem."""
    raw_counts = r["type_counts"]
    if isinstance(raw_counts, str):
        raw_counts = json.loads(raw_counts)
    chips, type_count = _build_signal_types(r["signal_type"], raw_counts or {})
    sig_count = int(r["signal_count"] or 0)

    return ListingItem(
        id=r["id"],
        source_site=r["source_site"],
        signal_type=r["signal_type"],
        property_address=r["property_address"],
        property_city=r["property_city"],
        property_county=r["property_county"],
        property_state=r["property_state"],
        property_zip=r["property_zip"],
        status=r["status"],
        pipeline_status=r["pipeline_status"],
        sale_date=r["sale_date"],
        sec_sale_date=r["sec_sale_date"],
        deposit_usd=_to_float(r["deposit_usd"]),
        opening_bid_usd=_to_float(r["opening_bid_usd"]),
        appraised_value_usd=_to_float(r["appraised_value_usd"]),
        auction_status=r["auction_status"],
        trustee_name=r["trustee_name"],
        case_number=r["case_number"],
        source_listing_id=r["source_listing_id"],
        case_status=r["case_status"],
        case_status_date=r["case_status_date"],
        match_method=r["match_method"],
        match_confidence=r["match_confidence"],
        match_score=_to_float(r["match_score"]),
        address_status=r["address_status"],
        first_seen_at=r["first_seen_at"],
        last_seen_at=r["last_seen_at"],
        signal_count=sig_count,
        signal_types=chips,
        signal_type_count=type_count,
        is_hot=type_count >= 2,
        owner_name=r["owner_name"],
        situs_address=r["situs_address"],
        current_market_value=_to_float(r["current_market_value"]),
        current_tax_balance=_to_float(r["current_tax_balance"]),
        delinquent_flag=bool(r["delinquent_flag"]) if r["delinquent_flag"] is not None else False,
        street_view_url=build_street_view_url(
            address=r["property_address"],
            city=r["property_city"],
            state=r["property_state"],
            zip_code=r["property_zip"],
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/listings
# ─────────────────────────────────────────────────────────────────────────────

_BASE_SELECT = """
    SELECT
        l.id,
        l.source_site,
        l.signal_type,
        l.property_address,
        l.property_city,
        l.property_county,
        l.property_state,
        l.property_zip,
        l.status,
        l.pipeline_status,
        l.sale_date,
        l.sec_sale_date,
        l.deposit_usd,
        l.opening_bid_usd,
        l.appraised_value_usd,
        l.auction_status,
        l.trustee_name,
        l.case_number,
        l.source_listing_id,
        l.case_status,
        l.case_status_date,
        l.match_method,
        l.match_confidence,
        l.match_score,
        l.address_status,
        l.first_seen_at,
        l.last_seen_at,
        COALESCE(sig.n, 0)              AS signal_count,
        sig.type_counts                 AS type_counts,
        p.owner_name,
        p.situs_address,
        p.current_market_value,
        p.current_tax_balance,
        p.delinquent_flag
    FROM tranchi.listings l
    LEFT JOIN (
        SELECT parcel_number,
               jsonb_object_agg(signal_type, cnt) AS type_counts,
               sum(cnt)::int                       AS n
        FROM (
            SELECT parcel_number, signal_type, count(*) AS cnt
            FROM tranchi.signals
            GROUP BY parcel_number, signal_type
        ) z
        GROUP BY parcel_number
    ) sig ON sig.parcel_number = l.source_listing_id
    LEFT JOIN tranchi.parcels p ON p.parcel_number = l.source_listing_id
"""


def _build_where(
    *,
    source_site: str | None,
    status: str | None,
    county: str | None,
    city: str | None,
    signal_type: str | None,
    has_signals: bool | None,
    min_signals: int | None,
    q: str | None,
    include_duplicates: bool = False,
    include_unverified: bool = False,
) -> tuple[list[str], list, int]:
    """Return (conditions, params, next_idx)."""
    conditions: list[str] = []
    params: list = []
    idx = 1

    if source_site:
        conditions.append(f"l.source_site = ${idx}")
        params.append(source_site)
        idx += 1

    if status:
        conditions.append(f"l.status = ${idx}")
        params.append(status)
        idx += 1

    if county:
        conditions.append(f"l.property_county ILIKE ${idx}")
        params.append(county)
        idx += 1

    if city:
        conditions.append(f"l.property_city ILIKE ${idx}")
        params.append(city)
        idx += 1

    if q:
        conditions.append(f"l.property_address ILIKE ${idx}")
        params.append(f"%{q}%")
        idx += 1

    # Signal filters reference the sig subquery alias; they must be applied in
    # the outer WHERE (after the subquery join), not inside the subquery.
    if has_signals is True:
        conditions.append("sig.n IS NOT NULL")
    elif has_signals is False:
        conditions.append("sig.n IS NULL")

    if min_signals is not None:
        conditions.append(f"COALESCE(sig.n, 0) >= ${idx}")
        params.append(min_signals)
        idx += 1

    if signal_type:
        # EXISTS check against signals table directly — don't rely on the
        # aggregated types array because array containment requires exact type.
        conditions.append(
            f"EXISTS (SELECT 1 FROM tranchi.signals s2 "
            f"WHERE s2.parcel_number = l.source_listing_id AND s2.signal_type = ${idx})"
        )
        params.append(signal_type)
        idx += 1

    # Always-on probate validity gate (Marc's #1 rule: open cases only). A probate
    # listing whose court case_status reads closed/disposed/terminated/dismissed is
    # no longer a live lead — the estate is settled and the property has transferred.
    # NULL case_status (not yet re-checked) stays visible until the backfill populates
    # it. Non-probate listings are unaffected.
    conditions.append(
        "NOT (l.signal_type = 'probate' AND l.case_status IS NOT NULL AND ("
        "l.case_status ILIKE '%closed%' OR l.case_status ILIKE '%disposed%' "
        "OR l.case_status ILIKE '%terminated%' OR l.case_status ILIKE '%dismissed%'))"
    )

    # Always-on dedup gate. Cross-source dedup (run.py) marks the non-canonical copy of
    # a property with duplicate_of -> canonical row but leaves it status='active'. The
    # READ layer is what hides it; without this a parcel/address appearing 2-5x would
    # show the same deal multiple times. Detail-by-id (GET /{id}) bypasses _build_where,
    # so a duplicate can still be opened directly. include_duplicates=true = debug escape.
    if not include_duplicates:
        conditions.append("l.duplicate_of IS NULL")

    # Always-on probate join-CONFIDENCE gate (precision-first; see Babel
    # reference/JOIN-PRECISION.md). A probate listing is shown only when its
    # decedent->parcel join is 'confirmed' or 'probable'. 'unverified' (weak name-only)
    # and NULL/legacy (un-tiered) joins are mis-join risks — hidden from the feed. This
    # is DATA-QUALITY gating, NOT deal pre-filtering: a mis-join is bad data, not a
    # narrow-but-valid deal, so it stays consistent with "pull everything valid, UI
    # filters". Rows are re-tiered by the precision matcher + scripts/reresolve_probate.py.
    # include_unverified=true = debug escape (still subject to the open-cases gate above).
    if not include_unverified:
        conditions.append(
            "(l.signal_type IS DISTINCT FROM 'probate' "
            "OR l.match_confidence IN ('confirmed', 'probable'))"
        )

    return conditions, params, idx


@router.get("", response_model=ListingPage)
@limiter.limit("60/minute")
async def list_listings(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    source_site: str | None = Query(default=None),
    status: str | None = Query(default=None),
    county: str | None = Query(default=None),
    city: str | None = Query(default=None),
    signal_type: str | None = Query(default=None),
    has_signals: bool | None = Query(default=None),
    min_signals: int | None = Query(default=None, ge=0),
    q: str | None = Query(default=None, description="Address ILIKE search"),
    include_duplicates: bool = Query(default=False, description="Debug: show cross-source duplicate rows"),
    include_unverified: bool = Query(default=False, description="Debug: show probate rows with unverified/untiered decedent→parcel joins"),
    sort: str = Query(default="first_seen_at"),
    order: str = Query(default="desc"),
    conn: asyncpg.Connection = Depends(get_db),
) -> ListingPage:
    # Sanitize sort / order
    if sort not in _VALID_SORT:
        sort = "first_seen_at"
    if order not in _VALID_ORDER:
        order = "desc"

    sort_col_map = {
        "first_seen_at": "l.first_seen_at",
        "signal_count":  "COALESCE(sig.n, 0)",
        "sale_date":     "l.sale_date",
        "address":       "l.property_address",
    }
    sort_expr = sort_col_map[sort]
    sort_dir = "ASC" if order == "asc" else "DESC"

    conditions, params, idx = _build_where(
        source_site=source_site,
        status=status,
        county=county,
        city=city,
        signal_type=signal_type,
        has_signals=has_signals,
        min_signals=min_signals,
        q=q,
        include_duplicates=include_duplicates,
        include_unverified=include_unverified,
    )

    where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    count_sql = f"""
        SELECT COUNT(*)
        FROM tranchi.listings l
        LEFT JOIN (
            SELECT parcel_number, count(*) AS n
            FROM tranchi.signals
            GROUP BY parcel_number
        ) sig ON sig.parcel_number = l.source_listing_id
        {where_sql}
    """
    total: int = await conn.fetchval(count_sql, *params)

    offset = (page - 1) * page_size
    data_sql = (
        _BASE_SELECT
        + f"""
        {where_sql}
        ORDER BY {sort_expr} {sort_dir} NULLS LAST
        LIMIT ${idx} OFFSET ${idx + 1}
        """
    )
    params_data = params + [page_size, offset]
    rows = await conn.fetch(data_sql, *params_data)

    items = [_row_to_item(r) for r in rows]
    total_pages = max(1, (total + page_size - 1) // page_size)

    return ListingPage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/listings/{id}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{listing_id}", response_model=ListingDetailResponse)
async def get_listing(
    listing_id: UUID,
    conn: asyncpg.Connection = Depends(get_db),
) -> ListingDetailResponse:
    """Full listing detail with parcel data and signal history."""
    # Main listing row (same joins as the list query for a single row)
    row = await conn.fetchrow(
        _BASE_SELECT + "WHERE l.id = $1",
        listing_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Listing not found",
        )

    listing_item = _row_to_item(row)

    # Parcel detail (may be null if source_listing_id is null or no parcel match)
    parcel: ParcelDetail | None = None
    if row["source_listing_id"]:
        p_row = await conn.fetchrow(
            """
            SELECT
                parcel_number, owner_name, situs_address, owner_mailing_address,
                current_market_value, taxable_value, current_tax_balance,
                delinquent_flag, year_built, sq_ft, beds, baths,
                last_sale_date, last_sale_price, source_url
            FROM tranchi.parcels
            WHERE parcel_number = $1
            """,
            row["source_listing_id"],
        )
        if p_row is not None:
            parcel = ParcelDetail(
                parcel_number=p_row["parcel_number"],
                owner_name=p_row["owner_name"],
                situs_address=p_row["situs_address"],
                owner_mailing_address=p_row["owner_mailing_address"],
                current_market_value=_to_float(p_row["current_market_value"]),
                taxable_value=_to_float(p_row["taxable_value"]),
                current_tax_balance=_to_float(p_row["current_tax_balance"]),
                delinquent_flag=bool(p_row["delinquent_flag"]) if p_row["delinquent_flag"] is not None else False,
                year_built=p_row["year_built"],
                sq_ft=p_row["sq_ft"],
                beds=p_row["beds"],
                baths=_to_float(p_row["baths"]),
                last_sale_date=p_row["last_sale_date"],
                last_sale_price=_to_float(p_row["last_sale_price"]),
                source_url=p_row["source_url"],
            )

    # Signals for this parcel
    signals: list[SignalItem] = []
    if row["source_listing_id"]:
        sig_rows = await conn.fetch(
            """
            SELECT signal_type, source, observed_at, confidence, payload
            FROM tranchi.signals
            WHERE parcel_number = $1
            ORDER BY observed_at DESC
            """,
            row["source_listing_id"],
        )
        for sr in sig_rows:
            payload_val = sr["payload"]
            if isinstance(payload_val, str):
                import json
                payload_val = json.loads(payload_val)
            signals.append(
                SignalItem(
                    signal_type=sr["signal_type"],
                    source=sr["source"],
                    observed_at=sr["observed_at"],
                    confidence=_to_float(sr["confidence"]),
                    payload=payload_val or {},
                )
            )

    return ListingDetailResponse(
        listing=listing_item,
        parcel=parcel,
        signals=signals,
    )
