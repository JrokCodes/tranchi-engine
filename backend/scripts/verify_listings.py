"""
Listing verifier — the repeatable "is this a real, valid, current lead?" check.

Marc's verification method is human cross-confluence: for a sample of listings,
confirm the property is real, the lead is still live, and (probate) the case is
open — using multiple independent sources so we know the data isn't fabricated.

This script does the parts that are RELIABLE and SCRIPTABLE, and emits the
Redfin/Zillow URLs for the one part that needs a human/browser eyeball (Redfin
and Zillow block scripted access via CloudFront — that check is the on-demand
/verify browser step, not an HTTP call).

Per-listing verdict combines:
  1. SOURCE AUTHORITY   — the listing came from an official government feed
                          (DLN county legal journal / probate court / fiscal office).
                          Deterministic scrape of public records → cannot be
                          "hallucinated"; this is the primary validity guarantee.
  2. FRESHNESS          — status='active'; auctions: sale_date >= today; probate:
                          case_status not closed/disposed/terminated/dismissed.
  3. PROPERTY IS REAL   — the parcel exists in tranchi.parcels (independent county
                          fiscal-office record) with an owner + market value. This
                          is a SECOND independent confirmation the address is a real
                          property, separate from the deal source.
  4. JOIN CONFIDENCE    — probate only: match_confidence tier (confirmed/probable/
                          unverified). Unverified = name-only fuzzy join → human check.
  5. NOT FOR SALE       — Redfin/Zillow URL emitted for the manual browser check
                          (off-market = consistent with distress; active MLS = flag).

Usage:
  python scripts/verify_listings.py --sample 15                # mixed random sample, console
  python scripts/verify_listings.py --stratified 3             # 3 per deal source = 12 + 3 fill = 15
  python scripts/verify_listings.py --stratified 3 --html out.html   # Marc-presentable HTML walk-through
  python scripts/verify_listings.py --signal probate --limit 10
"""
from __future__ import annotations

import argparse
import asyncio
import html as _html
import os
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

_here = Path(__file__).resolve().parent
_backend = _here.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_env_file = _backend / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)

import asyncpg  # noqa: E402

_CLOSED = ("closed", "disposed", "terminated", "dismissed")

# The four deal sources used for stratified sampling. Code violations are
# signals, not deal sources, so they don't participate.
DEAL_SOURCES = (
    "probate",
    "tax_delinquent_foreclosure",
    "mortgage_foreclosure",
    "land_bank_inventory",
)

_SELECT_COLS = """
    l.id, l.signal_type, l.source_site, l.property_address, l.property_city,
    l.property_zip, l.sale_date, l.opening_bid_usd, l.case_number,
    l.case_status, l.match_confidence, l.match_method, l.status,
    l.source_listing_id, l.address_status,
    (CURRENT_DATE - l.first_seen_at::date) AS lead_age_days,
    p.owner_name, p.current_market_value, p.current_tax_balance,
    p.land_use_code, p.last_sale_date, p.last_sale_price,
    CURRENT_DATE AS today
"""

_FROM_JOIN = """
    FROM tranchi.listings l
    LEFT JOIN tranchi.parcels p ON p.parcel_number = l.source_listing_id
"""


# ---------------------------------------------------------------- URL helpers


def _redfin_url(addr: str, city: str | None, zip_: str | None) -> str:
    q = ", ".join(p for p in (addr, city, "OH", zip_) if p)
    return "https://www.redfin.com/?q=" + urllib.parse.quote(q)


def _zillow_url(addr: str, city: str | None, zip_: str | None) -> str:
    q = " ".join(p for p in (addr, city, "OH", zip_) if p)
    return "https://www.zillow.com/homes/" + urllib.parse.quote(q) + "_rb/"


def _myplace_url(parcel: str | None) -> str:
    # Cuyahoga MyPlace: the search box accepts a parcel number. Base URL takes you
    # to the search; paste the parcel to land on the authoritative county record.
    if not parcel:
        return "https://myplace.cuyahogacounty.gov/"
    return f"https://myplace.cuyahogacounty.gov/?parcel={urllib.parse.quote(parcel)}"


# ---------------------------------------------------------- Console formatting


