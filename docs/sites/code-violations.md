# Cleveland Code Violations

## Source

- **URL (portal):** https://data.clevelandohio.gov/
- **URL (FeatureServer):** https://services3.arcgis.com/dty2kHktVXHrqO8i/arcgis/rest/services/Complaint_Violation_Notices/FeatureServer/0
- **Owner:** City of Cleveland (Building & Housing dept), exposed via Cleveland GIS open-data hub
- **Vendor:** Esri ArcGIS Online (Feature Service REST)
- **Org ID:** `dty2kHktVXHrqO8i` (the "opendataCLE" org)
- **Record status:** Fully public open data (CC-BY or Open Data Commons license — explicitly meant for programmatic reuse)
- **ToS posture:** None. ArcGIS REST is designed to be consumed programmatically. Zero friction.
- **Last verified:** 2026-05-23 (Playwright probe + scraper run, 32,697 records)

## Data shape

**Why this is a *signal* not a *listing*:** A code violation tags an existing property; it doesn't create a new property surface. A property with 3+ open violations is statistically much more likely to sell within 6-12 months at a discount — but the violation itself isn't a deal-listing.

**Two ArcGIS datasets exist:**

1. **Building Complaint Violation Notices** (~32,697 rows) — one row per complaint that resulted in a Violation Notice from Building & Housing since 2015. **This is our primary source.**
2. **Building Violation Status History** (~485,508 rows) — one row per workflow task step per violation. Same violation generates 4-6 status-change rows. **Opt-in via `include_status_history=True`** if we ever want lifecycle data.

**Fields exposed (per Notice row):**

| Field | Example |
|---|---|
| OBJECTID | int auto-incrementing |
| Parcel # | `11019068` (8-digit compact format, NO hyphens) |
| Address | `11213 HOPKINS AVE` |
| Violation type code | e.g. `EXT-MAINT`, `STRUCT-UNSAFE`, `OPEN-DUMPING` |
| Violation description | free text |
| Status | `Open`, `Closed`, `Pending Compliance` |
| Open date | `2016-04-15` |
| Close date | nullable |
| Lat / Lng | for geometry |

**Update cadence:** New violations posted as Building & Housing logs them — typically dozens per day, hundreds per week.

## Scrape approach

- **Tool stack:** `httpx` + JSON, via shared `arcgis_client.py` helper
- **Pagination:** ArcGIS FeatureServer `resultOffset` in batches of **1000** (NOT 2000 — both Cleveland layers have `maxRecordCount=1000` and 2000 silently truncates)
- **Anti-bot:** None. Esri's hosted services are designed for load.
- **Discovery:** FeatureServer URL discovered via `opendata.arcgis.com/api/v3/datasets?q=Building+Violation+Cleveland` — owned by `opendataCLE` (org ID `dty2kHktVXHrqO8i`). Both Cleveland datasets live under the same org root.

**Backfill vs delta:**
- **First run (backfill):** `where=1=1` to pull everything since 2015. ~32K records, ~33 paginated requests, takes a few minutes.
- **Subsequent runs (delta):** `where=FILE_DATE >= DATE '<last_run_date - 1 day>'`. Orchestrator passes `last_run_date` from the most recent `tranchi.scrape_runs` row with status `success`.

## Output destination

- **Table:** `tranchi.signals`
- **`signal_type`:** `code_violation`
- **`source`:** `cleveland_open_data`
- **`parcel_number`:** 8-digit compact form as stored by source (e.g. `11019068`) — see "cross-source gotcha" below
- **`observed_at`:** the violation's `open_date`
- **`confidence`:** `1.0` (official record)
- **`payload`** (JSONB): violation_type, violation_description, status, open_date, close_date, address, lat/lng

**Foreign-key pattern:** Before each signal insert, a stub row is upserted into `tranchi.parcels (parcel_number)` with `ON CONFLICT DO NOTHING`. This satisfies the FK; the full parcel record gets filled later by `fiscal_officer.py` when (or if) that parcel ever gets queried.

## Verification recipe

1. Open https://data.clevelandohio.gov/
2. Search "Building Complaint Violation Notices"
3. Open the dataset → check the record count displayed near the top of the page (or open the table view)
4. As of 2026-05-23: scraper reports **32,697** rows. Expect minor daily increases.
5. **Spot-check via direct API:** open `https://services3.arcgis.com/dty2kHktVXHrqO8i/arcgis/rest/services/Complaint_Violation_Notices/FeatureServer/0/query?where=OBJECTID=1&outFields=*&f=json` in a browser — confirm the JSON response matches what the scraper stored for OBJECTID=1.

## Known issues / gotchas

- ⚠️ **Parcel-number format mismatch** — Cleveland Open Data stores parcels as **8-digit compact** (`11019068`), but Sheriff Sales and Fiscal Officer use **display format** (`110-19-068`). Both refer to the same parcel. The canonicalization helper in `db.py` (or a new `normalize_parcel_number()` helper) must convert both to display format before cross-source signal-to-listing joins, or this dataset's signals won't tie to anything.
- ⚠️ **Missing unique index on `tranchi.signals`** (Phase C TODO) — `upsert_signals` uses `ON CONFLICT (parcel_number, signal_type, source, (observed_at::date))` but migration `001_tranchi_schema.py` doesn't create the matching unique index. Until added, re-runs INSERT duplicates. Add via:
  ```sql
  CREATE UNIQUE INDEX IF NOT EXISTS uq_tranchi_signals_natural_key
      ON tranchi.signals (parcel_number, signal_type, source, (observed_at::date));
  ```
- **Batch size 1000 enforced** — passes `batch_size=1000` explicitly. The shared `arcgis_client.py` default of 2000 would silently truncate Cleveland pages to 1000.
- **Delta-pull wiring incomplete** — orchestrator must read `tranchi.scrape_runs.started_at` for last successful run and pass to the scraper constructor. Phase C work.
- **Status History (485K rows) opt-in only** — sufficient signal stacking from Notices alone. History would just generate noise from workflow-step rows.

## File pointers

- **Scraper:** `backend/app/scrapers/code_violations.py` (422 lines)
- **Shared helper:** `backend/app/scrapers/arcgis_client.py`
- **New abstract class:** `SignalScraper` added to `backend/app/scrapers/base.py`
- **Tests:** `tests/scrapers/test_code_violations.py` (to be written)
- **Field-map appendix:** `Clients/Marc/tranchi/research/cuyahoga-field-map.md` § C
- **Screenshots:** (no screenshot — pure API)
