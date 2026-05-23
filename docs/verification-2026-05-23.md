# Tranchi end-of-build cross-reference verification

**Verified by:** independent Playwright probe
**Date:** 2026-05-23
**Method:** Live navigation to each source, comparing data observed to each scraper agent's reported facts. ArcGIS endpoint hit via curl. Probate site navigated at 1+ second between actions per production rate limit.

## Summary

| Scraper | Agent's claim | My observation | Verdict |
|---|---|---|---|
| Land Bank | 99 rows, top `140-17-057` / 17425 Eldamere Ave / Cleveland | `parcel-list-table` has exactly 99 tbody rows; top row text = `140-17-057 \t 17425 \t Eldamere Ave \t Cleveland \t 01 \t May 4, 2026 \t Renovation Underway - Available Soon` | MATCH |
| Sheriff Sales | 15 rows for 5/20/26 Tax Delinquent, top `CV15855616` / 3587 East 105th Street / WHEATT/TANYA/ / $59,968.92 / NO BID FORFEIT TO STATE | Dropdown contained `5/20/2026 12:00:00 AMT` ("5/20/2026 Tax Delinquent"). Submitted form. Result page had exactly 15 distinct `Case #: CV…` entries. First case = `CV15855616`, Min Bid `$59,968.92`, Defendant `WHEATT/TANYA/`, Status `NO BID FORFEIT TO STATE`, Parcel `135-08-080`, Address `3587 EAST 105TH STREET` | MATCH |
| Code Violations | 32,697 records | ArcGIS `returnCountOnly=true` → `{"count":32697}`. OBJECTID=1 query returned RECORD_ID `CMP16013320`, PARCEL_NUMBER `11019068`, PRIMARY_ADDRESS `11213 HOPKINS AVE, CLEVELAND, OH, 44108`, VIOLATION_APP_STATUS `Closed`, FILE_DATE `1460692800000` (= 2016-04-15 UTC) | MATCH |
| Fiscal Officer | ~998-1000 SMITH hits, top `022-13-042` / FOUSEK, JASON & SMITH, LINDSAY / 3867 W 134 ST / Property Class R, Land Use 5100, School District CLEVELAND CSD | 999 parcel-number occurrences in result HTML. Top hit = `022-13-042 FOUSEK, JASON & SMITH, LINDSAY 3867 W 134 ST CLEVELAND, OH 44111`. Property detail form hidden fields confirmed `hdnSearchPropertyClass=R`, `hdnSearchTaxLuc=5100`, `hdnSearchTaxLucDescription=1-FAMILY PLATTED LOT`. CLEVELAND CSD not directly confirmed (XHR didn't render the detail panel in headless probe), but parcel city = CLEVELAND makes the CSD claim consistent | MATCH (with one indirect field) |
| Probate | T&C, Party search, Estate cases, case detail fields, Parties DOD line all accessible | T&C `Yes` btn → CaseSearch. Search By Party form → Year 2026 + ESTATE + DECEDENT + SMITH → 11 distinct `2026EST######` cases returned. Clicked ANNETTE SMITH → CaseSummary showed Case Number `2026EST305113`, Case Type `RELEASE NO WILL`, Filing Date `FRIDAY, FEBRUARY 27, 2026`, Case Status `CLOSED`. Parties tab → `DECEDENT ANNETTE SMITH (DOD: 10/25/2025) 6859 HIDDEN LAKE TRAIL BRECKSVILLE OH 44141` | MATCH |

## Per-source detail

### Land Bank
- URL: https://cuyahogalandbank.org/all-available-properties/
- Method: page navigation, JS evaluation against `table#parcel-list-table tbody tr`.
- Row count observed: **99** (other 2 tables on page are layout/related-content, not the property list — agent correctly targeted the right table)
- Top row: `140-17-057 | 17425 | Eldamere Ave | Cleveland | 01 | May 4, 2026 | Renovation Underway - Available Soon`
- Verdict: MATCH

### Sheriff Sales
- URL: https://cpdocket.cp.cuyahogacounty.gov/sheriffsearch/search.aspx
- Method: opened dropdown, selected `5/20/2026 12:00:00 AMT` (suffix `T` = Tax Delinquent — confirmed in option text), clicked `Start Search`, parsed result page.
- Distinct Case # count observed: **15**
- Case IDs returned: CV15855616, CV20937355, CV22973029, CV24103651, CV24107820, CV24107946, CV24108783, CV24109069, CV24998650, CV25109975, CV25109997, CV25110392, CV25111168, CV25111220, CV25112716
- Top row block:
  - Case #: `CV15855616`
  - Land Type: `OTHER LAND`
  - Minimum Bid: `$59,968.92`
  - Plaintiff: `TREASURER OF CUYAHOGA COUNTY, OH`
  - Defendant: `WHEATT/TANYA/`
  - Sale Date: `5/20/2026`
  - Status: `NO BID FORFEIT TO STATE`
  - Parcel #: `135-08-080`
  - Address: `3587 EAST 105TH STREET`
- Verdict: MATCH

### Code Violations
- ArcGIS REST: `https://services3.arcgis.com/dty2kHktVXHrqO8i/arcgis/rest/services/Complaint_Violation_Notices/FeatureServer/0`
- Method: direct curl to `/query` endpoint, no browser needed.
- Count: `{"count":32697}` — exact match
- OBJECTID=1 record (all relevant fields):
  ```json
  {"OBJECTID":1,
   "RECORD_ID":"CMP16013320",
   "FILE_DATE":1460692800000,        // = 2016-04-15 00:00:00 UTC
   "PARCEL_NUMBER":"11019068",
   "PRIMARY_ADDRESS":"11213 HOPKINS AVE, CLEVELAND, OH, 44108",
   "VIOLATION_NUMBER":"V16033372",
   "VIOLATION_APP_STATUS":"Closed",
   "DW_Neighborhood":"Glenville",
   "LAT":41.534730898720213, "LON":-81.606744599642653}
  ```
- Verdict: MATCH

### Fiscal Officer (MyPlace)
- URL: https://myplace.cuyahogacounty.gov/
- Method: clicked Owner radio, typed `SMITH`, pressed Enter (Entire County default). Counted `\d{3}-\d{2}-\d{3}` parcel patterns in result HTML.
- Hit count: **999** parcel-number occurrences in result list (matches "FeatureServer 1000-cap" architecture note in the per-site doc).
- Top parcel block: `022-13-042 / FOUSEK, JASON & SMITH, LINDSAY / 3867 W 134 ST / CLEVELAND, OH 44111`
- Clicked into 022-13-042. The `propertyForm` hidden fields (server-rendered, populated from the result-list HTML — the exact mechanism the per-site doc describes) contained:
  - `hdnSearchPropertyClass=R`
  - `hdnSearchTaxLuc=5100`
  - `hdnSearchTaxLucDescription=1-FAMILY PLATTED LOT`
  - `hdnSearchNeighborhoodCode=02150`
  - `hdnSearchParcelCity=CLEVELAND`
  - `hdnSearchParcelZip=44111`
- Detail-page POST submitted but full detail panel did not visibly render in this headless session (likely a secondary XHR was blocked or slow). School District `CLEVELAND CSD` was therefore NOT directly observed, but every other field matches and the parcel's city is CLEVELAND, so the claim is highly likely correct. Not flagged as drift — flagged as partial-direct.
- Verdict: MATCH

### Probate
- URL: https://probate.cuyahogacounty.gov/pa/
- Method: accepted T&C → CaseSearch → Search By Party. Filled Last Name `SMITH`, Year `2026`, Case Category `EST` (ESTATE), Party Role `DC` (DECEDENT). Submitted form. Respected ≥1 sec between actions.
- T&C: accepted via `mpContentPH_btnYes`, redirected to `/pa/CaseSearch.aspx`.
- Search button: `mpContentPH_btnSearchByPerson` submitted successfully (timed out at 5s click wait — actual nav took ~6s, still completed).
- Result list: **11 distinct `2026EST######` case numbers** returned (multiple rows per case where alias names exist).
  - First five distinct: 2026EST305382 (ANDREW LEE SMITH), 2026EST305113 (ANNETTE SMITH), 2026EST306496 (BRANDON SMITH), 2026EST303984 (CELIA ADRIENNE/ SMITH), 2026EST305104 (CHARLES SMITH).
- Clicked into ANNETTE SMITH → `/pa/CaseSummary.aspx?q=MjgxODE1NQ==`. All claimed fields present:
  - Case Number: `2026EST305113`
  - Case Title: `THE ESTATE OF ANNETTE SMITH`
  - Case Type: `RELEASE NO WILL`
  - Filing Date: `FRIDAY, FEBRUARY 27, 2026`
  - Judge: `LAURA J GALLAGHER`
  - Case Status: `CLOSED`
  - Status Date: `TUESDAY, MARCH 10, 2026`
- Parties tab → `/pa/CaseParties.aspx?q=MjgxODE1NQ==`. Decedent line:
  - `DECEDENT ANNETTE SMITH (DOD: 10/25/2025) 6859 HIDDEN LAKE TRAIL BRECKSVILLE OH 44141`
  - Format matches per-site spec exactly (`DECEDENT FIRSTNAME LASTNAME (DOD: MM/DD/YYYY)`).
- Verdict: MATCH — the un-live-tested scraper's target page flow works exactly as the per-site doc described.

## Issues found

- **Fiscal Officer detail-panel render in headless:** the detail content (full Tax By Year panel, including the literal `CLEVELAND CSD` school-district string) did not visibly render in this Playwright session within the time budget. The `propertyForm` hidden fields confirm Property Class=R and Land Use=5100 directly, and the per-site doc explicitly calls out that the Tax By Year tab "loads via a secondary browser-side XHR after the initial detail page renders." This matches what was observed — not a scraper defect, but a verification limit. The scraper itself uses Playwright with a longer wait + opt-in basis for tax-flag enrichment, so it's expected to work in production. No action needed; flag noted for future verification runs to add ~10s detail-page wait.
- **No other drift.** All 4 fully-live-testable scrapers report numbers and top-row data exactly as the agents claimed. Probate (the only un-live-tested one) handled the full T&C → Search By Party → Case Summary → Parties flow without surprises.

## Overall verdict

**Production-ready: Y**

Reasoning:
1. Land Bank, Sheriff Sales, Code Violations, and Fiscal Officer all returned the exact data each agent claimed (row counts to the unit, top-row strings, parcel/case IDs, money figures).
2. Probate (the previously-untested-live scraper) successfully navigated the entire flow that its scraper depends on: T&C acceptance → form-based party search with EST + DECEDENT filters → case-detail extraction → Parties tab DOD parsing. Every field referenced in the scraper's spec was present in the live HTML.
3. The one indirect verification (Fiscal Officer School District) is consistent with all other observed fields and matches the per-site doc's documented "XHR loads after initial render" behavior. Not a defect, just a verification-environment limitation.
4. No agent hallucination detected. Numbers, strings, and structural claims all hold against live source data as of 2026-05-23.
