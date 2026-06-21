"""
Per-market configuration — the single home for everything market-specific.

WHY THIS MODULE EXISTS
----------------------
Tranchi runs many markets (Cuyahoga OH, Shelby/Memphis TN, soon metro-ring counties) from ONE
engine. Per-market values (source endpoints, parcel formats, registry authorities, verification
guidance, staleness policies, ...) used to be hardcoded across ~13 shared files, so adding a market
meant editing all of them and a single miss = a silent cross-market bug. This module is ITEM 1 of
`Clients/Marc/tranchi/markets/_FUTURE-ARCHITECTURE-HANDOFF.md`: a market reads ONLY its own config,
so a Cuyahoga URL can never appear in a TN run, and "add a market" becomes "add an entry here".

DISAMBIGUATOR = MARKET (county-level), NOT STATE.
Parcel numbers are assigned per-county, so two same-state counties can share a parcel-number string.
Always key on the `market` slug (`cuyahoga`, `shelby`, `fayette`, ...). A state holds many markets.
`tranchi.parcels` carries a `market` column (migration 013); cross-market joins are scoped
`AND p.market = l.market`.

CURRENT STATE (2026-06-07): ITEM-1 call-site fold COMPLETE. Per-market values now live here and the
shared modules read them: prefilter (`all_states`), run.py full-run skip (`full_run_skip_keys`),
parcel writers (`market_for_scraper`/`state_for_market`), staleness (`merged_staleness_policies`),
sources router (`merged_source_meta`), and the run.py post-passes (`probate_transfer_rule`,
`redemption_windows`). The market registry (`MARKETS` keys / `MARKET_SCRAPERS`) is what
audit/verify/enrich iterate.

DELIBERATE EXCEPTION — parcel canonicalization stays in `db.py::normalize_parcel_number`. It is a
correctness-critical, FORMAT-auto-detecting function (OH 'DDD-NN-NNN' vs TN 14-char cannot be
confused — the audited-clean basis), called from generic parse paths that don't know the market.
Format auto-detection is more cross-market-safe than per-market dispatch here, so it is NOT folded;
a new market's parcel format is added as a new detection branch there (the documented extension point).

TO ADD A MARKET: add a `_make_<market>_market()` + a `MARKETS` entry + a `MARKET_SCRAPERS` entry
here, write the scrapers, and (if a novel parcel format) add a branch to normalize_parcel_number.
Full runbook: `Clients/Marc/scraper-playbook/ADD-A-MARKET.md`.
"""
from __future__ import annotations

import urllib.parse

# Parcel normalizers live in db.py (the single home of canonical parcel form). market_config
# imports db (one-directional: db never imports market_config at load time), so each market
# can carry its `parcel_normalize_fn`. The 4 pre-Lucas markets use the global format-detector;
# Lucas dispatches to normalize_parcel_lucas because a bare 7-digit PARID is form-identical to
# a Summit parcel and cannot be disambiguated by string (F-008).
from app.scrapers.db import normalize_parcel_lucas, normalize_parcel_number


# GENERAL verification guidance shown once at the top of every HTML report.
# Applies to all markets — explains the off-market and vacant-land norms that
# confuse non-experts.
GENERAL_GUIDE = (
    "A real distressed or pre-auction property being OFF-MARKET on Zillow/Redfin is "
    "normal and actually good — it means the owner is NOT trying to sell through the "
    "open market, which is exactly the distress signal we want. A Zillow 'sold' banner "
    "is often years-old noise, NOT a kill — the county current-owner record (Layer 2) "
    "is the authority on who owns the property right now. "
    "Vacant lots (land use 000 or similar, address like '0 <street>') are verified by "
    "PARCEL NUMBER, not street number, and often won't appear on Zillow at all — "
    "that is expected and is not a red flag."
)


# ---------------------------------------------------------------- read gates
# Always-on probate READ gates applied by routers/listings.py (the single home for the
# gate vocabulary, out of the router). GENERAL today — both court systems use the same
# English status words and the same join-confidence tiers. If a market's court vocabulary
# ever diverges, graduate these to a per-market `read_gates` key + a market-scoped query.
#
# PROBATE_CLOSED_KEYWORDS: a probate listing whose case_status matches any of these
#   (ILIKE '%kw%') is a settled estate — hidden from the feed (Marc's #1 rule: open only).
# PROBATE_VISIBLE_CONFIDENCE: a probate listing is shown only when its decedent→parcel
#   join is in this set (precision-first; 'unverified'/NULL are mis-join risks, hidden).
PROBATE_CLOSED_KEYWORDS = ("closed", "disposed", "terminated", "dismissed")
PROBATE_VISIBLE_CONFIDENCE = ("confirmed", "probable", "review")


# ---------------------------------------------------------------- URL helpers


def _redfin_url(addr: str, city: str | None, state: str, zip_: str | None) -> str:
    q = ", ".join(p for p in (addr, city, state, zip_) if p)
    return "https://www.redfin.com/?q=" + urllib.parse.quote(q)


def _zillow_url(addr: str, city: str | None, state: str, zip_: str | None) -> str:
    q = " ".join(p for p in (addr, city, state, zip_) if p)
    return "https://www.zillow.com/homes/" + urllib.parse.quote(q) + "_rb/"


def _trustee_parcel_url(native_parcel_id: str | None) -> str | None:
    """Build one-click Shelby County Trustee parcel URL from the native spaced id.

    The Trustee URL requires the county's native SPACED form (e.g. '042035  00007'),
    URL-encoded as '042035%20%2000007'. Our canonical 14-char form is NOT accepted.
    Returns None when native_parcel_id is absent (stub parcels / not yet in ReGIS).
    """
    if not native_parcel_id:
        return None
    return (
        "https://apps2.shelbycountytrustee.com/Parcel?parcel="
        + urllib.parse.quote(native_parcel_id, safe="")
    )


# ---------------------------------------------------------------- market configs


