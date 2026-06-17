"""
Wayne County (MI / Detroit) parcel registry spine scraper.

SPINE SOURCE: City of Detroit open-data parcel roll (Office of the Assessor), hosted ArcGIS.
  parcels:  https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest/services/parcel_file_current/FeatureServer/0
            378,366 parcels (2026-06-11); refreshed ~daily. maxRecordCount 2000.
  sales:    https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest/services/assessor_property_sales_view/FeatureServer/0
            509,708 recorded transfers (2026-06-11), ~3-day lag. Ordered sale_date DESC →
            first occurrence per parcel = the MOST RECENT sale. This is the AUTHORITATIVE
            last_sale_date for the transferred-guard (run.py:_mark_transferred_listings) and
            the off-market sold-check. Built into the spine (Jayden 2026-06-16) so the guard
            works at launch — Detroit's parcel roll also carries a baked sale_date, used as a
            fallback for parcels absent from the sales view (e.g. never-sold vacant land).

CANONICAL WAYNE PARCEL FORMATS (db.py:normalize_parcel_number 'wayne' branch) — TWO:
  Detroit ward:  '02000184.' (8 digits + SIGNIFICANT trailing period), '02000185-6' /
                 '02000185-600' (hyphen-range, ~33,815 rows), '03001910.001' / '21003982.002L'
                 (split-suffix + optional alpha). The trailing '.'/'-'/alpha are LOAD-BEARING
                 ('01000001.' != '010000023').
  Out-county:    '35024030846002' (14-digit packed) — NOT this layer (Detroit-only spine v1).

INVARIANT — pass parcel_id VERBATIM to normalize_parcel_number. NEVER pre-strip the trailing
  period: a bare 8-digit Detroit parcel ('02000184') mis-routes to the Cuyahoga branch and is
  reformatted to '020-00-184' (silent join failure + cross-market collision). The ArcGIS
  `parcel_id` field already carries the period; we only .strip() surrounding whitespace.

MI assessed value = 50% of true cash value → current_market_value = 2 * amt_assessed_value.

INVARIANT: populates tranchi.parcels ONLY — never writes tranchi.listings / tranchi.signals.
  run.py routes it via the registry path (fetch_parcels + upsert_parcels), same as
  fiscal_officer / shelby_parcels / summit_parcels. registry_in_full_run=False — 378K parcels
  (+509K sales) is too heavy for the 3h cron; runs on its own weekly cron via `--site wayne_parcels`.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from app.scrapers.arcgis_client import query_features
from app.scrapers.base import ListingScraper
from app.scrapers.db import normalize_parcel_number
from app.scrapers.models import RawListing

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_PARCELS_FS = (
    "https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest/services/"
    "parcel_file_current/FeatureServer/0"
)
_SALES_FS = (
    "https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest/services/"
    "assessor_property_sales_view/FeatureServer/0"
)

# Detroit Open Data layers (parcel_file_current AND assessor_property_sales_view) cap at
# maxRecordCount=1000. The shared arcgis_client stops paginating when a page returns FEWER
# than batch_size, so batch_size MUST be <= the server cap — a batch of 2000 gets 1000 back,
# looks like the last page, and silently truncates the sweep to 1 page (caught on first EC2
# run: spine loaded 1000 of 378K). Keep this at 1000.
_PAGE_SIZE = 1000

_PARCEL_FIELDS = ",".join([
    "parcel_id",
    "address",                     # situs street (no city — constant "Detroit")
    "zip_code",                    # situs zip
    "taxpayer_1", "taxpayer_2",    # taxpayer of record (owner)
    "amt_assessed_value",          # 50% of market → ×2 = market estimate
    "amt_taxable_value",
    "property_class_description",
    "use_code_description",
    "sale_date", "amt_sale_price",  # baked-in most-recent sale (FALLBACK for last_sale)
    "year_built",
    "ward",
])

_SALES_FIELDS = "parcel_id,sale_date,amt_sale_price"

_MULTISPACE = re.compile(r"\s+")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean(s: Any) -> str:
    if s is None:
        return ""
    return _MULTISPACE.sub(" ", str(s)).strip()


def _safe_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _parse_arcgis_date(v: Any) -> date | None:
    """Parse an ArcGIS date value to a python date.

    Detroit's sales view returns sale_date as an ISO STRING ('2026-06-08'); the parcel
    roll returns it the same way ('2013-12-30'). But hosted ArcGIS layers can also emit
    epoch-millis integers — so handle BOTH (never blindly divide by 1000 on a string).
    Returns None on blank / unparseable / sentinel values.
    """
    if v in (None, ""):
        return None
    # epoch millis (int/float, or numeric string)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return datetime.fromtimestamp(v / 1000, tz=timezone.utc).date()
        except (ValueError, OverflowError, OSError):
            return None
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit():  # epoch millis as a string
        try:
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc).date()
        except (ValueError, OverflowError, OSError):
            return None
    # ISO date / datetime string
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _build_owner(attrs: dict[str, Any]) -> str | None:
    o1 = _clean(attrs.get("taxpayer_1"))
    o2 = _clean(attrs.get("taxpayer_2"))
    if o1 and o2:
        return f"{o1} {o2}"
    return o1 or None


def _build_situs(attrs: dict[str, Any]) -> str | None:
    """'<street>, Detroit, MI <zip>' — the city is constant Detroit on this layer."""
    street = _clean(attrs.get("address"))
    if not street:
        return None
    street = street.title()
    zipc = _clean(attrs.get("zip_code"))
    tail = "Detroit, MI" + (f" {zipc}" if zipc else "")
    return f"{street}, {tail}"


def _attrs_to_parcel(attrs: dict[str, Any]) -> dict[str, Any] | None:
    """Map a Detroit parcel-roll attribute dict to the upsert_parcels hit shape.

    Returns None if the row lacks a usable parcel number (skip silently).
    """
    raw_id = _clean(attrs.get("parcel_id"))
    if not raw_id:
        return None
    # VERBATIM into the normalizer (keep the trailing '.'/'-'/alpha — significant).
    parcel_number = normalize_parcel_number(raw_id)
    if not parcel_number:
        return None

    assessed = _safe_float(attrs.get("amt_assessed_value"))
    market_value = (assessed * 2) if (assessed is not None and assessed > 0) else None

    return {
        "parcel_number": parcel_number,
        "native_parcel_id": None,
        "owner_name": _build_owner(attrs),
        "owner_mailing_address": None,
        "situs_address": _build_situs(attrs),
        "property_class": _clean(attrs.get("property_class_description")) or None,
        "land_use_code": _clean(attrs.get("use_code_description")) or None,
        "land_use_description": _clean(attrs.get("use_code_description")) or None,
        "neighborhood_code": None,
        "ward": _clean(attrs.get("ward")) or None,
        "property_zip": _clean(attrs.get("zip_code")) or None,
        "current_market_value": market_value,
        "source_url": f"{_PARCELS_FS}/query",
        "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
        "tax_balance_due": None,   # delinquency lives with the County Treasurer, not this roll
        "taxable_value": _safe_float(attrs.get("amt_taxable_value")),
        "year_built": attrs.get("year_built") or None,
        "school_district": None,
        # Baseline last-sale from the roll; overlaid by the fresher Property Sales sweep.
        "last_sale_date": _parse_arcgis_date(attrs.get("sale_date")),
        "last_sale_price": _safe_float(attrs.get("amt_sale_price")),
        "delinquent_flag": False,
        "tax_years_delinquent": None,
        "first_delinquent_year": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Spine scraper class
# ─────────────────────────────────────────────────────────────────────────────

class WayneParcelsScraper(ListingScraper):
    """Wayne County (MI / Detroit) parcel registry spine + Property-Sales overlay.

    Registry scraper — mirrors SummitParcelsScraper. run.py routes it through the
    registry path (fetch_parcels + upsert_parcels); fetch_and_parse() is ABC-only.

    Full sweep: 378K parcels (~190 pages) + 509K sales (~255 pages) = a few minutes.
    For a proof run pass max_parcels=N (the sales overlay is then scoped to those parcels).
    """

    site_name = "Detroit Open Data — Parcels (Current)"

    def __init__(self, max_parcels: int | None = None, page_size: int = _PAGE_SIZE,
                 with_sales: bool = True) -> None:
        self.max_parcels = max_parcels
        self.page_size = min(page_size, _PAGE_SIZE)
        self.with_sales = with_sales

    async def _fetch_sales_map(self, max_pages: int | None = None) -> dict[str, dict[str, Any]]:
        """Sweep Detroit Property Sales ordered sale_date DESC; keep the FIRST (most
        recent) sale per parcel. Returns {parcel_number: {last_sale_date, last_sale_price}}.

        Production: max_pages=None → full 509K sweep (~255 pages, the authoritative map).
        Proof run: max_pages caps the scan (recent sales only) so the overlay is bounded —
        do NOT early-exit on a target parcel set: the first parcels by ROLL order rarely
        appear early in the sales-DESC stream, so a parcel-targeted scan reads the whole
        509K layer (the original hang).
        """
        sales: dict[str, dict[str, Any]] = {}
        pages = 0
        async for batch in query_features(
            _SALES_FS,
            where="1=1",
            out_fields=_SALES_FIELDS,
            batch_size=self.page_size,
            extra_params={"orderByFields": "sale_date DESC"},
        ):
            pages += 1
            for attrs in batch:
                pn = normalize_parcel_number(_clean(attrs.get("parcel_id")))
                if not pn or pn in sales:
                    continue  # DESC order → first seen is the most recent sale
                sales[pn] = {
                    "last_sale_date": _parse_arcgis_date(attrs.get("sale_date")),
                    "last_sale_price": _safe_float(attrs.get("amt_sale_price")),
                }
            if max_pages is not None and pages >= max_pages:
                break
        logger.info("WayneParcels: sales overlay — %d parcels with a recorded sale (%d pages)",
                    len(sales), pages)
        return sales

    async def fetch_parcels(self) -> list[dict[str, Any]]:
        """Sweep the Detroit parcel roll, then overlay the fresher Property-Sales date."""
        all_parcels: list[dict[str, Any]] = []
        seen: set[str] = set()
        skipped_no_id = 0

        async for batch in query_features(
            _PARCELS_FS, where="1=1", out_fields=_PARCEL_FIELDS, batch_size=self.page_size,
        ):
            for attrs in batch:
                parcel = _attrs_to_parcel(attrs)
                if parcel is None:
                    skipped_no_id += 1
                    continue
                pn = parcel["parcel_number"]
                if pn in seen:
                    continue
                seen.add(pn)
                all_parcels.append(parcel)
            logger.info("WayneParcels: parcels so far=%d (skipped no-id=%d)",
                        len(all_parcels), skipped_no_id)
            if self.max_parcels is not None and len(all_parcels) >= self.max_parcels:
                all_parcels = all_parcels[: self.max_parcels]
                logger.info("WayneParcels: reached max_parcels cap (%d), stopping.", self.max_parcels)
                break

        if self.with_sales:
            # Proof runs (max_parcels set) cap the sales scan; full sweeps read all 509K.
            max_pages = 8 if self.max_parcels else None
            sales = await self._fetch_sales_map(max_pages=max_pages)
            overlaid = 0
            for p in all_parcels:
                s = sales.get(p["parcel_number"])
                if s and s.get("last_sale_date"):
                    # Property Sales is the authority; overlay when present (fresher than roll).
                    p["last_sale_date"] = s["last_sale_date"]
                    if s.get("last_sale_price") is not None:
                        p["last_sale_price"] = s["last_sale_price"]
                    overlaid += 1
            logger.info("WayneParcels: overlaid fresher sale data onto %d/%d parcels",
                        overlaid, len(all_parcels))

        logger.info("WayneParcels: sweep complete — %d unique parcels, %d skipped (no id).",
                    len(all_parcels), skipped_no_id)
        return all_parcels

    async def fetch_and_parse(self) -> list[RawListing]:
        """Disabled — registry source, routed via fetch_parcels() in run.py."""
        raise NotImplementedError(
            "WayneParcelsScraper is a registry source — use fetch_parcels() via the "
            "registry path in run.py, not fetch_and_parse()."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Standalone dry-run proof
# ─────────────────────────────────────────────────────────────────────────────

async def _dry_run_sample(n: int = 25) -> None:
    import json

    print(f"\n=== Wayne County (Detroit) Parcels — dry-run sample (first {n}) ===\n")
    scraper = WayneParcelsScraper(max_parcels=n, page_size=min(n, _PAGE_SIZE))
    parcels = await scraper.fetch_parcels()
    print(f"Fetched {len(parcels)} parcels\n")
    fmt_counts: dict[str, int] = {}
    for p in parcels:
        pn = p["parcel_number"]
        kind = ("period" if pn.endswith(".") or "." in pn else
                "hyphen" if "-" in pn else
                "14digit" if pn.isdigit() and len(pn) == 14 else "other")
        fmt_counts[kind] = fmt_counts.get(kind, 0) + 1
    print("parcel-format distribution:", fmt_counts)
    with_sale = sum(1 for p in parcels if p["last_sale_date"])
    print(f"parcels with last_sale_date: {with_sale}/{len(parcels)}\n")
    for p in parcels[:12]:
        print(f"  parcel={p['parcel_number']!r:16s} class={(p['property_class'] or '?')[:18]:18s} "
              f"mv={p['current_market_value']} sale={p['last_sale_date']} "
              f"owner={str(p['owner_name'])[:26]:26s} situs={p['situs_address']}")
    if parcels:
        print("\n--- Full dict for first parcel ---")
        print(json.dumps({k: str(v) for k, v in parcels[0].items() if v is not None}, indent=2))


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_dry_run_sample(25))
