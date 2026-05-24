# Phase 2 Backlog — Tranchi Engine

Prioritized work after the Phase 1 build (5 Cuyahoga sources live + read API + dashboard).

## 0. ✅ SHIPPED (2026-05-24) — Upcoming tax-deed + mortgage sales via Daily Legal News
The actionable *upcoming* sales Marc asked for now land via **`dln.py`** (source_site
`"Cuyahoga Sheriff Sale (DLN)"`). DLN is the official county legal-notice journal exposing a
public WordPress REST API (`/wp-json/dln/v1/data-table?type=delinquent-tax | sheriff-sales`) —
no auth, no EULA, robots allow-all, future-dated, both tax + mortgage. This **supersedes** the
court-docket and Realauction plans below. Migration `004` added `auction_status`,
`opening_bid_usd`, `appraised_value_usd`, `sec_sale_date`. `sheriff.py` now preserves
`auction_status` for lead recovery. Cross-source dedup is parcel-aware. See
`Clients/Marc/tranchi/research/dln-field-map.md`.

## 1. Realauction — EVALUATED → DEFERRED (not built)
**Decision:** deferred. DLN (item 0) already provides the same upcoming sales from a clean public
source weeks earlier. Realauction's only marginal value is the *live* current-auction delta
(re-offer dates, ~1–3 last-minute adds, during-sale sold/high-bid status) — not worth its
`robots: Disallow:/` + redistribution-EULA friction. A secure **unauthenticated** scrape is
feasible (no hard blocker found) if ever revived; full mechanics + coverage head-to-head preserved
in **`Clients/Marc/tranchi/research/realauction-findings.md`**. Revisit only if Marc wants live
during-auction tracking.

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