def _make_cuyahoga_market() -> dict:
    """Cuyahoga (Cleveland, OH) market config."""

    def registry_link(parcel: str | None, address: str | None,
                      native_parcel_id: str | None = None) -> str:
        if not parcel:
            return "https://myplace.cuyahogacounty.gov/"
        return f"https://myplace.cuyahogacounty.gov/?parcel={urllib.parse.quote(parcel)}"

    def source_link(signal_type: str, parcel: str | None, case: str | None,
                    sale_date, address: str | None,
                    native_parcel_id: str | None = None) -> str:
        sig = signal_type or ""
        if sig == "probate":
            return f"https://probate.cuyahogacounty.gov/pa/   (search case {case or '?'})"
        elif sig in ("tax_delinquent_foreclosure", "mortgage_foreclosure"):
            return f"https://www.dln.com/   (search '{case or parcel or ''}' or by sale_date)"
        elif sig == "forfeited_land":
            return "https://cuyahogacounty.gov/fiscal-officer/departments/real-property/forfeited-lands   (Forfeited Lands Locator)"
        elif sig == "land_bank_inventory":
            return "https://landbank.cuyahogalandbank.org/   (property inventory)"
        else:
            return "https://myplace.cuyahogacounty.gov/"

    def offmarket_links(address: str, city: str | None, zip_: str | None) -> tuple[str, str]:
        return _zillow_url(address, city, "OH", zip_), _redfin_url(address, city, "OH", zip_)

    verification_guide = {
        "probate": {
            "valid": (
                "Case Status on ProWare says OPEN or PENDING. Filing year matches the case number. "
                "Decedent name on the case page matches (or reasonably maps to) the parcel owner — "
                "if they match, this is a real estate in probate where heirs may want a fast sale."
            ),
            "red_flags": (
                "Case Status says Closed / Disposed / Terminated / Dismissed (the estate resolved — "
                "lead is dead). Parcel owner name doesn't match the decedent at all (mis-join). "
                "A transfer date on MyPlace at-or-after the case filing year means the property "
                "already sold out of the estate."
            ),
        },
        "tax_delinquent_foreclosure": {
            "valid": (
                "Row still appears on DLN's upcoming-auctions feed with matching parcel and sale date. "
                "By Ohio law (ORC 5721) every row here is already delinquent ≥1 year — no extra check needed. "
                "MyPlace confirms the parcel exists and the owner hasn't paid off."
            ),
            "red_flags": (
                "Row has dropped off the DLN feed (auction cancelled / owner paid up). "
                "Sale date is in the past (STALE — the auction already ran). "
                "MyPlace shows a recent transfer to a new private owner (paid off / sold)."
            ),
        },
        "mortgage_foreclosure": {
            "valid": (
                "Row still appears on DLN's Sheriff Sales feed with matching parcel and sale date. "
                "Owner is facing forced sale by the lender — a distressed timeline. "
                "MyPlace confirms the existing owner name and no recent transfer."
            ),
            "red_flags": (
                "Row has dropped off DLN (sale cancelled / settled). Sale date is past. "
                "MyPlace shows a new owner (property already changed hands)."
            ),
        },
        "forfeited_land": {
            "valid": (
                "Parcel still appears on the Cuyahoga Forfeited Lands Locator (ArcGIS map). "
                "MyPlace owner says 'STATE OF OHIO FORF' — the county holds the deed. "
                "County market value > opening bid (positive equity = real arbitrage opportunity)."
            ),
            "red_flags": (
                "Parcel dropped off the locator (sold at a subsequent auction). "
                "MyPlace owner is a private party (title already transferred). "
                "Opening bid ≥ market value (no equity — skip)."
            ),
        },
        "land_bank_inventory": {
            "valid": (
                "Property still listed on the Cuyahoga Land Bank available-properties page. "
                "MyPlace confirms the parcel address. These are county-owned, priced for community redevelopment — "
                "expect deep discount but also deed restrictions."
            ),
            "red_flags": (
                "Property no longer on the Land Bank page (sold or under contract — "
                "our scraper marks it not_listed next cycle). "
                "MyPlace shows a private owner (already sold)."
            ),
        },
        # Pre-Distress LEADS (surface_distress.py). tax_delinquent is live; code_violation is
        # gated on the status-refresh cron (see distress_lead_rules).
        "tax_delinquent": {
            "valid": (
                "MyPlace shows a delinquent tax balance owed (>= $2k or flagged for foreclosure) with a "
                "private-party owner. PRE-DISTRESS LEAD — the owner is under pressure but the property is "
                "NOT yet for sale through any channel. Off-market on Zillow/Redfin is consistent with a "
                "distressed owner who hasn't listed (good)."
            ),
            "red_flags": (
                "Balance paid off / $0 due (resolved — not distress). Already on the DLN sheriff-sale feed "
                "(that's a buy-now deal, not a lead). A recent transfer to a new private owner on MyPlace "
                "(already sold). Actively listed on MLS (owner selling normally — note in outreach)."
            ),
        },
        "code_violation": {
            "valid": (
                "Cleveland Accela shows an OPEN building/housing violation cited within ~24 months with a "
                "private-party owner. PRE-DISTRESS LEAD — chronic-violation owner under pressure, not yet "
                "selling. Off-market on Zillow/Redfin is consistent (good)."
            ),
            "red_flags": (
                "Violation status is Closed / resolved (not distress). Cited > 24 months ago (stale). "
                "Recent transfer to a new owner (already sold). Actively listed on MLS."
            ),
        },
    }

    return {
        "db": "tranchi",
        "state": "OH",
        "parcel_normalize_fn": normalize_parcel_number,  # global format-detector (unchanged)
        "market_filter": "l.market = 'cuyahoga'",  # sample scope: by market, NOT state (two OH markets coexist)
        # Pre-distress lead surfacing (surface_distress.py). `distress_lead_county` is the
        # county label stamped on lead rows. `distress_lead_rules` (per signal_type):
        #   address_source 'payload' — Cuyahoga signal payloads carry the FULL situs
        #     ("STREET, CITY, OH, ZIP"); the parcel spine is too thin (~23% situs) to rely on.
        #   gate_sql — the defensible-slice filter (REAL pre-distress only): tax = balance
        #     >= $2k OR foreclosure-flagged; code_violation = OPEN and cited within 24 months
        #     (most violations are Closed/resolved = not distress). Regex guards on the cast
        #     columns keep a malformed payload value from aborting the whole INSERT.
        # Surfaced only once tranchi.distress_lead_types has the matching enabled rows (mig 016).
        "distress_lead_county": "Cuyahoga",
        "distress_lead_rules": {
            "tax_delinquent": {
                "address_source": "payload",
                "address_key": "address",
                "owner_key": "owner",
                "gate_sql": (
                    "((s.payload->>'delq_balance' ~ '^[0-9.]+$' "
                    "  AND (s.payload->>'delq_balance')::numeric >= 2000) "
                    " OR s.payload->>'foreclosure' = 'true')"
                ),
            },
            # GATED ON THE STATUS-REFRESH CRON. cleveland_open_data is a DELTA pull (only new
            # filings each 3h), so existing signals' Open/Closed status + last_seen_at freeze at
            # first scrape. The `run.py --site code_violations --full` cron (every ~3 days) re-pulls
            # the full Notices layer — the source DOES carry live VIOLATION_APP_STATUS — refreshing
            # status (resolved -> Closed, dropped by this gate) + freshness so the standard 4-day
            # gate applies. The distress_lead_types row starts disabled until a refresh validates
            # the live Open+<=24mo slice; then flip enabled=true.
            "code_violation": {
                "address_source": "payload",
                "address_key": "address_full",
                "owner_key": None,
                "gate_sql": (
                    "(s.payload->>'status' ILIKE 'open' "
                    " AND s.payload->>'open_date' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' "
                    " AND (s.payload->>'open_date')::date >= now() - interval '24 months')"
                ),
            },
        },
        "deal_sources": (
            "probate",
            "tax_delinquent_foreclosure",
            "forfeited_land",
            "mortgage_foreclosure",
            "land_bank_inventory",
        ),
        "source_sites": {
            "Cuyahoga Probate Court": "probate",
            "Cuyahoga Forfeited Land": "forfeited_land",
            "Cuyahoga Sheriff Sale (DLN)": "tax_delinquent_foreclosure",
            "Cuyahoga Sheriff Sales": "tax_delinquent_foreclosure",
            "Cuyahoga Land Bank": "land_bank_inventory",
        },
        # Per-source retirement policy (see staleness.py invariant). Keyed by the
        # listing source_site as stored in tranchi.listings. staleness.SOURCE_STALENESS
        # is built by merging every market's slice.
        "staleness_policies": {
            "Cuyahoga Sheriff Sale (DLN)": "full_rescan",
            "Cuyahoga Land Bank": "full_rescan",
            "Cuyahoga Forfeited Land": "full_rescan",
            "Cuyahoga Probate Court": "cursor",
            "Cuyahoga Sheriff Sales": "archive",
        },
        # Per-source public-site + role metadata for /api/v1/sources. (url, category)
        # where category ∈ {deal, signal, registry, lead}. routers/sources._SOURCE_META
        # is built by merging every market's slice.
        "source_meta": {
            "Cuyahoga Land Bank": ("https://cuyahogalandbank.org/all-available-properties/", "deal"),
            "Cuyahoga Sheriff Sales": ("https://cpdocket.cp.cuyahogacounty.gov/sheriffsearch/search.aspx", "deal"),
            "Cuyahoga Sheriff Sale (DLN)": ("https://www.dln.com/", "deal"),
            "Cuyahoga Probate Court": ("https://probate.cuyahogacounty.gov/pa/", "deal"),
            "Cuyahoga Forfeited Land": ("https://cuyahogacounty.gov/fiscal-officer/departments/real-property/forfeited-lands", "deal"),
            "Cleveland Code Violations": ("https://data.clevelandohio.gov/", "signal"),
            "Cuyahoga Delinquent Tax": ("https://cuyahogacounty.gov/treasury/delinquency", "signal"),
            "Cuyahoga Fiscal Officer": ("https://myplace.cuyahogacounty.gov", "registry"),
            # Pre-distress LEADS (surface_distress.py) — category 'lead' groups them under the
            # Pre-Distress view, not the buy-now deal feed.
            "Cuyahoga Tax Delinquent (Lead)": ("https://myplace.cuyahogacounty.gov/", "lead"),
            "Cuyahoga Code Violation (Lead)": ("https://data.clevelandohio.gov/", "lead"),
        },
        # Probate "sold-after-filing" auto-transfer rule (run.py::_mark_transferred_listings).
        # Cuyahoga case numbers encode the filing year as the first 4 chars ('2026EST...').
        # `case_regex` identifies a probate case of this market's format; `filing_year_substr`
        # = (start, length) for SQL substring() to pull the year. None => market has no such
        # rule (e.g. Shelby's PR##### numbers carry no year — that gap is a tracked follow-up).
        "probate_transfer_rule": {
            "case_regex": r"^[0-9]{4}EST",
            "filing_year_substr": (1, 4),
        },
        # OH tax deeds are FINAL at sale (no statutory owner redemption) — no window pass.
        "redemption_windows": None,
        # Live cross-verify endpoints (scripts/playwright_source_check.py). The single home
        # for this market's verifier URLs so a new market declares them here, not inline in
        # the shared verifier. The verifier FUNCTIONS + per-signal dispatch stay in that
        # script (per-county scraping is irreducible code), but they read URLs from here.
        "verifier_endpoints": {
            "dln_api": "https://www.dln.com/wp-json/dln/v1/data-table",
            "landbank_url": "https://cuyahogalandbank.org/all-available-properties/",
            "myplace_base": "https://myplace.cuyahogacounty.gov",
        },
        "registry_link": registry_link,
        "registry_label": "Cuyahoga MyPlace — County Parcel Registry",
        "registry_search_hint": (
            "Direct link opens the parcel page for {parcel}. If the page loads but is blank, "
            "paste the parcel number in the search box at top-right."
        ),
        "registry_look_for": (
            "(1) Owner name on the page should match our stored owner. "
            "(2) Situs address should match our stored address. "
            "(3) Click the 'Transfers' tab — the most recent transfer date is our last_sale_date. "
            "If a transfer date is at-or-after the case filing year, our _mark_transferred_listings "
            "guard removes the listing automatically; the absence of one is also a valid lead signal."
        ),
        "source_link": source_link,
        "offmarket_links": offmarket_links,
        "verification_guide": verification_guide,
    }


