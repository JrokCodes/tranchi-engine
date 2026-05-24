# Phase 2 Backlog — Tranchi Engine

Prioritized work after the Phase 1 build (5 Cuyahoga sources live + read API + dashboard). Top item is the **tax-deed expansion**, which Jayden plans to attempt next.

## 1. (PRIORITY) Realauction — upcoming tax-deed / sheriff auctions  🔒 login-walled
**Why:** The current `sheriff.py` scraper only sees the **last ~30 days of *past*** tax-delinquent foreclosure sales (the ProWare docket dropdown has no future dates), so they land as `status=expired`. The actionable, *upcoming* tax-deed auctions — exactly the "tax deed records" Marc asked for — live on **Realauction** (`cuyahoga.sheriffsaleauction.ohio.gov` / `realauction.com`).
**The catch:** Realauction is behind a **login wall** (free account registration, sometimes a deposit gate to bid, but the auction *calendar + property list* is usually viewable post-login). Needs an auth/session strategy:
- Register a Tranchi account; store creds in `/home/ubuntu/.secrets/tranchi/` (mode 600).
- Establish an authenticated session (cookie jar) before scraping the auction calendar + per-property detail.
- Likely needs Playwright (JS-driven portal) rather than plain httpx — accept the heavier footprint here.
- Respect the site: low request rate, generic UA, no bidding actions — read-only.
**Output:** `tranchi.listings`, `signal_type="tax_delinquent_foreclosure"`, `status=active` with a real future `sale_date`.

## 2. Sheriff backfill (quick win, no new code)
`sheriff.py` already supports `SHERIFF_BACKFILL=1` (env) / `backfill=True` to pull **all ~18 historical tax-delinquent sale dates** in one run instead of the 30-day lookback. Run once to thicken historical coverage. Trade-off: they're past sales (expired) — useful as records, not live deals.

## 3. Land Bank inventory completeness check
We currently pull **99** properties from `https://cuyahogalandbank.org/all-available-properties/`, but an earlier field-map probe expected **~144**. Verify whether the list is paginated or split by category/status and we're only getting the first page/category. Likely a small fix in `landbank.py`.

## 4. Fiscal Officer tax-flag enrichment → real tax-distress signals
`fiscal_officer.py` has a `enrich_detail=True` path that reads MyPlace "Tax By Year" / distress flags (foreclosure, cert pending, cert sold, payment plan, delinquent) and writes them to `tranchi.signals`. It's **skipped in the bulk cron** (too slow at full scale; needs Playwright for the XHR). Run it as a **targeted job** over the distressed subset so tax-delinquency becomes a real stacked **"Tax Distress"** signal dimension (the dashboard already maps these signal types — they just aren't populated yet).

## 5. Probate signal-insertion completeness (low priority)
Probate emits ~2,100 listings but its per-parcel `probate` *signal* only lands when the matched parcel already exists in `tranchi.parcels` (FK-gated; otherwise skipped). Moot for the dashboard now that each listing's own dimension is derived at API read-time, but worth tightening (upsert the matched parcel before the signal insert) so the `signals` table is complete for analytics.

## Already spec'd (from docs/README source taxonomy, not started)
Common Pleas lis pendens, Cleveland Vacant Property Registry, Cleveland Municipal Court evictions, HUD Home Store REO, Cuyahoga Annual Delinquent Tax PDF. County expansion: Summit / Lake / Lorain / Medina / Geauga (same code, different county filter).
