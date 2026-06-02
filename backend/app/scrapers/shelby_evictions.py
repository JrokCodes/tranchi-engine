"""
Shelby County (TN) eviction filings (Data Midsouth) — writes to tranchi.signals.

Shelby County General Sessions Court FED (Forcible Entry & Detainer) eviction filings,
aggregated by Innovate Memphis on the Data Midsouth OpenDataSoft portal (free, no auth,
no captcha). A recent eviction filing on a parcel is a "tired-landlord" distress SIGNAL
— it stacks into HOT alongside tax-sale / foreclosure / land-bank / probate listings;
it is NOT a standalone listing (an eviction alone isn't a deal).

Source (verified live 2026-06-02): 257,682 rows 2016→2026-05-22, actively maintained.
  Records:  https://www.datamidsouth.org/api/explore/v2.1/catalog/datasets/eviction-court-cases-shelby-county/records
  CSV bulk: .../eviction-court-cases-shelby-county/exports/csv
  Fields: property_address ("3910 Stuart Rd, Memphis, Tennessee, 38111"), zipcode,
          filing_date (freshness), first_named_plaintiff (landlord), living_units,
          case_id, lat/lon. NO parcel_id — resolve address→spine (house# + zip ILIKE).

INVARIANT (read before editing):
  - FRESHNESS-GATED: only filings within _WINDOW_DAYS are ingested, so HOT means a
    CURRENT eviction, not a decade-old one (the Cuyahoga N9 stale-signal lesson).
  - APARTMENT-COMPLEX FILTER: a 200-unit complex files routine monthly FEDs that are
    churn, not distress. Rows with living_units >= _MAX_UNITS are skipped so they don't
    false-flag a parcel HOT. The tired-landlord signal is for small/SFR rentals.
  - NO PARCEL in source → address-only. Resolve property_address + zipcode to the spine
    via shelby_foreclosure._resolve_parcels (house# + zip + street, UNIQUE match only;
    ambiguous/no-match → dropped, never invent a parcel). Unresolved evictions emit no
    signal (we can't HOT a parcel we can't identify).
  - AGGREGATE per resolved parcel: keep the LATEST filing_date as observed_at (so the
    natural key (parcel, signal_type, source, observed_at::date) updates in place and a
    parcel with many filings is ONE signal, naturally recency-bearing). payload carries
    the filing count in-window + last landlord.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

try:  # asyncpg only needed for the pool type hint
    import asyncpg  # noqa: F401
except Exception:  # pragma: no cover
    asyncpg = None  # type: ignore

from app.scrapers.base import SignalScraper
from app.scrapers.db import canonical_address, normalize_parcel_number
from app.scrapers.models import RawSignal
from app.scrapers.shelby_foreclosure import _join_key, _street_zip
from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

SITE_NAME = "Shelby Evictions"
SIGNAL_SOURCE = "shelby_general_sessions"

_DATASET = "eviction-court-cases-shelby-county"
_EXPORT_URL = (
    f"https://www.datamidsouth.org/api/explore/v2.1/catalog/datasets/{_DATASET}/exports/csv"
)
_TIMEOUT = 90.0
_WINDOW_DAYS = 365          # freshness gate
_MAX_UNITS = 5              # skip complexes with >= this many units (routine churn)


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    raw = raw.strip()[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _to_int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


async def _build_spine_index(pool: Any) -> dict[str, set[str]]:
    """Load the spine ONCE into {join_key: {parcel,...}}.

    Evictions has thousands of distinct addresses; resolving each via shelby_foreclosure
    ._resolve_parcels (ILIKE-ANY over the whole spine) is O(spine x patterns) and hangs.
    An in-memory index keyed exactly like _resolve_parcels (house# + zip + street) is
    O(spine + evictions). Ambiguous keys (>1 parcel) are kept and dropped at lookup.
    """
    spine: dict[str, set[str]] = {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT parcel_number, situs_address FROM tranchi.parcels "
            "WHERE situs_address IS NOT NULL AND situs_address <> ''"
        )
    for r in rows:
        s_street, s_zip = _street_zip(r["situs_address"])
        skey = _join_key(s_street, s_zip)
        if not skey:
            continue
        norm = normalize_parcel_number(r["parcel_number"])
        if norm:
            spine.setdefault(skey, set()).add(norm)
    return spine


class ShelbyEvictionsScraper(SignalScraper):
    """Shelby eviction filings as tired-landlord distress signals.

    Needs `pool` to resolve property addresses to spine parcels. Output flows through
    the signal path in run.py (fetch_signals -> _cv_upsert_signals).
    """

    site_name = SITE_NAME
    signal_source = SIGNAL_SOURCE

    def __init__(self, pool: "asyncpg.Pool | None" = None, dry_run: bool = False) -> None:
        self.pool = pool
        self.dry_run = dry_run

    async def fetch_signals(self) -> list[RawSignal]:
        cutoff = (datetime.now(tz=timezone.utc).date() - timedelta(days=_WINDOW_DAYS))
        params = {
            "select": "property_address,zipcode,filing_date,first_named_plaintiff,living_units",
            "where": f"filing_date >= date'{cutoff.isoformat()}'",
            "limit": "-1",
            "use_labels": "false",
        }
        headers = default_headers()
        try:
            async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
                resp = await client.get(_EXPORT_URL, params=params, timeout=_TIMEOUT)
                resp.raise_for_status()
                text = resp.text
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.error("ShelbyEvictions: CSV export failed: %s", exc)
            return []

        # ODS CSV export is ';'-delimited and carries a UTF-8 BOM — strip the BOM or the
        # first column name becomes '﻿property_address' and lookups silently return None.
        text = text.lstrip("﻿")
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        if reader.fieldnames and len(reader.fieldnames) == 1:  # fresh StringIO; ',' fallback
            reader = csv.DictReader(io.StringIO(text), delimiter=",")

        rows_seen = skipped_complex = skipped_nostreet = 0
        # jkey -> aggregate (latest filing, count, last landlord, a representative addr)
        agg: dict[str, dict[str, Any]] = {}
        for r in reader:
            rows_seen += 1
            units = _to_int(r.get("living_units"))
            if units is not None and units >= _MAX_UNITS:
                skipped_complex += 1
                continue
            prop_addr = (r.get("property_address") or "").strip()
            zip5 = (r.get("zipcode") or "").strip()[:5] or None
            fdate = _parse_date(r.get("filing_date"))
            street = canonical_address(prop_addr.split(",")[0].strip()) if prop_addr else None
            if not street:
                skipped_nostreet += 1
                continue
            jkey = _join_key(street, zip5)
            if not jkey:
                continue
            rec = agg.get(jkey)
            if rec is None:
                rec = {"street": street, "zip5": zip5, "latest": fdate, "count": 0,
                       "landlord": (r.get("first_named_plaintiff") or "").strip() or None}
                agg[jkey] = rec
            rec["count"] += 1
            if fdate and (rec["latest"] is None or fdate > rec["latest"]):
                rec["latest"] = fdate
                rec["landlord"] = (r.get("first_named_plaintiff") or "").strip() or rec["landlord"]

        # Resolve all distinct addresses against an in-memory spine index (unique-only).
        spine = await _build_spine_index(self.pool)

        signals: list[RawSignal] = []
        for jkey, rec in agg.items():
            cands = spine.get(jkey)
            parcel = next(iter(cands)) if cands and len(cands) == 1 else None
            if not parcel:
                continue  # unresolved/ambiguous — can't HOT a parcel we can't identify
            latest = rec["latest"] or cutoff
            observed_at = datetime(latest.year, latest.month, latest.day, tzinfo=timezone.utc)
            signals.append(RawSignal(
                parcel_number=parcel,            # already canonical (from the spine)
                signal_type="eviction",
                source=SIGNAL_SOURCE,
                observed_at=observed_at,
                confidence=1.0,
                payload={
                    "filings_in_window": rec["count"],
                    "latest_filing_date": latest.isoformat(),
                    "landlord": rec["landlord"],
                },
            ))

        logger.info(
            "ShelbyEvictions: %d signals (%d resolved parcels) from %d rows "
            "(skipped %d complexes >= %d units, %d no-street, %d distinct addrs, %d unresolved)",
            len(signals), len(signals), rows_seen, skipped_complex, _MAX_UNITS,
            skipped_nostreet, len(agg), len(agg) - len(signals),
        )
        return signals