def _make_shelby_market() -> dict:
    """Shelby County (Memphis, TN) market config.

    UNIVERSAL VERIFIER: The Shelby County Trustee parcel page is the single source
    of truth for ALL Shelby categories. One page shows: PRIMARY OWNER, LOCATION,
    MAILING ADDRESS, TAX DUE / TOTAL BALANCE, multi-year BILLING HISTORY, and —
    when scheduled — a 'TAX SALE PROPERTY NOTICE / Tax Sale ID: TS####' block.
    It also has external links to REGISTER GIS and ASSESSOR GIS.

    URL form: https://apps2.shelbycountytrustee.com/Parcel?parcel=<native_id_url_encoded>
    where native_id is the spaced PARCELID from ReGIS (e.g. '042035  00007').
    Our canonical 14-char form does NOT work — only the native spaced form does.
    """

    def registry_link(parcel: str | None, address: str | None,
                      native_parcel_id: str | None = None) -> str:
        # Primary: Trustee parcel page (one-click, shows owner + tax due + tax sale notice).
        url = _trustee_parcel_url(native_parcel_id)
        if url:
            return url
        # Fallback: Trustee PropertyClient search (SPA — human must type the address).
        return "https://apps2.shelbycountytrustee.com/PropertyClient"

    def source_link(signal_type: str, parcel: str | None, case: str | None,
                    sale_date, address: str | None,
                    native_parcel_id: str | None = None) -> str:
        sig = signal_type or ""
        if sig == "tax_deed":
            # Layer 1 for tax_deed = same Trustee page, but caller looks for the
            # 'TAX SALE PROPERTY NOTICE / Tax Sale ID: TS####' block as the proof.
            url = _trustee_parcel_url(native_parcel_id)
            if url:
                return url
            # Fallback: Trustee search SPA (human types address).
            return "https://apps2.shelbycountytrustee.com/PropertyClient"
        elif sig == "land_bank_inventory":
            return (
                "https://public-sctn.epropertyplus.com/landmgmtpub/app/base/landing"
                f"   (Shelby County Land Bank — search parcel {parcel or '?'})"
            )
        else:
            url = _trustee_parcel_url(native_parcel_id)
            if url:
                return url
            return "https://apps2.shelbycountytrustee.com/PropertyClient"

    def offmarket_links(address: str, city: str | None, zip_: str | None) -> tuple[str, str]:
        return _zillow_url(address, city, "TN", zip_), _redfin_url(address, city, "TN", zip_)

    verification_guide = {
        "tax_deed": {
            "valid": (
                "On the Trustee page: PRIMARY OWNER should be a PRIVATE party (person or LLC) who is "
                "behind on taxes. TAX DUE should be > $0. You should see a 'TAX SALE PROPERTY NOTICE' "
                "block with a Tax Sale ID (e.g. TS2302) — this is the proof the parcel is scheduled. "
                "TN NOTE: These are PRE-sale listings. After the sale, TN gives the original owner ~1 "
                "year to redeem (pay back taxes + costs). Phase-1 is the highest-leverage window: "
                "approach BEFORE the sale while the owner is still motivated."
            ),
            "red_flags": (
                "No tax balance due / no 'Tax Sale Property Notice' on the Trustee page = not scheduled "
                "or already redeemed (skip). Owner already shows as the land bank or county "
                "('SHELBY COUNTY TAX SALE' or similar) = county already took it; treat as land-bank "
                "deal instead. Parcel was recently transferred to a new private owner = paid off or "
                "sold, back-tax pressure gone."
            ),
        },
        "land_bank_inventory": {
            "valid": (
                "On the Trustee page: PRIMARY OWNER should read 'SHELBY COUNTY TAX SALE...' — confirms "
                "the county acquired it via tax sale. Property should appear in the ePropertyPlus portal "
                "(https://public-sctn.epropertyplus.com/landmgmtpub/app/base/landing) with status FOR SALE. "
                "Trustee page also shows the billing history confirming the tax-sale acquisition."
            ),
            "red_flags": (
                "Trustee page shows a PRIVATE owner (county no longer holds it — already sold; our data "
                "is stale). Status in the ePropertyPlus portal is not FOR SALE (under contract or withdrawn). "
                "Property not found in the portal at all."
            ),
        },
        "mmlba": {
            "valid": (
                "On the Trustee page: PRIMARY OWNER should read 'MEMPHIS METROPOLITAN LAND BANK AUTHORITY' "
                "(or close variant). Property should appear on the MMLBA property-sales page "
                "(https://mmlba.org/property-sales/) — search by address in the gallery. "
                "This confirms the city holds the title, acquired for blight removal."
            ),
            "red_flags": (
                "Trustee page shows a private owner (already sold to a private buyer — our data is stale). "
                "Property is not on the MMLBA available list (sold, withdrawn, or never listed). "
                "Trustee shows a recent transfer date after our scrape date."
            ),
        },
        # Pre-Distress LEADS (distress_stage='distress_signal') — NOT for-sale listings;
        # off-market motivated-owner leads. Validity = owner still in distress + not sold + off-market.
        "tax_delinquent": {
            "valid": (
                "This is a PRE-DISTRESS LEAD: the owner is being sued for unpaid taxes (open lien, "
                "well past 12 months). On the Trustee page: PRIMARY OWNER should be a PRIVATE party "
                "(person/LLC), and TAX DUE / TOTAL BALANCE should be > $0 (often a multi-year balance). "
                "Off-market on Zillow/Redfin = good (the owner hasn't listed — exactly the off-market lead). "
                "A 'TAX SALE PROPERTY NOTICE' block appearing means the delinquency has escalated to a "
                "scheduled auction (the lead is graduating to a tax_deed deal)."
            ),
            "red_flags": (
                "TAX DUE = $0 on the Trustee page = owner paid off; lead is dead. Owner already shows as "
                "the county / land bank = the county took it (it's a land-bank deal now, not a lead). "
                "Parcel recently transferred to a new private owner = sold; back-tax pressure gone. "
                "Actively listed for sale on Zillow/Redfin = not really off-market (note it)."
            ),
        },
        "eviction": {
            "valid": (
                "This is a PRE-DISTRESS LEAD: a tired landlord clearing a problem tenant (motivated seller). "
                "On the Trustee page: confirm the parcel exists and the PRIMARY OWNER still matches our stored "
                "owner (the distressed landlord), i.e. NOT recently sold. Off-market on Zillow/Redfin = good. "
                "Eviction is weaker off-market than tax-distress (landlords sometimes list after clearing a "
                "tenant), so the Zillow/Redfin off-market check matters more here."
            ),
            "red_flags": (
                "Parcel recently transferred to a new owner = already sold (stale lead). Actively listed for "
                "sale on Zillow/Redfin = the landlord is already selling on-market (weaker lead — note it)."
            ),
        },
    }

    return {
        "db": "tranchi",
        "state": "TN",
        "parcel_normalize_fn": normalize_parcel_number,  # global format-detector (unchanged)
        "market_filter": "l.market = 'shelby'",
        # Pre-distress lead surfacing (surface_distress.py). Shelby sources the lead address
        # from the parcel SPINE (situs_address; the signal payload location is street-only)
        # and surfaces EVERY fresh signal (gate_sql=None) — the byte-for-byte prior behavior,
        # preserved by the market-aware refactor. (Cuyahoga, by contrast, reads the payload
        # and applies a defensible-slice gate — see that market's distress_lead_rules.)
        "distress_lead_county": "Shelby",
        "distress_lead_rules": {
            "tax_delinquent": {"address_source": "spine", "address_key": None,
                               "owner_key": "owner", "gate_sql": None},
            "eviction": {"address_source": "spine", "address_key": None,
                         "owner_key": "owner", "gate_sql": None},
        },
        "deal_sources": (
            "tax_deed",
            "land_bank_inventory",
            "tax_delinquent",   # Pre-Distress LEAD (Trustee delinquent-lawsuit list)
            "eviction",         # Pre-Distress LEAD (Data Midsouth tired-landlord)
        ),
        "source_sites": {
            "Shelby County Tax Sale": "tax_deed",
            "Shelby County Land Bank": "land_bank_inventory",
            "Memphis MMLBA": "land_bank_inventory",
            "Shelby Tax Delinquent (Lead)": "tax_delinquent",
            "Shelby Eviction (Lead)": "eviction",
        },
        # Per-source retirement policy (see staleness.py invariant). Probate is a CURSOR
        # forward-walk (retired only by case_status re-check), the rest re-pull their full
        # live set each run (absence = resolved).
        "staleness_policies": {
            "Shelby County Tax Sale": "full_rescan",
            "Shelby County Foreclosure": "full_rescan",
            "Shelby County Land Bank": "full_rescan",
            "Memphis MMLBA": "full_rescan",
            "Shelby Probate Court": "cursor",
        },
        # Per-source public-site + role metadata for /api/v1/sources. (url, category).
        "source_meta": {
            "Shelby County Tax Sale": ("https://www.shelbycountytrustee.com/191/Tax-Sale-Schedule", "deal"),
            "Shelby County Land Bank": ("https://landbank.shelbycountytn.gov/", "deal"),
            "Memphis MMLBA": ("https://mmlba.org/property-sales/", "deal"),
            "Shelby County Foreclosure": ("https://www.tnforeclosurenotices.com/results/counties/shelby/", "deal"),
            "Shelby Probate Court": ("https://prdata.shelbycountytn.gov/prweb/", "deal"),
            "Shelby Delinquent Tax": ("https://www.shelbycountytrustee.com/259/Delinquent-Realty-Lawsuit-List", "signal"),
            "Shelby Evictions": ("https://data.midsouth.io/", "signal"),
            "Shelby County Parcels (ReGIS)": ("https://scgis.shelbycountytn.gov/", "registry"),
            "Shelby Tax Delinquent (Lead)": ("https://www.shelbycountytrustee.com/259/Delinquent-Realty-Lawsuit-List", "lead"),
            "Shelby Eviction (Lead)": ("https://data.midsouth.io/", "lead"),
        },
        # Shelby probate case numbers (PR#####) carry NO filing year, so the
        # `filing_year_substr` variant (Cuyahoga) can't apply. Instead shelby_probate.py
        # parses + persists the court filing_date onto the listing, and the `filing_date`
        # mode compares it directly: a probate parcel sold AT/AFTER its filing date is no
        # longer an estate asset (run.py::_transfer_predicate). Rule 2 (sold-while-listed)
        # still applies on top once sale-date enrichment covers these parcels.
        "probate_transfer_rule": {"mode": "filing_date"},
        # TN redeemable tax deed (TCA 67-5-2701): after the sale the original owner has a
        # delinquency-age-driven window to redeem. Tiers evaluated top-down; first match
        # wins. `not_null` tier = basis present but below all thresholds; `default` = basis
        # NULL (assume the longest/safest window). Consumed by _compute_redemption_windows.
        "redemption_windows": {
            "signal_type": "tax_deed",
            "basis_column": "tax_years_delinquent",
            "tiers": [
                {"gte": 8, "days": 90, "basis": "8yr_plus"},
                {"gte": 5, "days": 180, "basis": "5_to_7yr"},
                {"not_null": True, "days": 365, "basis": "le_5yr"},
            ],
            "default": {"days": 365, "basis": "default_assumed"},
        },
        # Live cross-verify endpoints (scripts/playwright_source_check.py) — see the
        # cuyahoga note above. ePropertyPlus (land bank), Tax Sale CSV, ReGIS ArcGIS.
        "verifier_endpoints": {
            "epropertyplus_api": (
                "https://public-sctn.epropertyplus.com"
                "/landmgmtpub/remote/public/property/getPublishedProperties"
            ),
            "tax_sale_csv_url": "https://scgpublic.s3.amazonaws.com/TaxSaleExtract.csv",
            "arcgis_query_url": (
                "https://scgis.shelbycountytn.gov/serverhigh/rest/services/"
                "Parcel/CurrentParcels/MapServer/0/query"
            ),
        },
        "registry_link": registry_link,
        "registry_label": "Shelby County Trustee — One-Click Parcel Page",
        "registry_search_hint": (
            "Direct link opens the Trustee parcel page for {parcel} — ONE page shows owner, "
            "tax balance, billing history, and (when applicable) the Tax Sale notice. "
            "If native_parcel_id is not yet populated (link goes to the search page instead), "
            "type the street number + street name (no suffix) into the PropertyClient search form. "
            "Backup links: REGISTER GIS and ASSESSOR GIS are linked from the Trustee parcel page itself."
        ),
        "registry_look_for": (
            "On the Trustee parcel page: "
            "(1) PRIMARY OWNER should match our stored owner_name. "
            "(2) LOCATION / ADDRESS should match our stored situs address. "
            "(3) TAX DUE / TOTAL BALANCE > $0 = delinquent (expected for tax_deed). "
            "$0 balance on a tax_deed = owner paid off; lead may be dead. "
            "(4) For tax_deed: look for the 'TAX SALE PROPERTY NOTICE / Tax Sale ID: TS####' block — "
            "this is the definitive proof it is scheduled for tax sale. No block = not (yet) scheduled. "
            "(5) For land_bank: owner should read 'SHELBY COUNTY TAX SALE...' or similar. "
            "(6) For MMLBA: owner should read 'MEMPHIS METROPOLITAN LAND BANK AUTHORITY'. "
            "(7) The page has external links to REGISTER GIS and ASSESSOR GIS for deed history."
        ),
        "source_link": source_link,
        "offmarket_links": offmarket_links,
        "verification_guide": verification_guide,
    }