def _source_and_check(r: asyncpg.Record) -> tuple[str, str]:
    """Return (source_confirmation_url, what-to-check) for CONSOLE output.

    Commercial-aware: when land_use_code starts with '4' (commercial classes 4000-4999),
    Zillow doesn't cover the asset class — CHECK steers verification to MyPlace + Court.
    """
    sig = (r["signal_type"] or "")
    parcel = r["source_listing_id"]
    case = r["case_number"]
    addr_status = r["address_status"]
    land_use_code = (r["land_use_code"] or "")
    is_commercial = land_use_code.startswith("4") if land_use_code else False
    is_vacant_land = land_use_code.startswith("5") and addr_status == "no_street_number"

    if is_commercial:
        addr_hint = "SKIP Zillow (commercial, not on residential MLS); confirm via MyPlace + Court"
    elif is_vacant_land:
        addr_hint = "verify by PARCEL # (vacant land — county lists no street number)"
    elif addr_status == "no_street_number":
        addr_hint = "verify by PARCEL #, not address (unnumbered)"
    else:
        addr_hint = "address should match"

    if sig == "probate":
        src = f"https://probate.cuyahogacounty.gov/pa/   (search case {case or '?'})"
        chk = (f"(1) ProWare case {case or '?'} still says OPEN  "
               f"(2) MyPlace parcel {parcel} owner matches  "
               f"(3) Redfin/Zillow off-market = good  -- {addr_hint}")
    elif sig in ("tax_delinquent_foreclosure", "mortgage_foreclosure"):
        src = f"https://www.dln.com/   (search '{case or parcel or ''}' or by sale_date)"
        chk = (f"(1) DLN legal notice still lists sale_date={r['sale_date']}  "
               f"(2) MyPlace parcel {parcel} exists + delinquency present  "
               f"(3) Redfin/Zillow off-market = good  -- {addr_hint}")
    elif sig == "land_bank_inventory":
        src = "https://landbank.cuyahogalandbank.org/   (property inventory)"
        chk = (f"(1) Land Bank still lists this property  "
               f"(2) MyPlace parcel {parcel} confirms address  "
               f"(3) Redfin/Zillow off-market = expected (county-owned)  -- {addr_hint}")
    else:
        src = "https://myplace.cuyahogacounty.gov/"
        chk = f"(1) MyPlace parcel {parcel} exists  (2) Redfin/Zillow check  -- {addr_hint}"
    return src, chk


# ------------------------------------------------------------------ Verdict


def _verdict(r: asyncpg.Record) -> tuple[str, list[str]]:
    """Return (verdict, notes). VALID / REVIEW / STALE."""
    notes: list[str] = []
    status = r["status"]
    signal = r["signal_type"] or ""
    fresh = status == "active"
    if not fresh:
        return "STALE", [f"status={status}"]
    if r["sale_date"] is not None and r["sale_date"] < r["today"]:
        return "STALE", ["sale_date in past"]
    cs = (r["case_status"] or "").lower()
    if signal == "probate" and cs and any(w in cs for w in _CLOSED):
        return "STALE", [f"case {r['case_status']}"]

    # Post-filing transfer check (probate only).
    last_sale = r["last_sale_date"]
    if signal == "probate" and last_sale is not None and (r["case_number"] or "").strip():
        case_num = r["case_number"].strip()
        if len(case_num) >= 4 and case_num[:4].isdigit():
            filing_year = int(case_num[:4])
            if last_sale.year >= filing_year:
                price = r["last_sale_price"]
                price_str = f" for ${int(price):,}" if price else ""
                return "STALE", [f"TRANSFERRED — parcel sold {last_sale}{price_str} (case filed {filing_year})"]

    parcel_real = r["owner_name"] is not None
    if parcel_real:
        notes.append(f"parcel real (owner: {r['owner_name']}, mv=${int(r['current_market_value'] or 0):,})")
    else:
        notes.append("NO parcel registry match — confirm address")
    verdict = "VALID"
    if signal == "probate":
        tier = r["match_confidence"] or "legacy"
        notes.append(f"match={tier}")
        if tier == "unverified":
            verdict = "REVIEW"
            notes.append("name-only fuzzy join — verify owner==decedent")
    if signal == "probate" and r["lead_age_days"] is not None:
        notes.append(f"lead age: {int(r['lead_age_days'])}d")
    if not parcel_real and verdict == "VALID":
        verdict = "REVIEW"
    return verdict, notes


