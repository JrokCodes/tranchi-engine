# Cuyahoga Fiscal Officer (MyPlace)

## Source

- **URL:** https://myplace.cuyahogacounty.gov/
- **Owner:** Cuyahoga County Fiscal Officer (real-property division)
- **Stack:** Server-rendered ASP.NET app (initial probe mislabeled this as React SPA — see "architecture discovery" below)
- **Record status:** Public parcel database; Ohio open-records
- **ToS posture:** None. Standard public-records disclaimer ("as-is, no accuracy guarantee"). No anti-scraping language.
- **Last verified:** 2026-05-23 (Playwright probe + scraper run, 999 parcels for letter 'S')

## Why this is the identity spine

Every other source in this engine references either an owner name or a parcel address. The Fiscal Officer DB is the **only authoritative way** to resolve those into a parcel ID + current ownership + market value + tax balance. Without this, the system can only show isolated events — it can't say "Property at 123 Main St has 3 different motivated-seller signals." All cross-source signal stacking depends on this table populating `tranchi.parcels`.

Specifically: **`probate.py` calls `fiscal_officer.search_by_owner(decedent_name)`** to find parcels owned by a deceased person. That's the entire probate→property bridge.

## Data shape

**Two callable surfaces:**

1. **Bulk-pull scraper** (`FiscalOfficerScraper.fetch_and_parse()`) — A-Z owner-name sweep, ~999 parcels per letter (FeatureServer cap), ~20,800 unique parcels total
2. **On-demand owner-search** (`async def search_by_owner(name) -> list[ParcelMatch]`) — high-recall fuzzy lookup with confidence score, called by probate.py

**Search modes (form values):**
- `Owner` — owner-name search (we use this)
- `Parcel` — direct parcel lookup
- `Address` — street search

**Municipality dropdown:** 58 cities + "Entire County" (code `99`). Use `99` for cross-municipality searches.

**Fields per parcel (extracted by scraper):**

| Field | Example |
|---|---|
| Parcel # | `022-13-042` (display format) |
| Owner name | `FOUSEK, JASON & SMITH, LINDSAY` |
| Owner mailing address | (separate from situs) |
| Situs address | `3867 W 134 ST` |
| City / ZIP | `Cleveland, OH 44111` |
| Property class | `R` (residential), `C` (commercial), etc. |
| Land use code | `5100` (1-family platted lot) |
| Land use description | `1-FAMILY PLATTED LOT` |
| Zoning | `TWO FAMILY` |
| Neighborhood code | `02150` |
| School district | `CLEVELAND CSD` |
| Acreage, year built, sq ft, beds, baths | from detail page POST |
| Last sale date + price | from detail page POST |
| Current market value, taxable value | from detail page POST |

**Distress flags (Tax By Year tab — requires Playwright):**
- `Foreclosure` (boolean)
- `Cert. Pending` (boolean)
- `Cert. Sold` (boolean)
- `Payment Plan` (boolean)
- `Balance Due` (dollar amount)

**These distress flags are the gold for cross-source signal stacking.** When a parcel has any flag = True, it writes a row to `tranchi.signals` with `signal_type='fiscal_distress'`.

## Scrape approach

**Two-track architecture:**

### Track A: Bulk parcel sweep (cron, every 3h)
- **Tool stack:** Pure `httpx` + `BeautifulSoup`
- **Strategy:** A-Z owner-name sweep, ~999 hits per letter
- **Detail enrichment:** off by default (would be ~6+ hours per cron run for 26K POSTs). Enable selectively for parcels that appear in OTHER sources (sheriff, probate) — those are the ones we care about.
- **Tax-flag enrichment:** off by default (requires Playwright). Enable via `enrich_tax=True` for parcels we want full distress signals on.
- **Incremental mode:** `FiscalOfficerScraper(sweep_letters=["A","B","C"])` to spread the sweep across multiple cron ticks.

### Track B: On-demand owner-search (called by probate.py)
- **Tool stack:** Pure `httpx` + `BeautifulSoup`
- **Signature:** `async def search_by_owner(name: str, fuzzy: bool = True) -> list[ParcelMatch]`
- **Returns:** `list[ParcelMatch]` with `parcel_number, owner_name, situs_address, confidence (0-1), ambiguous (bool), full_record (dict)`
- **High-recall mode:** include all candidates above 0.75 confidence with `ambiguous=True` if multiple. Per plan: better to over-include than miss leads.