def _make_summit_market() -> dict:
    """Summit County (Akron, OH) market config.

    OH framework carries over from Cuyahoga (judicial foreclosure via sheriff sale,
    tax foreclosure, probate; sale FINAL at confirmation — no MI-style redemption).
    Single canonical parcel format: 7-digit zero-padded string (normalize_parcel_number
    'summit' branch). Buy-now sources: RealAuction (mortgage Fri + tax Tue, ONE source_site
    emitting two signal_types — the DLN precedent), Akron Legal News (cross-check / enrich),
    Probate (CourtView eServices), Land Bank (Tolemi). Pre-distress lever: delinquent-tax
    signal (SC720_DELQ tape) surfaced via surface_distress (enabled only after buy-now verify).

    AUTHORITY for the catalog cross-check = the Summit GIS spine `ownernme1` (current owner),
    the same role Cuyahoga EPV `parcel_owner` plays. RealAuction Tue/Fri rosters are pre-sale
    catalogs (rows can cancel/redeem/sell) — the live-authority owner cross-check is mandatory.
    """

    def registry_link(parcel: str | None, address: str | None,
                      native_parcel_id: str | None = None) -> str:
        # Summit Fiscal Office property search (human-facing current-owner authority).
        # No stable per-parcel deep-link param confirmed — opens the search page; the
        # GIS spine ownernme1 we ingest is the machine authority for the cross-check.
        return "https://fiscaloffice.summitoh.net/index.php/property-search"

    def source_link(signal_type: str, parcel: str | None, case: str | None,
                    sale_date, address: str | None,
                    native_parcel_id: str | None = None) -> str:
        sig = signal_type or ""
        if sig == "probate":
            return f"https://search.summitohioprobate.com/eservices/home.page.2   (search case {case or '?'})"
        elif sig in ("mortgage_foreclosure", "tax_delinquent_foreclosure"):
            return "https://summit.sheriffsaleauction.ohio.gov/   (Summit Sheriff Sale — find by case/parcel/date)"
        elif sig == "land_bank_inventory":
            return "https://summit-county-oh-publicity.tolemi.com/   (Summit County Land Bank inventory)"
        elif sig == "tax_delinquent":
            return "https://fiscaloffice.summitoh.net/index.php/property-search   (Fiscal Office delinquent-tax record)"
        else:
            return "https://fiscaloffice.summitoh.net/index.php/property-search"

    def offmarket_links(address: str, city: str | None, zip_: str | None) -> tuple[str, str]:
        return _zillow_url(address, city, "OH", zip_), _redfin_url(address, city, "OH", zip_)

    verification_guide = {
        "mortgage_foreclosure": {
            "valid": (
                "Row still on the Summit Sheriff Sale (RealAuction, Fridays) roster with matching "
                "case# and a sale date >= today. Owner faces a lender-forced sale. The Summit GIS "
                "Fiscal record confirms the current owner and shows no recent transfer."
            ),
            "red_flags": (
                "Row dropped off the RealAuction roster (sale cancelled / redeemed / settled). "
                "Sale date is in the past (STALE). GIS Fiscal record shows a new owner — already sold."
            ),
        },
        "tax_delinquent_foreclosure": {
            "valid": (
                "Row on the Summit Sheriff Sale (RealAuction, TUESDAYS) roster with the tax signature "
                "(Appraised $0.00 + flat $1,000 deposit). By ORC 5721.19 these are tax-impositions sales "
                "— already delinquent. Sale date >= today; GIS Fiscal confirms the parcel + owner."
            ),
            "red_flags": (
                "Row dropped off the Tuesday roster (redeemed before sale / cancelled). Sale date past "
                "(STALE). GIS shows a recent transfer to a new private owner (paid off / sold)."
            ),
        },
        "probate": {
            "valid": (
                "Summit Probate (CourtView eServices) shows the estate case OPEN (Case Status = Open). "
                "The case number's leading year matches its filing year. The decedent (NOT the fiduciary) "
                "maps to a Summit residential parcel — a real estate-in-probate where heirs may sell fast."
            ),
            "red_flags": (
                "Case Status = Closed (estate settled — dead lead). Decedent doesn't map to any real "
                "property. A GIS transfer at/after the case filing year means the property already sold "
                "out of the estate (our _mark_transferred_listings guard removes it)."
            ),
        },
        "land_bank_inventory": {
            "valid": (
                "Parcel still on the Summit County Land Bank (Tolemi publiCity) inventory. GIS Fiscal "
                "confirms the parcel. County-held, priced for redevelopment — expect discount + restrictions."
            ),
            "red_flags": (
                "Parcel no longer on the Land Bank inventory (sold / under contract — our scraper marks it "
                "not_listed next cycle). GIS shows a private owner (already conveyed out)."
            ),
        },
        # Pre-Distress LEAD (surface_distress.py) — surfaced only after buy-now is verified.
        "tax_delinquent": {
            "valid": (
                "On the SC720_DELQ certified-delinquent tape with a balance (>= $2k on a residential parcel) "
                "and a PRIVATE-party owner. PRE-DISTRESS LEAD — owner under tax pressure but NOT yet for sale "
                "through any channel. Off-market on Zillow/Redfin is consistent with a distressed owner (good)."
            ),
            "red_flags": (
                "Balance cured / absent from the new monthly tape (resolved). Already on a RealAuction roster "
                "(that's a buy-now deal, not a lead). GIS shows a recent transfer to a new private owner (sold). "
                "Actively listed on MLS (owner selling normally — note in outreach)."
            ),
        },
        # Pre-Distress LEAD (filing stage, ALN /notices/foreclosures) — surfaced only after buy-now verified.
        "foreclosure_filing": {
            "valid": (
                "A foreclosure COMPLAINT was filed in Common Pleas (Akron Legal News notice) — the earliest "
                "distress stage, months before any sheriff sale. PRE-DISTRESS LEAD: the owner is in the "
                "pipeline but the property is NOT yet at auction or for sale. GIS Fiscal confirms the parcel + "
                "a private-party owner. Off-market on Zillow/Redfin is consistent (good)."
            ),
            "red_flags": (
                "The case has already advanced to a scheduled sheriff sale on RealAuction (that's a buy-now "
                "deal now, not a lead). The complaint was dismissed/withdrawn. GIS shows a recent transfer to a "
                "new private owner (resolved / sold). Actively listed on MLS."
            ),
        },
    }

    return {
        "db": "tranchi",
        "state": "OH",
        "parcel_normalize_fn": normalize_parcel_number,  # global format-detector (unchanged)
        "market_filter": "l.market = 'summit'",
        # Pre-distress lead surfacing (surface_distress.py). Summit sources the lead ADDRESS
        # from the parcel SPINE (situs_address) — the SC720_DELQ tape's PROPERTY_ADDRESS is
        # ~26% blank, and its TAXBILL_ADDRESS is the OWNER MAILING address, not the situs; the
        # spine joins the 7-digit parcel 1:1 and always carries situs. gate_sql = the defensible
        # slice: certified balance >= $2k AND residential land-use (Summit LUC 5xx). Regex guards
        # on the cast columns keep a malformed payload value from aborting the whole INSERT.
        # The distress_lead_types row (migration 017) starts DISABLED — surfaced only once buy-now
        # is verified (G1 ruling 2026-06-11), then flip enabled=true.
        "distress_lead_county": "Summit",
        "distress_lead_rules": {
            "tax_delinquent": {
                "address_source": "spine",
                "address_key": None,
                "owner_key": "owner",
                "gate_sql": (
                    "((s.payload->>'delq_amount' ~ '^[0-9.]+$' "
                    "  AND (s.payload->>'delq_amount')::numeric >= 2000) "
                    " AND s.payload->>'luc' ~ '^5')"
                ),
            },
            # Foreclosure-FILING stage (Akron Legal News /notices/foreclosures) — the
            # earliest distress signal (complaint filed, months before any sheriff sale).
            # gate_sql=None: a filing is itself the escalation, so every fresh one surfaces
            # (unlike the 28K raw tax tape that needs a $2k floor). Address from the spine
            # (the 7-digit parcel joins 1:1). Surfaced only when its distress_lead_types row
            # is enabled — held OFF until buy-now is verified (G1 discipline).
            "foreclosure_filing": {
                "address_source": "spine",
                "address_key": None,
                "owner_key": "owner",
                "gate_sql": None,
            },
        },
        "deal_sources": (
            "mortgage_foreclosure",
            "tax_delinquent_foreclosure",
            "probate",
            "land_bank_inventory",
            "tax_delinquent",      # Pre-Distress LEAD (SC720_DELQ certified-delinquent tape)
            "foreclosure_filing",  # Pre-Distress LEAD (ALN Common Pleas foreclosure complaints)
        ),
        "source_sites": {
            # RealAuction is ONE source_site emitting BOTH mortgage (Fri) + tax (Tue)
            # signal_types — the DLN precedent. Mapped here to its primary (mortgage).
            "Summit Sheriff Sale (RealAuction)": "mortgage_foreclosure",
            "Akron Legal News": "mortgage_foreclosure",
            "Summit Probate Court": "probate",
            "Summit County Land Bank": "land_bank_inventory",
            "Summit Foreclosure Filings (ALN)": "foreclosure_filing",
            "Summit Tax Delinquent (Lead)": "tax_delinquent",
            "Summit Foreclosure Filing (Lead)": "foreclosure_filing",
        },
        # Per-source retirement policy. RealAuction (catalog) + ALN + Land Bank re-pull their
        # full live set each run (absence = resolved); Probate is a CURSOR forward-walk
        # (retired only by case_status re-check).
        "staleness_policies": {
            "Summit Sheriff Sale (RealAuction)": "full_rescan",
            "Akron Legal News": "full_rescan",
            "Summit County Land Bank": "full_rescan",
            "Summit Probate Court": "cursor",
        },
        # Per-source public-site + role metadata for /api/v1/sources. (url, category).
        "source_meta": {
            "Summit County Parcels (GIS)": (
                "https://scgis.summitoh.net/hosted/rest/services/parcels_web_GEODATA_Tax_Parcels/FeatureServer/0",
                "registry",
            ),
            "Summit Sheriff Sale (RealAuction)": ("https://summit.sheriffsaleauction.ohio.gov/", "deal"),
            "Akron Legal News": ("https://www.akronlegalnews.com/notices/sheriff_sale_abstracts", "deal"),
            "Summit Probate Court": ("https://search.summitohioprobate.com/eservices/home.page.2", "deal"),
            "Summit County Land Bank": ("https://summit-county-oh-publicity.tolemi.com/", "deal"),
            "Summit Delinquent Tax": (
                "https://fiscaloffice.summitoh.net/index.php/documents-a-forms/finish/10-cama/282-sc720delq",
                "signal",
            ),
            "Summit Foreclosure Filings (ALN)": ("https://www.akronlegalnews.com/notices/foreclosures", "signal"),
            # Pre-distress LEADS (surface_distress.py) — category 'lead' groups them under Pre-Distress.
            "Summit Tax Delinquent (Lead)": ("https://fiscaloffice.summitoh.net/index.php/property-search", "lead"),
            "Summit Foreclosure Filing (Lead)": ("https://www.akronlegalnews.com/notices/foreclosures", "lead"),
        },
        # Summit probate case numbers encode the filing year as the first 4 chars
        # ('2026 ES 00449'). The transfer guard pulls the year via substring(1,4) and
        # the regex identifies a Summit probate case (run.py::_transfer_predicate).
        "probate_transfer_rule": {
            "case_regex": r"^[0-9]{4} ES",
            "filing_year_substr": (1, 4),
        },
        # OH sale is FINAL at court confirmation — no statutory owner redemption window.
        "redemption_windows": None,
        # Live cross-verify endpoints (scripts/playwright_source_check.py reads URLs from here).
        "verifier_endpoints": {
            "gis_query": (
                "https://scgis.summitoh.net/hosted/rest/services/"
                "parcels_web_GEODATA_Tax_Parcels/FeatureServer/0/query"
            ),
            "realauction_base": "https://summit.sheriffsaleauction.ohio.gov/",
            "landbank_graphql": "https://cg.tolemi.com/q",
            "aln_base": "https://www.akronlegalnews.com/notices/sheriff_sale_abstracts",
            "delq_url": "https://fiscaloffice.summitoh.net/index.php/documents-a-forms/finish/10-cama/282-sc720delq",
        },
        "registry_link": registry_link,
        "registry_label": "Summit County Fiscal Office — Property Search",
        "registry_search_hint": (
            "Opens the Summit Fiscal Office property search — type the 7-digit parcel number "
            "(or the street address) to pull the current-owner record. The GIS Tax Parcels "
            "layer is the machine authority behind our stored owner."
        ),
        "registry_look_for": (
            "(1) Owner name on the Fiscal record should match our stored owner. "
            "(2) Situs address should match our stored address. "
            "(3) The most recent transfer/sale date is our last_sale_date — a transfer at-or-after "
            "a probate case's filing year (or after we first listed) means the property already sold; "
            "the _mark_transferred_listings guard removes such listings automatically."
        ),
        "source_link": source_link,
        "offmarket_links": offmarket_links,
        "verification_guide": verification_guide,
    }