# --------------------------------------------------------- Structured layers


def _layers(r: asyncpg.Record) -> dict:
    """Build the 3-layer structured data for HTML rendering.

    Layer 1 = the deal-source page (probate court / DLN / Land Bank).
    Layer 2 = the county parcel registry (MyPlace) — independent second source.
    Layer 3 = the off-market human eyeball (Zillow + Redfin), with commercial /
              vacant-land overrides.
    """
    sig = r["signal_type"] or ""
    parcel = r["source_listing_id"] or "?"
    case = r["case_number"]
    addr_status = r["address_status"]
    land_use_code = (r["land_use_code"] or "")
    is_commercial = land_use_code.startswith("4") if land_use_code else False
    is_vacant_land = land_use_code.startswith("5") and addr_status == "no_street_number"

    first_seen_label = "—"
    if r["lead_age_days"] is not None:
        first_seen_label = f"{int(r['lead_age_days'])} days ago"

    if sig == "probate":
        # Parse the ProWare 3-part case key from our stored case_number string.
        # Format is <YEAR><CATEGORY><SUFFIX>, e.g. "2026EST306531" → year=2026,
        # category=EST, suffix=306531. ProWare's Case Search form takes these as
        # three separate fields; pasting the whole string into any single field
        # returns no result (or returns wrong results via the name-fuzzy box).
        filing_year = None
        case_category_code = None
        case_suffix = None
        if case:
            for i, ch in enumerate(case):
                if not ch.isdigit():
                    break
            else:
                i = len(case)
            year_part = case[:i]
            rest = case[i:]
            if year_part.isdigit() and len(year_part) == 4:
                filing_year = int(year_part)
            for j, ch in enumerate(rest):
                if ch.isdigit():
                    break
            else:
                j = len(rest)
            case_category_code = rest[:j] or None
            case_suffix = rest[j:] or None
        # Map ProWare category code → dropdown label
        category_label = {
            "EST": "ESTATE",
            "GDN": "GUARDIANSHIP",
            "TRU": "TRUST",
            "ML": "MARRIAGE LICENSE",
            "WIL": "WILL",
        }.get(case_category_code or "", case_category_code or "ESTATE")
        match_method = r["match_method"] or ""
        match_conf = r["match_confidence"] or "legacy"
        is_uncertain_join = (
            match_method == "" or match_conf in ("unverified", "legacy", None)
        )
        candidate_decedent_label = (
            f"{r['owner_name'] or '—'} (name-fuzzy match — verify on ProWare)"
            if is_uncertain_join
            else (r["owner_name"] or "—")
        )
        layer1 = {
            "title": "Cuyahoga Probate Court — ProWare",
            "url": "https://probate.cuyahogacounty.gov/pa/",
            "search_for": (
                "From the ProWare landing page, accept the disclaimer, then click "
                "'Case Search' (NOT 'Docket and Index Search' — that's a name-fuzzy "
                "box that returns wrong results). The 'Search by Case' form has "
                f"THREE separate fields. Enter Case Year = {filing_year or '?'}, "
                f"Case Category = {category_label}, Case Number = {case_suffix or '?'} "
                "(JUST the digits after the category code — NOT the full "
                f"'{case or '?'}' string)."
            ),
            "look_for": (
                "Case Status must say OPEN or PENDING (not Closed / Disposed / Terminated / Dismissed). "
                f"Filing year on the page should be {filing_year if filing_year else '(unknown — case# malformed)'}. "
                "Decedent name on the case page is the single source of truth — if the parcel owner below "
                "doesn't match the decedent name, this is a mis-joined row (REVIEW, not a real lead). "
                "Click 'Parties' on the case page to confirm the decedent."
            ),
            "stored": [
                ("Full Case Number", case or "—"),
                ("ProWare → Case Year", str(filing_year) if filing_year else "—"),
                ("ProWare → Case Category", category_label),
                ("ProWare → Case Number (just the suffix)", case_suffix or "—"),
                ("Case Status (our copy)", r["case_status"] or "—"),
                ("Candidate Decedent (parcel owner)", candidate_decedent_label),
                ("Match Method", match_method or "(legacy / pre-tiering)"),
                ("Match Confidence", match_conf),
                ("First Seen By Us", first_seen_label),
            ],
        }
    elif sig == "tax_delinquent_foreclosure":
        layer1 = {
            "title": "Daily Legal News — Delinquent Tax Auctions",
            "url": "https://www.dln.com/",
            "search_for": (
                f"Open the Delinquent Tax table on the home page. Filter by sale date "
                f"{r['sale_date']} or search for case '{case or parcel}'."
            ),
            "look_for": (
                "Row must still appear in the upcoming-auctions feed with matching parcel and sale date. "
                "By Ohio law (ORC 5721) tax-foreclosure parcels are delinquent ≥1 year — every row here "
                "already meets the aged-lien bar by construction."
            ),
            "stored": [
                ("Case Number", case or "—"),
                ("Sale Date", str(r["sale_date"] or "—")),
                ("Opening Bid", f"${int(r['opening_bid_usd']):,}" if r["opening_bid_usd"] else "—"),
                ("Parcel", parcel),
            ],
        }
    elif sig == "mortgage_foreclosure":
        layer1 = {
            "title": "Daily Legal News — Sheriff Sales (Mortgage Foreclosure)",
            "url": "https://www.dln.com/",
            "search_for": (
                f"Open the Sheriff Sales table on the home page. Filter by sale date "
                f"{r['sale_date']} or search for case '{case or parcel}'."
            ),
            "look_for": (
                "Row must still appear in the upcoming-sales feed with matching parcel and sale date. "
                "Owner facing forced sale by the lender — distressed timeline regardless of outcome."
            ),
            "stored": [
                ("Case Number", case or "—"),
                ("Sale Date", str(r["sale_date"] or "—")),
                ("Opening Bid", f"${int(r['opening_bid_usd']):,}" if r["opening_bid_usd"] else "—"),
                ("Parcel", parcel),
            ],
        }
    elif sig == "land_bank_inventory":
        layer1 = {
            "title": "Cuyahoga Land Bank — Available Properties",
            "url": "https://landbank.cuyahogalandbank.org/all-available-properties/",
            "search_for": (
                f"Scroll the inventory list or use Ctrl+F to find parcel {parcel} or "
                f"address '{r['property_address']}'."
            ),
            "look_for": (
                "Property must still appear on the available-properties list. Land Bank is a FULL_RESCAN "
                "source — if it drops off the live page, our scraper marks it not_listed next cycle "
                "(meaning sold or under contract)."
            ),
            "stored": [
                ("Parcel", parcel),
                ("Address", r["property_address"] or "—"),
            ],
        }
    else:
        layer1 = {
            "title": "Source not classified",
            "url": "https://myplace.cuyahogacounty.gov/",
            "search_for": f"Paste parcel {parcel} in the search box.",
            "look_for": "Confirm the parcel exists and address matches.",
            "stored": [("Signal Type", sig or "—")],
        }

    # Layer 2 — MyPlace (always the same shape, regardless of deal source)
    last_sale_label = "(not enriched yet)"
    if r["last_sale_date"]:
        if r["last_sale_price"]:
            last_sale_label = f"{r['last_sale_date']} for ${int(r['last_sale_price']):,}"
        else:
            last_sale_label = str(r["last_sale_date"])

    layer2 = {
        "title": "Cuyahoga MyPlace — County Parcel Registry",
        "url": _myplace_url(parcel),
        "search_for": (
            f"Direct link opens the parcel page for {parcel}. If the page loads but is blank, "
            "paste the parcel number in the search box at top-right."
        ),
        "look_for": (
            "(1) Owner name on the page should match our stored owner. "
            "(2) Situs address should match our stored address. "
            "(3) Click the 'Transfers' tab — the most recent transfer date is our last_sale_date. "
            "If a transfer date is at-or-after the case filing year, our _mark_transferred_listings "
            "guard removes the listing automatically; the absence of one is also a valid lead signal."
        ),
        "stored": [
            ("Parcel Number", parcel),
            ("Owner (county registry)", r["owner_name"] or "(not enriched yet)"),
            ("Situs Address", r["property_address"] or "—"),
            ("Market Value", f"${int(r['current_market_value']):,}" if r["current_market_value"] else "—"),
            ("Land-Use Code", land_use_code or "—"),
            ("Most Recent Transfer", last_sale_label),
        ],
    }

    # Layer 3 — off-market check
    if is_commercial:
        layer3 = {
            "title": "Off-Market Check — SKIP (Commercial Property)",
            "zillow_url": None,
            "redfin_url": None,
            "look_for": (
                f"Zillow and Redfin do not cover commercial properties. This parcel's land-use code "
                f"is {land_use_code} (commercial 4000-series), so a Zillow 'no result' is expected — "
                "not a stale signal. Verify instead via MyPlace and the court case (Layer 1 + 2 above)."
            ),
            "stored": [
                ("Land-Use Code", f"{land_use_code} (commercial 4000-series)"),
                ("Owner", r["owner_name"] or "—"),
            ],
        }
    elif is_vacant_land:
        layer3 = {
            "title": "Off-Market Check — Verify by Parcel (Vacant Land)",
            "zillow_url": _zillow_url(r["property_address"], r["property_city"], r["property_zip"]),
            "redfin_url": _redfin_url(r["property_address"], r["property_city"], r["property_zip"]),
            "look_for": (
                "This parcel is unnumbered vacant land (5000-series). Zillow/Redfin often return "
                f"no result — that's expected. Verify the parcel ({parcel}) on MyPlace; if it exists "
                "in the registry with a valid owner, it's a real land deal."
            ),
            "stored": [
                ("Land-Use Code", f"{land_use_code} (vacant 5000-series)"),
                ("Address Status", addr_status or "—"),
            ],
        }
    else:
        layer3 = {
            "title": "Off-Market Check — Zillow + Redfin (residential MLS)",
            "zillow_url": _zillow_url(r["property_address"], r["property_city"], r["property_zip"]),
            "redfin_url": _redfin_url(r["property_address"], r["property_city"], r["property_zip"]),
            "look_for": (
                "Off-market on both Zillow and Redfin = consistent with distress (good — owner not on "
                "the open market). Active for-sale on MLS = note (owner trying to sell before "
                "auction or probate resolution — still a valid lead, worth flagging in outreach). "
                "Recently sold POST-filing date = the post-transfer guard should have caught it; "
                "if you see this, escalate so we can confirm the enrichment ran."
            ),
            "stored": [
                ("Property Address", f"{r['property_address']}, {r['property_city']} {r['property_zip'] or ''}".strip()),
            ],
        }

    return {"layer1": layer1, "layer2": layer2, "layer3": layer3}


