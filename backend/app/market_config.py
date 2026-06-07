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

CURRENT STATE (2026-06-06): this is the FOUNDATION seed. It holds the verification/market config
that `scripts/verify_listings.py` already proved out (moved here verbatim — zero behavior change).
The remaining call-sites (`run.py`, `staleness.py`, `prefilter.py`, `db.py`,
`playwright_source_check.py`, `enrich_*`, routers) still hold their own per-market branches; the
next focused backend session folds those into this module (see the handoff doc's call-site map) and
adds a market registry so audit/verify/enrich iterate all markets automatically.

TO ADD A MARKET (today): add a `_make_<market>_market()` + a `MARKETS` entry here, then wire the
remaining call-sites per `Clients/Marc/scraper-playbook/ADD-A-MARKET.md`. Post-refactor: just the
config + the scrapers.
"""
from __future__ import annotations

import urllib.parse


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
    }

    return {
        "db": "tranchi",
        "state": "OH",
        "state_filter": "l.property_state = 'OH'",
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
        "state_filter": "l.property_state = 'TN'",
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


# The market registry. To add a market: implement _make_<market>_market() and add it here.
MARKETS: dict[str, dict] = {
    "cuyahoga": _make_cuyahoga_market(),
    "shelby": _make_shelby_market(),
}