def _make_wayne_market() -> dict:
    """Wayne County (Detroit, MI) market config — the first non-OH/TN market.

    NEW LEGAL FRAMEWORK (MCL-verified, G1 2026-06-11):
      - Mortgage foreclosure = NON-JUDICIAL (by advertisement), MCL 600.3201+. The legal
        organ of record is Detroit Legal News (mipublicnotices area=82). POST-SALE the owner
        has a 6-MONTH redemption window (MCL 600.3240; abandoned = 1mo, MCL 600.3241a) — so a
        sheriff-deed is NOT final at sale. We surface BOTH pre-sale notices AND post-sale
        in-redemption rows, tagged, with a computed redemption window keyed on `sale_date`.
        (The redemption_windows policy below is wired in Phase 3 with wayne_foreclosure +
        the run.py _compute_redemption_windows / _mark_expired generalization; held None here
        so Phase-0 foundation stays additive — mortgage_foreclosure also exists in OH where
        the sale IS final, so the in-redemption carve-out must be MARKET-scoped, not blanket.)
      - Tax foreclosure auction = JUDICIAL tax-DEED (MCL 211.78+): ~3yr delinquency → Circuit
        Court Judgment → CLEAN deed at sale, NO post-sale redemption (Rafaeli surplus claims
        don't affect buyer title). Seasonal (Sept main auction); wayne_tax_auction ships dormant.

    TWO PARCEL FORMATS (normalize_parcel_number 'wayne' branch in db.py): Detroit ward
    '02000184.'/'02000185-6'/'03001910.001' (significant trailing '.'/'-'/alpha) + out-county
    packed 14-digit '35024030846002'. Detroit-only spine for v1 (county layer 9yr stale).

    PROBATE = Phase 2 (Bison-pattern Vision PDF extractor over PC565/PC559) — not in this build;
    probate_transfer_rule stays None until it ships.
    """

    def registry_link(parcel: str | None, address: str | None,
                      native_parcel_id: str | None = None) -> str:
        # Wayne County Treasurer property-tax lookup (pto): search by parcel/address, shows
        # current owner + live delinquency status — the authoritative "is this still in
        # foreclosure / who owns it now" cross-check named in the field-maps. ASP.NET search
        # page (human types the parcel); no stable per-parcel deep-link param.
        return "https://pto.waynecounty.com/"

    def source_link(signal_type: str, parcel: str | None, case: str | None,
                    sale_date, address: str | None,
                    native_parcel_id: str | None = None) -> str:
        sig = signal_type or ""
        if sig == "mortgage_foreclosure":
            return "https://www.mipublicnotices.com/   (search the foreclosure notice by address/mortgagor)"
        elif sig == "tax_delinquent_foreclosure":
            return "https://www.waynecountytreasurermi.com/   (Wayne County Treasurer tax-foreclosure auction)"
        elif sig == "land_bank_inventory":
            return "https://buildingdetroit.org/   (Detroit Land Bank — or waynecountylandbank.com for out-county)"
        elif sig == "tax_delinquent":
            return "https://pto.waynecounty.com/   (Wayne Treasurer delinquency / forfeiture record)"
        elif sig == "blight_violation":
            return "https://data.detroitmi.gov/datasets/blight-violations   (Detroit blight-ticket record)"
        else:
            return "https://pto.waynecounty.com/"

    def offmarket_links(address: str, city: str | None, zip_: str | None) -> tuple[str, str]:
        return _zillow_url(address, city, "MI", zip_), _redfin_url(address, city, "MI", zip_)

    verification_guide = {
        "mortgage_foreclosure": {
            "valid": (
                "Foreclosure-by-advertisement notice still published (Detroit Legal News / "
                "mipublicnotices) OR within the 6-MONTH post-sale redemption window (MCL 600.3240). "
                "MI NOTE: a PAST sale date is NOT stale here — post-sale the owner can still redeem "
                "for 6 months, so an in-redemption row is a live off-market lead (tagged 'in redemption'). "
                "pto.waynecounty.com confirms the parcel + a private-party owner still on title."
            ),
            "red_flags": (
                "Notice dropped off the feed with no re-publication AND the 6-month redemption window "
                "has elapsed (deed now final — our _finalize_expired_redemptions flips it to 'final'). "
                "pto/Detroit assessor shows a transfer to a new private owner after we listed it "
                "(redeemed / sold — _mark_transferred_listings removes it)."
            ),
        },
        "tax_delinquent_foreclosure": {
            "valid": (
                "Parcel still on the Wayne County Treasurer auction (STATUS active, auction window OPEN). "
                "Judicial tax-DEED: clean fee title, NO post-sale redemption. Min bid starts at back-tax "
                "(often <$2k; many no-reserve $900 lots). SEV (≈50% of market) high vs min bid = equity. "
                "pto.waynecounty.com confirms the parcel is still delinquent / Treasurer-held."
            ),
            "red_flags": (
                "Row carries STATUS_CD=RM (REMOVED — redeemed/withdrawn) OR the auction window has closed "
                "(sale_date past = the auction ran; clean deed, no redemption to wait on). "
                "pto shows the taxes paid / a new private owner (redeemed before sale)."
            ),
        },
        "land_bank_inventory": {
            "valid": (
                "Still listed on buildingdetroit.org (Auction / Own It Now / Rehabbed & Ready, or a "
                "buyable Marketed/Improved/Oversized lot) with marketable_feature != 'Under Contract', "
                "OR on the Wayne County Land Bank (ePropertyPlus) with available='Y'. Government-owned, "
                "priced to move; expect deep discount + possible deed restrictions."
            ),
            "red_flags": (
                "Property moved to buildingdetroit.org /pastlistings (sold) or vanished from the For-Sale "
                "layer (our scraper marks it not_listed next cycle). 'Under Contract' flag set. Restricted "
                "Side/Neighborhood lot (adjacent-owner-only) — those are coverage, never shown as a deal."
            ),
        },
        # Pre-Distress LEADS (surface_distress.py) — surfaced only AFTER buy-now is verified
        # (G3-era), with the gate tightened then (Detroit raw volume is huge; defensibility +
        # diversity over raw count). distress_lead_types rows stay DISABLED until then.
        "tax_delinquent": {
            "valid": (
                "On the Wayne County Treasurer forfeiture roll (≥2yr delinquent, facing the 2026 "
                "circuit-court tax foreclosure) with a PRIVATE-party owner. PRE-DISTRESS LEAD — under "
                "tax pressure but NOT yet for sale through any channel; owner can still redeem (prime "
                "outreach). Off-market on Zillow/Redfin is consistent (good). pto confirms a balance owed."
            ),
            "red_flags": (
                "pto shows the balance paid / parcel redeemed (resolved). Parcel already on the Treasurer "
                "AUCTION (that's a buy-now deal now, not a lead). Transfer to a new private owner (sold). "
                "Actively listed on MLS (selling normally — note it). NOT itself an offering of sale."
            ),
        },
        "blight_violation": {
            "valid": (
                "Parcel carries unpaid Detroit blight judgment(s) — amt_balance_due > 0, disposition "
                "'Responsible…', ideally In Collections or multiple tickets — with an LLC / absentee owner. "
                "PRE-DISTRESS LEAD: owner has stopped maintaining and stopped paying; blight debt attaches "
                "to the parcel and precedes tax-foreclosure. Off-market on Zillow/Redfin is consistent (good)."
            ),
            "red_flags": (
                "Balance paid in full (amt_balance_due = 0 — resolved). Disposition 'Not responsible' "
                "(dismissed). Recent transfer to a new owner on the Detroit assessor record (already sold). "
                "Single trivial ticket with $0 balance is weak — needs the qualified slice."
            ),
        },
    }

    return {
        "db": "tranchi",
        "state": "MI",
        "parcel_normalize_fn": normalize_parcel_number,  # global format-detector (unchanged)
        # Verify sample scope is by the county-level `market` column, not state (mirrors the
        # other markets since c557b57 — two same-state markets must not cross-contaminate).
        "market_filter": "l.market = 'wayne'",
        # Pre-distress lead surfacing (surface_distress.py). Wayne sources the lead ADDRESS from
        # the parcel SPINE (Detroit assessor situs is well-covered). blight_violation carries the
        # STRICT VALIDITY FLOOR (below) + A/B/C conviction tiers stamped by wayne_blight_tiering.py;
        # tax_delinquent stays loose (gate_sql=None) and DEFERRED (forfeiture PDF WAF-blocked,
        # annual). The distress_lead_types rows stay DISABLED until verified, so nothing surfaces
        # until `UPDATE tranchi.distress_lead_types SET enabled=true` after the go-live verify pass.
        "distress_lead_county": "Wayne",
        "distress_lead_rules": {
            "tax_delinquent": {"address_source": "spine", "address_key": None,
                               "owner_key": "owner", "gate_sql": None},
            # STRICT VALIDITY FLOOR (live-verified 2026-06-17 → ~43.8k floor parcels). Surfaces
            # only REAL, verifiable blight distress; conviction tiers (A/B/C) are stamped above
            # this floor by wayne_blight_tiering.py. Each predicate is a validity gate, not a
            # volume knob: Responsible* = owner IS liable (excludes 'Not responsible'/'Pending'/
            # blank); In Collections = city escalated (real debt); balance>0 = unpaid; residential
            # = Marc's buy-box + forces a spine join (verifiable). NOTE: gate_sql is interpolated
            # into surface_distress retire/refresh EXISTS subqueries that have ONLY `s` (no `p`
            # join) — so the residential check MUST be a correlated subquery on s.parcel_number,
            # never a bare p.* reference, or those statements throw. Regex-guard the numeric cast.
            "blight_violation": {
                "address_source": "spine", "address_key": None, "owner_key": "owner",
                # DELTA-pull source: wayne_blight scrapes incrementally on ticket_updated_at,
                # so only changed tickets get last_seen_at bumped each run; the bulk stay at
                # the last FULL re-pull's timestamp. The default 4-day _FRESH window therefore
                # aged out all ~43.8k leads ~4 days after the last full pull (the 2026-06-21
                # collapse: 43,718 → 6). Widen to 21 days so leads survive between the weekly
                # `wayne_blight --full` re-pull cron (heavy ~200k-row pull) without ever going
                # dark; resolved/paid tickets are still retired by gate_sql on the next delta
                # touch. See surface_distress.py `_lead_fragments` "fresh".
                "freshness_sql": "now() - interval '21 days'",
                "gate_sql": (
                    "(s.payload->>'disposition' ILIKE 'Responsible%' "
                    " AND s.payload->>'collection_status' = 'In Collections' "
                    " AND s.payload->>'amt_balance_due' ~ '^[0-9.]+$' "
                    " AND (s.payload->>'amt_balance_due')::numeric > 0 "
                    " AND EXISTS (SELECT 1 FROM tranchi.parcels p2 "
                    "             WHERE p2.parcel_number = s.parcel_number "
                    "               AND p2.market = s.market "
                    "               AND p2.property_class ILIKE 'RESIDENTIAL%'))"
                ),
            },
        },
        "deal_sources": (
            "mortgage_foreclosure",
            "tax_delinquent_foreclosure",
            "land_bank_inventory",
            "tax_delinquent",     # Pre-Distress LEAD (Treasurer forfeiture roll)
            "blight_violation",   # Pre-Distress LEAD (Detroit blight tickets)
        ),
        "source_sites": {
            "Wayne County Foreclosure": "mortgage_foreclosure",
            "Wayne County Treasurer's Auction": "tax_delinquent_foreclosure",
            "Detroit Land Bank Authority": "land_bank_inventory",
            "Wayne County Land Bank": "land_bank_inventory",
            "Wayne Tax Delinquent (Lead)": "tax_delinquent",
            "Wayne Blight (Lead)": "blight_violation",
        },
        # Per-source retirement policy (listings source_sites). All four deal feeds re-pull their
        # full live set each run (absence = resolved). Tax-auction is year-versioned full_rescan,
        # gated to an OPEN auction window inside the scraper (ships dormant). Signals (blight DELTA,
        # forfeiture ARCHIVE) own their own retirement in-scraper, not here.
        "staleness_policies": {
            "Wayne County Foreclosure": "full_rescan",
            "Detroit Land Bank Authority": "full_rescan",
            "Wayne County Land Bank": "full_rescan",
            "Wayne County Treasurer's Auction": "full_rescan",
        },
        "source_meta": {
            "Wayne County Foreclosure": ("https://www.mipublicnotices.com/", "deal"),
            "Wayne County Treasurer's Auction": ("https://www.waynecountytreasurermi.com/", "deal"),
            "Detroit Land Bank Authority": ("https://buildingdetroit.org/", "deal"),
            "Wayne County Land Bank": ("https://www.waynecountylandbank.com/", "deal"),
            "Detroit Open Data — Blight Tickets": ("https://data.detroitmi.gov/datasets/blight-violations", "signal"),
            "Wayne County Treasurer — Forfeiture List": (
                "https://www.waynecountymi.gov/Government/Elected-Officials/Treasurer/Property-Tax-Information/Forfeited-Property-List-with-Interested-Parties",
                "signal",
            ),
            "Detroit Open Data — Parcels (Current)": ("https://data.detroitmi.gov/", "registry"),
            "Wayne Tax Delinquent (Lead)": ("https://pto.waynecounty.com/", "lead"),
            "Wayne Blight (Lead)": ("https://data.detroitmi.gov/datasets/blight-violations", "lead"),
        },
        # Probate is Phase 2 (Vision PDF extractor) — no auto-transfer rule until it ships.
        "probate_transfer_rule": None,
        # MI mortgage redemption (MCL 600.3240) is a FLAT 6-month window keyed on `sale_date`
        # (not TN's delinquency-age tiers keyed on confirmation_order_date). Held None here;
        # wired in Phase 3 with wayne_foreclosure + the run.py _compute_redemption_windows /
        # _mark_expired generalization (the in-redemption carve-out is MARKET-scoped because
        # mortgage_foreclosure also exists in OH, where the sale is final at confirmation).
        "redemption_windows": None,
        # Live cross-verify endpoints (scripts/playwright_source_check.py reads URLs from here).
        "verifier_endpoints": {
            "parcels_arcgis": "https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest/services/parcel_file_current/FeatureServer/0/query",
            "property_sales_arcgis": "https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest/services/assessor_property_sales_view/FeatureServer/0/query",
            "blight_arcgis": "https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest/services/blight_tickets/FeatureServer/0/query",
            "mipublicnotices_api": "https://www.mipublicnotices.com/api/v1/search/search",
            "tax_auction_api": "https://waynecountytreasurermi.com/api/General/SearchItems",
            "dlba_api": "https://buildingdetroit.org/properties",
            "wclb_api": "https://public-wclb.epropertyplus.com/landmgmtpub/remote/public/property/getPublishedProperties",
            "pto_base": "https://pto.waynecounty.com/",
        },
        "registry_link": registry_link,
        "registry_label": "Wayne County Treasurer (pto) — live owner + delinquency lookup",
        "registry_search_hint": (
            "Opens pto.waynecounty.com — type the parcel ID (keep the trailing '.'/'-' exactly — "
            "it is significant) or the street address to pull the current owner + live tax status. "
            "The machine authority behind our stored owner/situs is the Detroit Open Data parcel + "
            "property-sales layers (city assessor)."
        ),
        "registry_look_for": (
            "(1) Owner on pto / Detroit assessor should match our stored owner_name. "
            "(2) Situs address should match. "
            "(3) For tax_delinquent / tax-auction: a balance owed > $0 confirms live distress; $0 = "
            "redeemed (dead). "
            "(4) The most recent transfer date (Detroit Property Sales) is our last_sale_date — a "
            "transfer AFTER we first listed means the parcel sold; _mark_transferred_listings removes it. "
            "(5) Mortgage foreclosure: a PAST sale date is still valid while inside the 6-month MI "
            "redemption window (tagged 'in redemption')."
        ),
        "source_link": source_link,
        "offmarket_links": offmarket_links,
        "verification_guide": verification_guide,
    }