# ----------------------------------------------------------------- Fetching


async def _fetch_signal(conn, signal: str, limit: int) -> list:
    return await conn.fetch(
        f"""
        SELECT {_SELECT_COLS}
        {_FROM_JOIN}
        WHERE l.status = 'active' AND l.duplicate_of IS NULL
          AND l.signal_type = $1
        ORDER BY random()
        LIMIT {int(limit)}
        """,
        signal,
    )


async def _fetch_random(conn, limit: int, exclude_ids: set) -> list:
    rows = await conn.fetch(
        f"""
        SELECT {_SELECT_COLS}
        {_FROM_JOIN}
        WHERE l.status = 'active' AND l.duplicate_of IS NULL
        ORDER BY random()
        LIMIT {int(limit) * 4}
        """,
    )
    out = []
    for r in rows:
        if r["id"] in exclude_ids:
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


# -------------------------------------------------------------- HTML render


def _esc(s) -> str:
    if s is None:
        return ""
    return _html.escape(str(s))


def _stored_rows_html(items: list[tuple[str, str]]) -> str:
    return "\n".join(
        f'<div class="row"><span class="label">{_esc(lbl)}</span><br><span class="val">{_esc(val)}</span></div>'
        for lbl, val in items
    )


def _layer_html(layer: dict, title: str) -> str:
    return f"""
    <div class="layer">
      <div class="layer-title">{_esc(title)}</div>
      <div class="layer-grid">
        <div class="col-action">
          <strong>Open & verify:</strong>
          <a class="btn" href="{_esc(layer['url'])}" target="_blank" rel="noopener">{_esc(layer['title'])} ↗</a>
          <div class="search-for"><em>{_esc(layer['search_for'])}</em></div>
          <div class="look-for"><strong>What to look for:</strong> {_esc(layer['look_for'])}</div>
        </div>
        <div class="col-stored">
          <div class="stored-title">Our stored data (must match the page)</div>
          {_stored_rows_html(layer['stored'])}
        </div>
      </div>
    </div>
"""


