# Verification — how to confirm scrapers are correct

Three-way triangulation per scraper. All three must agree before merging or trusting a production run.

## The three independent checks

### 1. Manual (you, on the live site)
Open the site in a browser, follow the recipe in the per-site doc, count rows + record top-row fields. Your number becomes the ground truth.

### 2. Scraper count (CLI)
```
cd ~/tranchi-engine/backend
python -m app.scrapers.run --site <name> --dry-run
```
Logs `found / passed / active / filtered`. The `found` number should match your manual count (±small drift if the site updates between checks).

### 3. Playwright spot-check (automated diff)
For 2-3 random rows in scraper output, re-navigate to the source via Playwright, capture every field on the live row, diff against the scraper's extraction. Any mismatch → flag in `tranchi.scrape_diffs` for review (Phase C: actual table; for now: console log).

If all three agree → the scraper is trusted. Merge + enable in cron.

---

## Per-scraper manual recipe (the part you do)

Save these — they're how you verify before every Sunday demo (and any time we modify a scraper).

### 1. Land Bank
1. Open https://cuyahogalandbank.org/all-available-properties/
2. Let DataTables fully load
3. Scroll to bottom → footer says "Showing 1 to N of N entries"
4. Note: N (your row count ground truth)
5. Note: top row's parcel# + street
6. **Expected as of 2026-05-23:** 99 rows, top = `140-17-057` / `17425 Eldamere Ave`

### 2. Sheriff Sales
1. Open https://cpdocket.cp.cuyahogacounty.gov/sheriffsearch/search.aspx
2. Sale-date dropdown → pick the most recent **Tax Delinquent** date (suffix `T`)
3. Click Start Search
4. Count rows. Note top case# + defendant + min bid.
5. **Expected for 5/20/2026 Tax Delinquent:** 15 rows, top = `CV15855616` / `3587 East 105th Street` / `WHEATT/TANYA/` / `$59,968.92` / `NO BID FORFEIT TO STATE`

### 3. Code Violations
1. Open https://data.clevelandohio.gov/
2. Search "Building Complaint Violation Notices"
3. Open the dataset page → note record count at top of page
4. **Expected as of 2026-05-23:** 32,697 records (will grow slowly)
5. **Direct API spot-check:** open `https://services3.arcgis.com/dty2kHktVXHrqO8i/arcgis/rest/services/Complaint_Violation_Notices/FeatureServer/0/query?where=OBJECTID=1&outFields=*&f=json` in browser — confirm scraper stored same fields for OBJECTID=1

### 4. Fiscal Officer (MyPlace)
1. Open https://myplace.cuyahogacounty.gov/
2. Click **Owner** radio
3. City dropdown stays on **Entire County**
4. Type `SMITH` → search
5. Result panel: ~998-1000 hits, "N records found" message
6. **Expected as of 2026-05-23:** top parcel = `022-13-042` / `FOUSEK, JASON & SMITH, LINDSAY` / `3867 W 134 ST, CLEVELAND OH 44111`
7. Click that parcel → Tax By Year tab → fields: Property Class = R, Land Use = 5100, School District = CLEVELAND CSD

### 5. Probate Court
1. Open https://probate.cuyahogacounty.gov/pa/
2. Click **Yes** on the agreement page
3. Click **Search By Party** in nav
4. Year = 2026, Case Category = ESTATE, Party Role = Decedent
5. Leave Last Name blank, click Search
6. Note: top case# (format `2026EST...`), decedent name
7. Click into CaseSummary → note case#, decedent + DOD, executor, attorney
8. **Cross-check the parcel join:** copy decedent name, paste into MyPlace Owner search — scraper should have produced a `RawListing` per parcel found

---

## What "passes" means

A scraper passes verification when:
- ✅ Manual count and scraper `found` count agree (±5%)
- ✅ Top-row spot-check fields match (allowing for canonicalization differences: title-case vs ALL CAPS, slash-stripped names, hyphen vs no-hyphen parcel formats)
- ✅ Playwright spot-check on 2-3 random rows shows zero unaccounted-for field mismatches

If any of these fail → don't merge. Open the scraper, find the issue, retest.

---

## Common false-positive failures (don't panic)

- **Row count drift of 1-3** between manual check and scraper run → site updated in the gap. Acceptable.
- **Address casing differences** (`"105Th"` vs `"105th"`) → canonicalization runs after the scraper; raw output won't match exactly. Confirm canonicalized address matches instead.
- **Defendant slash format** (`WHEATT/TANYA/` vs `WHEATT TANYA`) → scraper strips slashes intentionally for storage. Match on lastname+firstname.
- **Owner name `&` vs `AND`** → fiscal officer stores `&`, normalization may change downstream. Match on logical equivalence.

## What's a real failure (panic)

- **Row count off by >5%** → likely a filter/pagination bug
- **Top-row fields don't match logically** (different parcel, different defendant) → likely indexing bug
- **Scraper errored out** → check `logs/<site>.log` and the `tranchi.scrape_runs` row for `error_message`
- **Counts agree but a field is consistently wrong** (e.g. all sale_dates one day off) → likely timezone bug (check `_time.py` usage)

---

## Production monitoring (Phase C)

Each scraper run writes a `tranchi.scrape_runs` row with `found / passed / active / filtered / dupes / delisted / expired / new_today` — the same shape as Gotham's Sources dashboard cards. Sudden drops in `found` count (e.g. drops to 0 unexpectedly) should fire an alert.

Recommended alarms (Phase C, not yet wired):
- `found = 0` for any source in 2 consecutive runs → alert
- `found` drops >50% week-over-week → warn
- Any scraper with `error_message != NULL` for 2 consecutive runs → alert
- Cross-source dedup `dupes` column trending up → may indicate source republishing