def _make_lucas_market() -> dict:
    """Lucas County (Toledo, OH) market config — the 5th market (G1-approved, S3 build).

    OH framework carries over from Cuyahoga/Summit (judicial foreclosure via sheriff sale,
    tax foreclosure, probate; sale FINAL at court confirmation — NO MI-style redemption →
    KILL on sold/confirmed; tax parcels KILL on redeemed pre-sale). Canonical parcel = PARID
    7-digit zero-padded — FORM-IDENTICAL to Summit, so parcel_normalize_fn is dispatched to
    normalize_parcel_lucas (F-008) and identity is (parcel_number, 'lucas').

    DIFFERENCE FROM SUMMIT (do NOT reuse Summit's discriminators): RealAuction weekday split is
    WEDNESDAY -> mortgage_foreclosure (appraised; opening = 2/3 appraisal, R.C. 2329.52) and
    THURSDAY -> tax_delinquent_foreclosure (non-appraised $0.00; opening = taxes+costs; 10%-of-bid
    deposit, ORC 5721.19). Classify by weekday + per-item signature, NEVER by plaintiff.

    AUTHORITY for the catalog cross-check = the AREIS spine OWNER (current owner of record), the
    same role Summit GIS ownernme1 plays. RealAuction Wed/Thu rosters are pre-sale catalogs.

    BUILD STATUS (engine-box session): registry (lucas_parcels / AREIS) is built + wired. The
    deal/signal scrapers — lucas_realauction (Wed+Thu), lucas_legalnews (Toledo Legal News),
    lucas_delinquent_tax (Column ~19k list, the 5k lever), lucas_probate (time-boxed) — are built
    + validated on the engine box (anti-bot/IP-gated endpoints, unreachable off-box). As each is
    validated, add it to MARKET_SCRAPERS['lucas'].deal_and_signal + run.py, and add its source_meta
    / staleness_policies / distress_lead_types row here. The descriptive verification_guide and
    deal_sources below are pre-written so verify/links work the moment data flows.
    """

    def registry_link(parcel: str | None, address: str | None,
                      native_parcel_id: str | None = None) -> str:
        # Lucas County Auditor AREIS property search (human-facing current-owner authority).
        # The machine authority behind our stored owner is the AREIS MapServer L38 OWNER field.
        return "https://areis.co.lucas.oh.us/"

    def source_link(signal_type: str, parcel: str | None, case: str | None,
                    sale_date, address: str | None,
                    native_parcel_id: str | None = None) -> str:
        sig = signal_type or ""
        if sig in ("mortgage_foreclosure", "tax_delinquent_foreclosure"):
            return "https://lucas.sheriffsaleauction.ohio.gov/   (Lucas Sheriff Sale — find by case/parcel/date)"
        elif sig == "foreclosure_filing":
            return "https://www.toledolegalnews.com/legal_notices/foreclosure_sherrif_sales_lucas/   (Toledo Legal News)"
        elif sig == "probate":
            return f"https://www.lucas-co-probate-ct.org/   (Lucas Probate — search estate case {case or '?'})"
        elif sig == "tax_delinquent":
            return "https://ohio.column.us/   (Auditor Delinquent Land Tax List — find by parcel)"
        else:
            return "https://areis.co.lucas.oh.us/"

    def offmarket_links(address: str, city: str | None, zip_: str | None) -> tuple[str, str]:
        return _zillow_url(address, city, "OH", zip_), _redfin_url(address, city, "OH", zip_)

    verification_guide = {
        "mortgage_foreclosure": {
            "valid": (
                "Row still on the Lucas Sheriff Sale (RealAuction, WEDNESDAYS) roster with matching "
                "case# and a sale date >= today. Mortgage signature: appraised property, opening = 2/3 "
                "of appraisal (R.C. 2329.52). The AREIS Auditor record confirms the owner of record and "
                "shows no recent transfer."
            ),
            "red_flags": (
                "Row dropped off the Wednesday roster (cancelled / redeemed / settled). Sale date past "
                "(STALE). AREIS shows a new owner — already sold (OH sale is FINAL at confirmation)."
            ),
        },
        "tax_delinquent_foreclosure": {
            "valid": (
                "Row on the Lucas Sheriff Sale (RealAuction, THURSDAYS) roster with the TAX signature "
                "(Appraised $0.00 + opening = taxes+costs + 10%-of-bid deposit, ORC 5721.19). Sale date "
                ">= today; AREIS confirms the parcel + owner. Classified by weekday+signature, not plaintiff."
            ),
            "red_flags": (
                "Row dropped off the Thursday roster (redeemed before sale / cancelled). Sale date past "
                "(STALE). AREIS shows a transfer to a new private owner (paid off / sold)."
            ),
        },
        "probate": {
            "valid": (
                "Lucas Probate shows the estate case OPEN. The decedent (NOT the fiduciary) maps to a "
                "Lucas residential parcel — real estate-in-probate where heirs may sell fast."
            ),
            "red_flags": (
                "Case Closed (settled — dead lead). Decedent doesn't map to any real property. An AREIS "
                "transfer at/after the case filing means the property already sold out of the estate."
            ),
        },
        # Pre-Distress LEAD (surface_distress.py) — surfaced only after buy-now is verified.
        "tax_delinquent": {
            "valid": (
                "On the Auditor Delinquent Land Tax List (Column ~19k) with a real certified balance on a "
                "RESIDENTIAL parcel (LUC 5xx) and a PRIVATE-party owner. PRE-DISTRESS LEAD — owner under tax "
                "pressure but NOT yet for sale through any channel. Off-market on Zillow/Redfin is consistent."
            ),
            "red_flags": (
                "Balance cured / absent from the new list. Already on a RealAuction roster (that's a buy-now "
                "deal, not a lead). AREIS shows a recent transfer to a new private owner (sold). Actively on MLS."
            ),
        },
        "foreclosure_filing": {
            "valid": (
                "A foreclosure COMPLAINT/notice appears in Toledo Legal News (Common Pleas filing stage) — the "
                "earliest distress stage, months before any sheriff sale. PRE-DISTRESS LEAD: owner in the "
                "pipeline, property not yet at auction or for sale. AREIS confirms the parcel + a private owner."
            ),
            "red_flags": (
                "The case already advanced to a scheduled sheriff sale on RealAuction (buy-now now, not a lead). "
                "The complaint was dismissed/withdrawn. AREIS shows a transfer to a new private owner. On MLS."
            ),
        },
    }

    return {
        "db": "tranchi",
        "state": "OH",
        # PARID is form-identical to Summit → market-dispatched normalizer (F-008), NOT the global
        # format-detector. This is the load-bearing reason parcels identity is (parcel_number, market).
        "parcel_normalize_fn": normalize_parcel_lucas,
        "market_filter": "l.market = 'lucas'",
        "distress_lead_county": "Lucas",
        # Pre-distress levers. Lead address from the AREIS spine situs (the Column list / Legal News
        # notices may carry partial situs). The delinquent gate enforces RULE #1: a real certified
        # balance AND residential land-use (Lucas LUC 5xx). distress_lead_types rows ship DISABLED —
        # enabled only after buy-now is verified (G1 discipline). Payload keys (delq_amount, luc)
        # follow the Summit/Shelby convention — the lucas_delinquent_tax scraper emits them.
        "distress_lead_rules": {
            "tax_delinquent": {
                "address_source": "spine",
                "address_key": None,
                "owner_key": "owner",
                "gate_sql": (
                    "((s.payload->>'delq_amount' ~ '^[0-9.]+$' "
                    "  AND (s.payload->>'delq_amount')::numeric >= 2000) "
                    " AND s.payload->>'luc' ~ '^5')"
                ),
            },
            "foreclosure_filing": {
                "address_source": "spine",
                "address_key": None,
                "owner_key": "owner",
                "gate_sql": None,
            },
        },
        "deal_sources": (
            "mortgage_foreclosure",
            "tax_delinquent_foreclosure",
            "probate",
            "tax_delinquent",      # Pre-Distress LEAD (Auditor Delinquent Land Tax List ~19k — the 5k lever)
            "foreclosure_filing",  # Pre-Distress LEAD (Toledo Legal News Common Pleas filings)
        ),
        "source_sites": {
            # RealAuction is ONE source_site emitting BOTH mortgage (Wed) + tax (Thu) signal_types.
            "Lucas Sheriff Sale (RealAuction)": "mortgage_foreclosure",
            "Toledo Legal News": "mortgage_foreclosure",
            "Lucas Probate Court": "probate",
            "Lucas Tax Delinquent (Lead)": "tax_delinquent",
            "Lucas Foreclosure Filing (Lead)": "foreclosure_filing",
        },
        # Built sources only (registry). Add per-source policies as the deal/signal scrapers land.
        "staleness_policies": {
            "Lucas Sheriff Sale (RealAuction)": "full_rescan",
            "Toledo Legal News": "full_rescan",
        },
        # Registry surfaced now; deal/signal source_meta added as each scraper is validated
        # (so the live /sources dashboard does not list unbuilt 0-count sources).
        "source_meta": {
            "Lucas County Parcels (AREIS)": (
                "https://lcaudgis.co.lucas.oh.us/gisaudserver/rest/services/AREIS_Web_Map_MIL1/MapServer/38",
                "registry",
            ),
        },
        # Lucas probate case format TBD (vendor unconfirmed — time-boxed recon). Filled when
        # lucas_probate lands; None until then so the transfer guard is a no-op for Lucas.
        "probate_transfer_rule": None,
        # OH sale is FINAL at court confirmation — no statutory owner redemption window.
        "redemption_windows": None,
        "verifier_endpoints": {
            "gis_query": (
                "https://lcaudgis.co.lucas.oh.us/gisaudserver/rest/services/"
                "AREIS_Web_Map_MIL1/MapServer/38/query"
            ),
            "realauction_base": "https://lucas.sheriffsaleauction.ohio.gov/",
            "legalnews_base": "https://www.toledolegalnews.com/legal_notices/foreclosure_sherrif_sales_lucas/",
            "delq_url": "https://ohio.column.us/",
        },
        "registry_link": registry_link,
        "registry_label": "Lucas County Auditor — AREIS Property Search",
        "registry_search_hint": (
            "Opens the Lucas County Auditor AREIS search — type the 7-digit PARID (or the street "
            "address) to pull the current-owner record. The AREIS MapServer L38 OWNER field is the "
            "machine authority behind our stored owner."
        ),
        "registry_look_for": (
            "(1) Owner name on the AREIS record should match our stored owner. "
            "(2) Situs address should match our stored address. "
            "(3) The most recent transfer/sale date — a transfer after we first listed means the "
            "property already sold; the _mark_transferred_listings guard removes such listings."
        ),
        "source_link": source_link,
        "offmarket_links": offmarket_links,
        "verification_guide": verification_guide,
    }