def _offmarket_html(layer: dict) -> str:
    if layer["zillow_url"]:
        links = (
            f'<a class="btn" href="{_esc(layer["zillow_url"])}" target="_blank" rel="noopener">Open Zillow ↗</a>'
            f'<a class="btn btn-secondary" href="{_esc(layer["redfin_url"])}" target="_blank" rel="noopener">Open Redfin ↗</a>'
        )
    else:
        links = "<em>(Skipped — commercial property)</em>"
    return f"""
    <div class="layer">
      <div class="layer-title">Layer 3 — {_esc(layer['title'])}</div>
      <div class="layer-grid">
        <div class="col-action">
          {links}
          <div class="look-for"><strong>What to look for:</strong> {_esc(layer['look_for'])}</div>
        </div>
        <div class="col-stored">
          <div class="stored-title">Context</div>
          {_stored_rows_html(layer['stored'])}
        </div>
      </div>
    </div>
"""


def _card_html(i: int, r: asyncpg.Record, layers: dict, verdict: str, notes: list[str]) -> str:
    addr = f"{r['property_address']}, {r['property_city']}"
    sig_label = (r["signal_type"] or "").replace("_", " ")
    parcel = r["source_listing_id"] or "—"
    meta_parts = [f"parcel {parcel}"]
    if r["sale_date"]:
        meta_parts.append(f"sale {r['sale_date']}")
    if r["opening_bid_usd"]:
        meta_parts.append(f"opening bid ${int(r['opening_bid_usd']):,}")
    if r["signal_type"] == "probate" and r["lead_age_days"] is not None:
        meta_parts.append(f"lead age {int(r['lead_age_days'])}d")
    meta_str = " · ".join(meta_parts)
    notes_str = " · ".join(notes) if notes else ""

    return f"""
  <div class="card">
    <div class="card-header">
      <div class="card-header-text">
        <div class="card-title">[#{i}] {_esc(sig_label)} — {_esc(addr)}</div>
        <div class="card-meta">{_esc(meta_str)}</div>
        {f'<div class="card-notes">{_esc(notes_str)}</div>' if notes_str else ''}
      </div>
      <span class="verdict {verdict}">{verdict}</span>
    </div>
    {_layer_html(layers['layer1'], 'Layer 1 — Source Page (the deal pipeline)')}
    {_layer_html(layers['layer2'], 'Layer 2 — County Registry (MyPlace — independent second source)')}
    {_offmarket_html(layers['layer3'])}
  </div>
"""