**Anti-bot:** None. Server-rendered HTML, no protection layer.

**Rate limiting:** Generic 0.5-1.5s jitter inherited from shared helpers. No special rule.

## Output destination

### Bulk sweep
- **Primary table:** `tranchi.parcels` (PK on `parcel_number`)
- **Side effect:** If `enrich_tax=True` and any distress flag is True → row in `tranchi.signals` with `signal_type='fiscal_distress'`

### Owner search (callable)
- Returns `list[ParcelMatch]` directly to caller (probate.py); does NOT write to DB itself

## Verification recipe

1. Open https://myplace.cuyahogacounty.gov/
2. Click **Owner** radio button
3. City dropdown: leave on **Entire County** (code 99)
4. Type `SMITH` and search
5. Result panel: ~998-1000 hits, message reads "N records found"
6. As of 2026-05-23, top parcel = `022-13-042` owned by `FOUSEK, JASON & SMITH, LINDSAY` at `3867 W 134 ST, CLEVELAND OH 44111`
7. Click that parcel → Tax By Year tab → fields: Property Class = R, Land Use = 5100, School District = CLEVELAND CSD

## Architecture discovery (field-map correction)

The Playwright probe initially classified MyPlace as "React SPA requiring Playwright for all scraping." This was wrong.

**Reality:** The search-results page is **1.4MB of server-rendered HTML** in the initial GET. The 998 parcel records, the `AddressInfo` block per parcel (7 items each), and the hit-list pagination are all in raw HTML — no JS execution needed. Pure `httpx` + `BeautifulSoup` works.

**Detail-page POST:** The parcel detail page (`/MainPage/PropertyData`) requires a POST with 18 hidden form fields derived from the hit-list HTML. The form is in a `<form id="propertyForm">` element on the hit-list page. Extract all hidden inputs, POST them, get the rendered detail HTML back. Still pure httpx.

**Where Playwright is needed:** Only the **Tax By Year tab's distress flags** (Foreclosure / Cert. Pending / Cert. Sold / Payment Plan / Balance Due) load via a secondary browser-side XHR after the initial detail page renders. Replicating that XHR with raw httpx is more work than just running Playwright for the parcels we care about. So:
- Bulk sweep + detail page: pure httpx (fast, no browser overhead)
- Tax-flag enrichment: Playwright, opt-in (slow, run selectively)

**Lesson for future scrapers:** Always probe the actual initial HTML payload before assuming a site needs Playwright. "Site has React in the markup" ≠ "all data requires JS execution."

## Known issues / gotchas

- **Bulk sweep is ~999 per letter** — FeatureServer truncates at 1000. If a letter has 1500 SMITH variants, we miss 500. Mitigation: layer in a second sweep with secondary filters (city dropdown values) to break up the larger letters.
- **Detail enrichment is expensive** — 26K POSTs ≈ 6+ hours. **Don't enable on cron.** Strategy: only detail-enrich parcels referenced by OTHER sources (sheriff sale defendants, probate decedents, code-violation addresses).
- **Owner-name format varies** — `FOUSEK, JASON & SMITH, LINDSAY` (multi-owner with `&`), `SMITH NARISHA` (single, no comma), `MCGHEE, MONIQUE A & SMITH, DKWON V` (multi with middle initials). Fuzzy matching in `search_by_owner` handles this but confidence scores will vary.
- **Playwright dependency added** — `pip install playwright && playwright install chromium`. Non-fatal if not installed (tax-flag enrichment just degrades to None).
- **Cross-source parcel format:** stores `DDD-NN-NNN` (display format). Code-violations scraper stores compact 8-digit. Cross-source join requires normalization (see code-violations.md and `db.py` canonicalization).

## File pointers

- **Scraper:** `backend/app/scrapers/fiscal_officer.py` (1,139 lines — biggest scraper because of two-track architecture)
- **Tests:** `tests/scrapers/test_fiscal_officer.py` (to be written)
- **Field-map appendix:** `Clients/Marc/tranchi/research/cuyahoga-field-map.md` § C
- **Screenshots:** `Clients/Marc/tranchi/research/myplace-landing.png`, `myplace-detail.png`
- **Probate-callable interface:**
  ```python
  from app.scrapers.fiscal_officer import search_by_owner, ParcelMatch
  matches: list[ParcelMatch] = await search_by_owner("ANNETTE SMITH")
  ```