# The market registry. To add a market: implement _make_<market>_market() and add it here.
MARKETS: dict[str, dict] = {
    "cuyahoga": _make_cuyahoga_market(),
    "shelby": _make_shelby_market(),
    "summit": _make_summit_market(),
    "wayne": _make_wayne_market(),
    "lucas": _make_lucas_market(),
}


# ---------------------------------------------------------------- scraper registry
# Which scraper keys (app.scrapers.run._SCRAPERS) belong to each market. This is the
# "add a market" ergonomic: list a market's scrapers in ONE place instead of scattering
# them across run.py / staleness.py / prefilter.py. `registry` = the parcel-spine scraper
# (writes tranchi.parcels only, runs on its own weekly cron, skipped from the 3h full run).
#
# `registry_in_full_run`: whether the parcel-spine scraper runs inside the every-3h
# full run. Cuyahoga's MyPlace sweep is light (~7K hits, enrich_detail=False) so it
# rides the 3h run; Shelby's ReGIS sweep is 353K parcels — too heavy for 3h, so it is
# EXCLUDED and scheduled on its own weekly cron (run explicitly via --site). This is
# the knob behind run.py's full-run skip set (see full_run_skip_keys()).
MARKET_SCRAPERS: dict[str, dict] = {
    "cuyahoga": {
        "registry": "fiscal_officer",
        "registry_in_full_run": True,
        "deal_and_signal": [
            "code_violations", "land_bank", "sheriff_sales", "probate", "dln",
            "forfeited_land", "delinquent_tax",
        ],
    },
    "shelby": {
        "registry": "shelby_parcels",
        "registry_in_full_run": False,
        "deal_and_signal": [
            "shelby_tax_sale", "shelby_foreclosure", "shelby_delinquent_tax",
            "shelby_county_landbank", "shelby_mmlba", "shelby_probate", "shelby_evictions",
        ],
    },
    "summit": {
        # 261K-parcel GIS sweep is too heavy for the 3h run → own weekly cron (--site summit_parcels).
        "registry": "summit_parcels",
        "registry_in_full_run": False,
        "deal_and_signal": [
            "summit_realauction",     # mortgage (Fri) + tax (Tue) foreclosure — ONE file, two signal_types
            "summit_legalnews",       # Akron Legal News sheriff-sale cross-check / enrich
            "summit_probate",         # Summit Probate Court (CourtView eServices)
            "summit_landbank",        # Summit County Land Bank (Tolemi GraphQL)
            "summit_delinquent_tax",  # SC720_DELQ certified-delinquent SIGNAL (pre-distress lever)
            "summit_foreclosure_filings",  # ALN Common Pleas foreclosure-FILING SIGNAL (pre-distress)
        ],
    },
    "wayne": {
        # 378K Detroit-parcel ArcGIS sweep (+ 509K property-sales enrich) too heavy for the 3h
        # run → own weekly cron (--site wayne_parcels). Probate is Phase 2 (not listed yet).
        "registry": "wayne_parcels",
        "registry_in_full_run": False,
        "deal_and_signal": [
            "wayne_foreclosure",     # mortgage foreclosure (mipublicnotices area=82 + auction.com) — redemption lifecycle
            "wayne_dlba",            # Detroit Land Bank (buildingdetroit + ArcGIS) — structures + buyable lots
            "wayne_wclb",            # Wayne County Land Bank (ePropertyPlus, out-county)
            "wayne_tax_auction",     # Wayne Treasurer tax-foreclosure auction — SEASONAL, ships dormant
            "wayne_blight",          # Detroit blight tickets SIGNAL (pre-distress lever; DELTA on ticket_updated_at)
            "wayne_delinquent_tax",  # Wayne Treasurer forfeiture-PDF SIGNAL (pre-distress)
        ],
    },
    "lucas": {
        # ~192K AREIS parcel sweep (L38 + L84 value join) too heavy for the 3h run → own weekly
        # cron (--site lucas_parcels). deal_and_signal is filled on the engine box as each anti-bot
        # source is validated + built: lucas_realauction (Wed mortgage + Thu tax), lucas_legalnews
        # (Toledo Legal News), lucas_delinquent_tax (Column ~19k — the 5k lever), lucas_probate
        # (time-boxed). Empty here so the engine never registers an unrunnable/unvalidated scraper.
        "registry": "lucas_parcels",
        "registry_in_full_run": False,
        "deal_and_signal": [],
    },
}


