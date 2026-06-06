# Tranchi Source Catalog — Memphis (Shelby County, TN)

**For Marc to review.** One card per data source. Each says **where it's from**, **what it is**,
**how to verify it yourself**, and a **KEEP?** box. Review in batches and tell us which types to
keep visible — flipping one off is a one-line change (no rebuild). Verified live 2026-06-06.

**The two views in the dashboard:**
- **Buy Now** — actively-acquirable deals (default view). **4,191 active.**
- **Pre-Distress** — earlier-stage off-market leads (motivated owner, not yet listed). **22,081 active.**
- Toggle between them top-left; both filter by market. Total Memphis inventory: **~26,300 active.**

**How to verify ANY listing (one click):** open the listing → the **Verify** row has buttons:
- **Zillow / Redfin** → confirm it's **off-market** (not actively for sale = the distress is real).
- **Shelby Trustee** → the county parcel page (owner, taxes owed, tax-sale notice).
- **Source** → the original record it came from.

---

## BUY NOW sources (acquirable deals)

### ▸ Shelby County Land Bank — 2,165 active   ·   KEEP? ☐
- **From:** Shelby County Land Bank (ePropertyPlus). **What:** county-owned parcels for sale,
  filtered to `FOR SALE` + available. **Valid when:** still on the live inventory (re-checked every 3h).
- **Verify:** Source button → the ePropertyPlus listing; Trustee button → owner = the Land Bank.

### ▸ Shelby County Tax Sale — 1,605 active   ·   KEEP? ☐
- **From:** Shelby County Trustee `TaxSaleExtract.csv`. **What:** the pre-sale tax-deed catalog
  (parcels headed to the county tax auction for unpaid taxes; TN is a *redeemable* deed state).
  **Valid when:** on the current Trustee catalog. **Note:** these are PRE-sale (auction ~Oct 2026).
- **Verify:** Trustee button → shows the parcel's tax balance + "Tax Sale Notice".

### ▸ Shelby Probate Court — 342 active   ·   KEEP? ☐
- **From:** Shelby County CourtConnect (probate estate cases). **What:** an owner died → the estate
  must liquidate → motivated, often below-market. **Valid when:** the case is OPEN (closed/settled
  cases are auto-retired) AND the parcel hasn't transferred to a new owner. Only confirmed/probable
  decedent→parcel matches are shown (weak name-only matches are hidden).
- **Verify:** open the listing → decedent name shown; Trustee button → confirm owner + no recent sale.

### ▸ Shelby County Foreclosure — 69 active   ·   KEEP? ☐
- **From:** tnforeclosurenotices.com + auction.com. **What:** lender-forced trustee sales (Notice of
  Trustee's Sale). **Valid when:** sale date is upcoming (past-date rows expire). TN foreclosures have
  no redemption (terminal). **Verify:** Source button → the foreclosure notice; Zillow → off-market.

### ▸ Memphis MMLBA — 10 active   ·   KEEP? ☐
- **From:** Memphis/Shelby County Land Bank Authority (city). **What:** a small distinct city-held
  inventory. **Verify:** Source button → mmlba.org gallery.

---

## PRE-DISTRESS sources (off-market leads — the "Pre-Distress" toggle)

> These are **not** for-sale listings — they're motivated-owner leads the owner hasn't listed (which
> is exactly the off-market value). Each is a real, registry-resolved Memphis parcel. They inherit
> the same off-market guard as Buy Now (we drop a lead the moment its parcel sells/transfers).

### ▸ Shelby Tax Delinquent (Lead) — 15,999 active   ·   KEEP? ☐
- **From:** Shelby County Trustee *Delinquent Realty Lawsuit List*. **What:** parcels where the owner
  is **being sued for unpaid taxes** (an open tax lien, well past 12 months — the lead type you asked
  for: "$20k owed on a $100k house"). The **$ owed** and **years delinquent** are on each record so
  users can filter. **Valid when:** still on the lawsuit list + parcel not yet sold.
- **Verify:** Trustee button → the exact tax balance + lawsuit; Zillow → off-market.

### ▸ Shelby Eviction (Lead) — 6,082 active   ·   KEEP? ☐
- **From:** Data Midsouth eviction filings. **What:** **tired-landlord** signal (owner burning money
  on a problem tenant → motivated to sell). Slightly weaker off-market rate than tax-distress (a
  landlord sometimes lists after clearing a tenant) — flag if quality disappoints; it's a one-flip
  toggle. **Valid when:** filed within 365 days + parcel not sold. **Verify:** Zillow → off-market.

---

## Behind-the-scenes signals (NOT listings — they make a listing "HOT")
A parcel carrying 2+ distress dimensions is badged **HOT**. Tax-delinquent + eviction stack onto
*any* listing on the same parcel. These run but never appear as standalone rows.

---

## What we are NOT sourcing (and why)
- **Sheriff/judicial sales** — already covered (TN folds them into tax sale + foreclosure + probate).
- **Code violations** — Memphis has no live feed (snapshot maxes Oct 2025; would fail our freshness bar).
- **Pre-foreclosure deeds** — TN has no lis-pendens; earliest reliable signal is the foreclosure notice we already capture.
- **Ring counties (DeSoto, Tipton, Fayette, Marshall)** — fully recon'd + costed, held in reserve.
  Shelby alone already gives 26k. Say the word and we add the metro ring.