def _render_html(rows: list, command: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    counts = {"VALID": 0, "REVIEW": 0, "STALE": 0}
    by_source: dict[str, dict[str, int]] = {}
    cards = []

    for i, r in enumerate(rows, 1):
        verdict, notes = _verdict(r)
        counts[verdict] += 1
        sig = r["signal_type"] or "unknown"
        bs = by_source.setdefault(sig, {"VALID": 0, "REVIEW": 0, "STALE": 0, "total": 0})
        bs[verdict] += 1
        bs["total"] += 1
        cards.append(_card_html(i, r, _layers(r), verdict, notes))

    by_source_str = " · ".join(
        f"{k.replace('_', ' ')}: {v['VALID']}/{v['total']} VALID"
        + (f" ({v['REVIEW']} REVIEW)" if v["REVIEW"] else "")
        + (f" ({v['STALE']} STALE)" if v["STALE"] else "")
        for k, v in sorted(by_source.items())
    )

    cards_html = "\n".join(cards)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tranchi — Verify Pass {ts}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      max-width: 1180px; margin: 2rem auto; padding: 0 1.25rem; color: #1c1c1c;
      background: #fafbfc;
    }}
    h1 {{ font-size: 1.55rem; margin: 0 0 0.3rem; letter-spacing: -0.01em; }}
    .subtitle {{ color: #666; margin-bottom: 1.25rem; font-size: 0.9rem; }}
    .summary {{
      background: #fff; padding: 1rem 1.15rem; border-radius: 8px; margin-bottom: 1.75rem;
      border-left: 4px solid #0066cc; border: 1px solid #d8dde2; border-left-width: 4px;
    }}
    .summary .big {{ font-size: 1.05rem; font-weight: 600; margin-bottom: 0.4rem; }}
    .summary .src-line {{ font-size: 0.85rem; color: #333; }}
    .summary .help {{ margin-top: 0.55rem; font-size: 0.85rem; color: #555; }}
    .card {{
      border: 1px solid #d8dde2; border-radius: 10px; margin-bottom: 1.75rem;
      overflow: hidden; background: #fff;
      box-shadow: 0 1px 2px rgba(16, 22, 26, 0.04);
    }}
    .card-header {{
      background: #fafbfc; padding: 0.85rem 1.1rem; border-bottom: 1px solid #d8dde2;
      display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem;
    }}
    .card-title {{ font-size: 0.98rem; font-weight: 600; }}
    .card-meta {{ font-size: 0.8rem; color: #666; margin-top: 0.2rem; }}
    .card-notes {{ font-size: 0.8rem; color: #444; margin-top: 0.2rem; font-style: italic; }}
    .verdict {{
      padding: 0.22rem 0.7rem; border-radius: 4px; font-size: 0.74rem;
      font-weight: 700; letter-spacing: 0.06em; flex-shrink: 0; white-space: nowrap;
    }}
    .verdict.VALID {{ background: #d4edda; color: #0e5223; }}
    .verdict.REVIEW {{ background: #fff3cd; color: #6c4d00; }}
    .verdict.STALE {{ background: #f8d7da; color: #6d1119; }}
    .layer {{ padding: 0.95rem 1.1rem; border-top: 1px solid #eef1f4; }}
    .layer-title {{
      font-weight: 700; font-size: 0.74rem; text-transform: uppercase;
      color: #555; letter-spacing: 0.07em; margin-bottom: 0.65rem;
    }}
    .layer-grid {{
      display: grid; grid-template-columns: 1.1fr 1fr; gap: 1.25rem; align-items: start;
    }}
    @media (max-width: 720px) {{ .layer-grid {{ grid-template-columns: 1fr; }} }}
    .col-action {{ font-size: 0.88rem; }}
    .col-stored {{
      font-size: 0.83rem; background: #f9fafb; padding: 0.65rem 0.8rem;
      border-radius: 6px; border: 1px solid #eef1f4;
    }}
    .stored-title {{
      font-size: 0.7rem; color: #555; text-transform: uppercase;
      letter-spacing: 0.06em; margin-bottom: 0.55rem; font-weight: 700;
    }}
    .col-stored .row {{ margin-bottom: 0.5rem; }}
    .col-stored .row:last-child {{ margin-bottom: 0; }}
    .col-stored .label {{ color: #666; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; }}
    .col-stored .val {{ font-weight: 500; color: #1a1a1a; }}
    .search-for {{ margin: 0.3rem 0 0.55rem; color: #555; font-size: 0.82rem; }}
    .look-for {{
      background: #f4f8fc; border-left: 3px solid #0066cc;
      padding: 0.5rem 0.7rem; margin-top: 0.5rem; font-size: 0.85rem; line-height: 1.5;
      border-radius: 0 4px 4px 0;
    }}
    a {{ color: #0066cc; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .btn {{
      display: inline-block; background: #0066cc; color: #fff !important;
      padding: 0.4rem 0.75rem; border-radius: 5px; font-weight: 600;
      font-size: 0.8rem; margin-right: 0.4rem; margin-bottom: 0.2rem;
    }}
    .btn:hover {{ background: #004fa3; text-decoration: none; }}
    .btn-secondary {{ background: #fff; color: #0066cc !important; border: 1px solid #0066cc; }}
    .btn-secondary:hover {{ background: #f4f8fc; }}
    code {{ background: #f0f2f5; padding: 0.1rem 0.35rem; border-radius: 3px; font-size: 0.85em; }}
    .footer {{
      margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid #d8dde2;
      color: #666; font-size: 0.82rem;
    }}
  </style>
</head>
<body>
  <h1>Tranchi — Verify Pass</h1>
  <div class="subtitle">Generated {ts} · <code>{_esc(command)}</code></div>

  <div class="summary">
    <div class="big">Result: {counts['VALID']} VALID · {counts['REVIEW']} REVIEW · {counts['STALE']} STALE (of {len(rows)})</div>
    <div class="src-line">By source — {by_source_str}</div>
    <div class="help">
      Each card shows three independent verification layers. <strong>Click the link</strong> in each layer to open the live source page,
      and compare what you see there against the <em>stored data</em> column on the right. Layer 1 = the deal pipeline; Layer 2 = the
      county registry (independent second source); Layer 3 = the off-market human eyeball.
    </div>
  </div>

  {cards_html}

  <div class="footer">
    Re-run: <code>{_esc(command)}</code><br>
    Full method writeup: <code>Clients/Marc/tranchi/VALIDATION-DIGEST.md</code>
  </div>
</body>
</html>
"""


# -------------------------------------------------------------------- Main


def _print_console(rows: list) -> None:
    print(f"\n=== VERIFICATION PASS — {len(rows)} listings ===\n")
    counts = {"VALID": 0, "REVIEW": 0, "STALE": 0}
    for i, r in enumerate(rows, 1):
        verdict, notes = _verdict(r)
        counts[verdict] += 1
        bid = f" bid=${int(r['opening_bid_usd']):,}" if r["opening_bid_usd"] else ""
        sd = f" sale={r['sale_date']}" if r["sale_date"] else ""
        src_url, check = _source_and_check(r)
        print(f"[{i:>2}] {verdict:<6} {r['signal_type']:<26} {r['property_address']}, {r['property_city']} ({r['source_listing_id']}){sd}{bid}")
        print(f"      {' | '.join(notes)}")
        print(f"      Redfin:  {_redfin_url(r['property_address'], r['property_city'], r['property_zip'])}")
        print(f"      Zillow:  {_zillow_url(r['property_address'], r['property_city'], r['property_zip'])}")
        print(f"      MyPlace: {_myplace_url(r['source_listing_id'])}")
        print(f"      Source:  {src_url}")
        print(f"      CHECK:   {check}")
    print("\n" + "=" * 70)
    print(f"  VALID={counts['VALID']}  REVIEW={counts['REVIEW']}  STALE={counts['STALE']}  (of {len(rows)})")
    print("  Manual step: open each Redfin/Zillow link — off-market = consistent with distress; active MLS = flag.")
    print("=" * 70 + "\n")


async def run(args) -> None:
    url = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(url)
    try:
        rows: list = []

        if args.stratified:
            for sig in DEAL_SOURCES:
                got = await _fetch_signal(conn, sig, args.stratified)
                rows.extend(got)
            target_total = args.sample or (args.stratified * len(DEAL_SOURCES) + 3)
            if len(rows) < target_total:
                fill = await _fetch_random(conn, target_total - len(rows), {r["id"] for r in rows})
                rows.extend(fill)
        elif args.signal:
            rows = await _fetch_signal(conn, args.signal, args.limit or args.sample or 15)
        else:
            n = args.limit or args.sample or 15
            rows = await _fetch_random(conn, n, set())

        if args.html:
            html = _render_html(rows, _command_repr(args))
            out_path = Path(args.html).expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(html, encoding="utf-8")
            counts = {"VALID": 0, "REVIEW": 0, "STALE": 0}
            for r in rows:
                v, _ = _verdict(r)
                counts[v] += 1
            print(f"\nHTML written: {out_path}")
            print(f"  Result: {counts['VALID']} VALID · {counts['REVIEW']} REVIEW · {counts['STALE']} STALE (of {len(rows)})")
            print(f"  Open in browser:  file://{out_path}\n")
        else:
            _print_console(rows)
    finally:
        await conn.close()


def _command_repr(args) -> str:
    parts = ["scripts/verify_listings.py"]
    if args.stratified:
        parts.append(f"--stratified {args.stratified}")
    if args.sample:
        parts.append(f"--sample {args.sample}")
    if args.signal:
        parts.append(f"--signal {args.signal}")
    if args.limit:
        parts.append(f"--limit {args.limit}")
    if args.html:
        parts.append(f"--html {args.html}")
    return " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Tranchi listing verifier")
    ap.add_argument("--sample", type=int, default=None, help="mixed random sample size (default 15 if no other selector)")
    ap.add_argument("--stratified", type=int, default=None,
                    help="N per deal source (probate / tax-deed / mortgage / land bank). Random-fills to --sample total.")
    ap.add_argument("--signal", type=str, default=None, help="filter to one signal_type")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--html", type=str, default=None,
                    help="write HTML walk-through to PATH and print short summary; also opens cleanly in a browser")
    args = ap.parse_args()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