def all_states() -> set[str]:
    """Every state that has a live market — the prefilter allowlist source of truth."""
    return {cfg["state"] for cfg in MARKETS.values()}


def registry_scraper_keys() -> set[str]:
    """The parcel-spine scraper keys across all markets (run on their own weekly cron)."""
    return {m["registry"] for m in MARKET_SCRAPERS.values()}


def merged_staleness_policies() -> dict[str, str]:
    """Every market's {source_site: policy-string} merged into one dict.

    The source of truth behind staleness.SOURCE_STALENESS. Policy strings are the
    StalenessPolicy enum *values* ('full_rescan' | 'cursor' | 'archive') so this module
    stays import-free of staleness.py (which imports market_config — avoids a cycle).
    """
    out: dict[str, str] = {}
    for cfg in MARKETS.values():
        out.update(cfg.get("staleness_policies", {}))
    return out


def merged_source_meta() -> dict[str, tuple[str | None, str]]:
    """Every market's {source_site: (url, category)} merged — backs sources._SOURCE_META."""
    out: dict[str, tuple[str | None, str]] = {}
    for cfg in MARKETS.values():
        out.update(cfg.get("source_meta", {}))
    return out


def full_run_skip_keys() -> set[str]:
    """Registry spines EXCLUDED from the every-3h full run (heavy weekly-cron sweeps).

    A new heavy-registry market opts out by setting `registry_in_full_run=False` in its
    MARKET_SCRAPERS entry — no edit to run.py. Today returns {'shelby_parcels'}.
    """
    return {
        m["registry"] for m in MARKET_SCRAPERS.values()
        if not m.get("registry_in_full_run", True)
    }


def state_for_market(market: str) -> str:
    """The 2-letter property_state for a market slug (e.g. 'cuyahoga' -> 'OH')."""
    return MARKETS[market]["state"]


def market_for_scraper(scraper_key: str) -> str | None:
    """Reverse-lookup: which market owns this scraper key (registry or deal/signal).

    The single source of truth that lets a writer/orchestrator tag its rows with the
    right market without a per-scraper hardcode. Returns None for unknown keys.
    """
    for market, spec in MARKET_SCRAPERS.items():
        if scraper_key == spec["registry"] or scraper_key in spec["deal_and_signal"]:
            return market
    return None
