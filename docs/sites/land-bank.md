# Cuyahoga Land Bank

## Source

- **URL:** https://cuyahogalandbank.org/all-available-properties/
- **Owner:** Cuyahoga County Land Reutilization Corporation ("Land Bank") — public mission-driven nonprofit that takes title to tax-foreclosed, abandoned, and condemned properties and sells them cheap to rehabbers, nonprofits, and adjacent homeowners.
- **Record status:** Public inventory; Land Bank explicitly publishes this list for prospective buyers
- **ToS posture:** None. Public agency, explicitly wants visibility on these properties.
- **Last verified:** 2026-05-23 (Playwright probe + scraper run)

## Data shape

Single-page table, ~99 active properties (was ~144 in earlier probe — inventory varies). Sold by inquiry only — **no price field anywhere**. Surface this to Marc on Sunday as accurate-to-source.

**Table columns (verbatim):**

| Column | Type | Example |
|---|---|---|
| Parcel number | string `DDD-NN-NNN` | `140-17-057` |
| Number | int | `17425` |
| Street | string | `Eldamere Ave` |
| City | string (Cuyahoga municipality) | `Cleveland` |
| Ward | string (city ward number, blank for non-Cleveland) | `8` |
| Date Posted | date | `2026-05-15` |
| Status | enum (3 values) | `Vacant Land - Available` |

**Status values (exactly 3):**
- `Vacant Land - Available`
- `Renovation Underway - Available Soon`
- `New Construction Underway - Available Soon`

**Detail page:** `?parcel_id=<PID>` (predictable URL). Adds photos + deed restrictions + sale type — currently NOT enriched by default (would be 99 extra GETs per run).

**Photos:** `storage.googleapis.com/cclrc-pps2.appspot.com/Cuyahoga_Land_Bank/parcel_event/Level_1_-_<PID>_-_<EVENT_ID>/images/<file>.JPG`

**Update cadence:** Inventory turns slowly. New properties posted weekly-ish. Cron at 3h is over-frequent but harmless.

## Scrape approach

- **Tool stack:** `httpx` + `BeautifulSoup` (vanilla WordPress + DataTables, server-rendered)
- **Pagination:** None — all rows preloaded in one DOM
- **Anti-bot:** None. WordPress + Toolset Views, no protection layer.
- **Rate limiting:** Generic 0.5-1.5s jitter inherited from shared helpers
- **User-Agent:** Rotation from `user_agents.random_ua()`

**Detail-page enrichment toggle:** `_ENRICH_DETAIL = False` in module header. Flip to True for one-time enrichment runs. Adds ~90-120s per run (99 GETs with jitter). Don't enable for cron.

## Output destination

- **Table:** `tranchi.listings`
- **`source_site`:** `land_bank`
- **`signal_type`:** `land_bank_inventory`
- **`case_number`:** parcel number (Land Bank has no case#; parcel# serves as the dedup key)
- **`source_listing_id`:** parcel number
- **Cross-source join key:** parcel number (display format `DDD-NN-NNN`)

## Verification recipe

1. Open https://cuyahogalandbank.org/all-available-properties/
2. Let DataTables fully load
3. Scroll to bottom — footer says **"Showing 1 to 99 of 99 entries"** (or current count)
4. Top row as of 2026-05-23: parcel `140-17-057`, address `17425 Eldamere Ave`, Cleveland
5. **Scraper should match the visible row count** (±1 if inventory shifted between your check and the scrape)
6. **Random spot-check:** pick a middle row (say row 40), note parcel + address, find it in scraper output, fields must match

## Known issues / gotchas

- **No price field exists** — Land Bank sells by inquiry. UI must show "Inquire for price" or similar, not a missing-data warning.
- **Ward column is blank for non-Cleveland properties** (Cleveland has wards 1-17; suburbs don't use the same system). Don't filter on ward presence.
- **Inventory count fluctuates daily** — 99 today, 144 last month. Don't hardcode an expected count for monitoring; use a delta alarm instead (e.g. alert if count drops >20% week-over-week).
- **Detail-page enrichment is off by default.** If we want photos/restrictions/sale-type in production, run that as a separate weekly cron, not the 3h scraper run.

## File pointers

- **Scraper:** `backend/app/scrapers/landbank.py` (207 lines)
- **Reference template:** Gotham `brockandscott.py` (same pattern: server-rendered HTML, regex/bs4 parse)
- **Tests:** `tests/scrapers/test_landbank.py` (to be written)
- **Field-map appendix:** `Clients/Marc/tranchi/research/cuyahoga-field-map.md` § D
- **Screenshots:** `Clients/Marc/tranchi/research/landbank-list.png`
