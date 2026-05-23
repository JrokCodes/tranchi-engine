# Cuyahoga Sheriff Sales

## Source

- **URL:** https://cpdocket.cp.cuyahogacounty.gov/sheriffsearch/search.aspx
- **Owner:** Cuyahoga County Sheriff's Office (via Clerk of Courts technology stack)
- **Vendor:** ProWare 1.0.55 (ASP.NET WebForms)
- **Record status:** Public court record; Ohio Rev. Code 149.43
- **ToS posture:** No T&C splash gate on this specific search page. Parent domain (`cpdocket.cp.cuyahogacounty.gov`) has a "not intended as a source for bulk downloads" TOS but it's milder than probate's explicit anti-mining clause. Posture: respect rate limits, generic UA, don't bulk-historical-pull.
- **Last verified:** 2026-05-23 (Playwright probe + scraper run)

## Data shape

Search modes:
- **By Date** (we use this) — pick a sale date from a dropdown, get all rows for that date
- **By Criteria** — case#, street, defendant, zip, parcel#, date range (we don't use this — date mode is sufficient and cleaner)

> ⚠️ **"IF YOU SEARCH BY DATE, ALL OTHER CRITERIA ARE IGNORED"** — per the UI itself. Use date-only.

**Date dropdown shows only PAST dates** (18 historical Tax Delinquent dates as of 2026-05-23, oldest 2025-11-26, newest 2026-05-20). **Upcoming sales are NOT here** — they live at `cuyahoga.sheriffsaleauction.ohio.gov` (Realauction vendor, auth-walled — Phase 2).

**Dropdown value format:** `M/D/YYYY 12:00:00 AM<suffix>` where:
- `T` suffix = Tax Delinquent
- `N` suffix = Non-Tax (we filter these out)

**URL-encoded `foreclosureType` filter:** `'TAX'` and `'TAXB'` (Tax Delinquent variants)

**Result table fields (per row):**

| Field | Example |
|---|---|
| Case # | `CV15855616` |
| Sale Date | `2026-05-20` |
| Parcel # | `135-08-080` |
| Address | `3587 East 105Th Street` (note Python `.title()` quirk: "105Th" not "105th"; canonicalizer fixes) |
| Defendant | `WHEATT TANYA` (slashes stripped, e.g. raw `WHEATT/TANYA/` → display) |
| Appraisal | `$89,953.38` |
| Min Bid | `$59,968.92` (typically 2/3 of appraisal in Ohio) |
| Status | `NO BID FORFEIT TO STATE`, `WITHDRAWN`, `SOLD`, etc. |

**Typical date returns 15-200 rows in single page** (no pagination needed in date mode).

**Detail pages exist (`detail.aspx`) but carry NO query string** — they're session-bound POST state. Direct linking breaks. Scraper skips detail pages; enough fields exist in the result table for Phase 1.

## Scrape approach

- **Tool stack:** `httpx` + `BeautifulSoup`, via shared `proware_client.py` (handles `__VIEWSTATE`, `__EVENTVALIDATION`, `__EVENTTARGET`)
- **Pagination:** None in date mode
- **Anti-bot:** None visible. ProWare has weak rate limiting in practice but is rate-limit-friendly at 1 req/sec.
- **Date strategy:** Pull last 30 days of Tax Delinquent past sales by default (typically 3-4 dates). Backfill mode pulls all 18 historical dates via `SHERIFF_BACKFILL=1` env var.
- **DOM parsing:** Uses ProWare span ID suffix matching (`lblOpeningBid`, `lblDefendant`, `lblAddress`, etc.) rather than fragile label-text scanning. **This pattern is reusable across all ProWare deployments** (Hamilton, Franklin, Summit counties also use ProWare).

## Output destination

- **Table:** `tranchi.listings`
- **`source_site`:** `sheriff_sales`
- **`signal_type`:** `tax_delinquent_foreclosure`
- **`case_number`:** court case number (e.g. `CV15855616`) — primary dedup key
- **`source_listing_id`:** parcel number
- **Cross-source join key:** parcel number (display format `DDD-NN-NNN`)
- **`status`:** mapped from sheriff status text — `expired` for completed past sales (NO BID, WITHDRAWN, SOLD), `active` only if the sale is upcoming/in-progress (won't happen with current cpdocket dropdown which is past-only)

## Verification recipe

1. Open https://cpdocket.cp.cuyahogacounty.gov/sheriffsearch/search.aspx
2. Sale date dropdown → pick a recent **Tax Delinquent** date (suffix `T`) — e.g. **5/20/2026 Tax Delinquent**
3. Click Start Search
4. Count rows — usually 15-25 per date
5. As of 2026-05-23 for 5/20/2026 Tax Delinquent: **15 rows**, top case `CV15855616` at `3587 East 105th Street`, defendant `WHEATT/TANYA/`, min bid `$59,968.92`, status `NO BID FORFEIT TO STATE`
6. **Random spot-check:** pick any row, copy case#, find it in scraper output, fields must match (allowing for `.title()` casing differences and slash stripping in defendant names)

## Known issues / gotchas

- **All current rows show `expired`** — correct. Dropdown is past-only; sales are completed.
- **`.title()` casing artifact** in address ("105Th" not "105th") — db.py canonicalizer normalizes downstream, no action needed in scraper.
- **Defendant slash format** — raw form `WHEATT/TANYA/` (LAST/FIRST/MIDDLE), stripped to space-separated for storage. If you need to do the fuzzy join back to MyPlace owner search later, normalize both to same format.
- **Detail-page enrichment requires session traversal** — not done in Phase 1. If we add it, route through `proware_client` so the session cookie persists across POST-backs.
- **Future sales need Realauction.ohio.gov scraper** (Phase 2) — auth-walled, needs separate handling.
- **Backfill mode (`SHERIFF_BACKFILL=1`)** pulls all 18 historical Tax Delinquent dates → ~250-350 rows total. Useful for one-time initial population; never run on cron.

## File pointers

- **Scraper:** `backend/app/scrapers/sheriff.py` (259 lines)
- **Shared helper:** `backend/app/scrapers/proware_client.py` (ASP.NET ViewState)
- **Reference template:** Gotham `mtglaw.py` (Power BI POST) + `orlans.py` (cookie/session)
- **Tests:** `tests/scrapers/test_sheriff.py` (to be written)
- **Field-map appendix:** `Clients/Marc/tranchi/research/cuyahoga-field-map.md` § B
- **Screenshots:** `Clients/Marc/tranchi/research/sheriff-dropdown.png`, `sheriff-results.png`
