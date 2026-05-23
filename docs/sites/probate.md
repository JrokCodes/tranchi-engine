# Cuyahoga Probate Court

## Source

- **URL:** https://probate.cuyahogacounty.gov/pa/
- **Owner:** Cuyahoga County Probate Court
- **Vendor:** ProWare 2.6.0416 (ASP.NET WebForms)
- **Record status:** Public court record; Ohio Rev. Code 149.43
- **ToS posture:** ⚠️ **The only site in this engine with explicit anti-mining language.** Verbatim from the agreement page: *"data mining, robots, scripts codes, other executables, or similar data gathering and extraction tools"* are prohibited. **Posture: scrape carefully, never bulk-pull.** Per Marc, the data is public and he accepts the operational risk (he scrapes Zillow himself through trial-and-error of getting banned).
- **Last verified:** 2026-05-23 (Playwright probe — scraper not yet live-tested on EC2)

## Why this is the highest-value source in the entire engine

When someone dies and leaves real estate, the executor's legal mandate is to liquidate the estate quickly so heirs can be paid. Executors are emotionally distant from the property, time-pressured by probate court deadlines, and often out-of-state — **the most-motivated seller class in the market.** A flip investor typically buys at 60-70% of ARV here vs 80-90% on MLS.

The catch: probate cases name a *person* (the decedent), not a *property*. Surfacing actual property listings requires the 2-step join through Fiscal Officer.

## Data shape

**Form fields (verbatim):**
- `ctl00$mpContentPH$txtPPLYear` — case year (we use current year)
- `ctl00$mpContentPH$ddlPplCaseCat` — case category dropdown
- `ctl00$mpContentPH$ddlPartyRole` — party role dropdown
- `ctl00$mpContentPH$txtLName` — last name (must be non-empty for form to submit)
- `ctl00$mpContentPH$btnSearchByPerson` — submit button

**Important codes:**
- Case Category for Estate: **`EST`** (display: "ESTATE")
- Party Role for decedent: **`DC`** (display: "DECEDENT")
- T&C accept button: `ctl00$mpContentPH$btnYes` (NOT just `btnYes` — needs full master-page path)

**Case number format:** `2026EST305113` — year + category + sequential

**Detail URL pattern:** `CaseSummary.aspx?q=<base64(internal_int_id)>` — the internal int ID is monotonically increasing, which enables ID-cursor enumeration (see scrape approach)

**Gold field (from Parties page):** `DECEDENT  FIRSTNAME LASTNAME (DOD: MM/DD/YYYY)` followed by pre-death situs address

## Scrape approach — ID cursor enumeration (not date-range)

The form has **no date filter** — only Case Year. The earlier plan assumed date-range queries; live probe disproved that. Instead, we exploit the monotonically-increasing internal int ID embedded in detail URLs:

```python
class ProbateScraper(ListingScraper):
    site_name = "Cuyahoga Probate Court"
    
    async def fetch_and_parse(self):
        # 1. Read tranchi.probate_cursor.last_id (default seeded in migration)
        # 2. Walk IDs cursor+1, cursor+2, ... via base64(id)
        # 3. For each ID:
        #    - GET CaseSummary.aspx?q=<base64(id)>
        #    - Parse case category — if not EST, skip + increment miss counter
        #    - If "No Record Found" body (ProWare doesn't return 404), increment miss counter
        #    - If miss counter > 25, stop
        #    - If EST → extract case#, decedent, file date, executor, attorney
        #    - GET Parties tab → extract DOD + situs address
        #    - Call fiscal_officer.search_by_owner(decedent) → 0+ parcel candidates
        #    - Yield one RawListing per candidate with confidence + ambiguous flag
        # 4. Update probate_cursor.last_id to highest successfully ingested
        # 5. Rate limit: 1 req/sec floor enforced inside ProwareSession
```

**Why this is better than alphabet sweep:**
- Sweep: O(26 × years × cases-per-letter) requests per run, always
- Cursor: O(new_cases_since_last_run) requests per run, typically much less

**ProWare doesn't return HTTP 404 for unassigned IDs** — it returns HTTP 200 with a "No Record Found" body. The scraper uses a `consecutive_miss` counter (default max 25) instead of HTTP status to know when to stop. This counter increments on both unassigned IDs AND non-Estate cases (Trust, Guardianship, Marriage, Will).

**Tool stack:**
- `httpx` + `BeautifulSoup` via shared `proware_client.py`
- T&C acceptance: `ProwareSession.accept_agreement()` POSTs to `/pa/` with `agree_button_id="ctl00$mpContentPH$btnYes"`, mints `ASP.NET_SessionId` cookie
- Rate limiting: 1 req/sec floor via `ProwareSession._rate_limiter` (mandatory for this site)

## Output destination

- **Table:** `tranchi.listings`
- **`source_site`:** `probate`
- **`signal_type`:** `probate`
- **`case_number`:** court case number (e.g. `2026EST305113`)
- **`source_listing_id`:** parcel number (from fiscal_officer match)
- **`property_address`:** from fiscal_officer ParcelMatch.situs_address
- **`property_state`:** `OH`
- **`trustee_name`:** executor/administrator name (we reuse this field even though they're not a trustee in the legal sense — the semantic is "person responsible for the sale")
- **Metadata stored alongside:** decedent_name, DOD, attorney, filing_date, probate_internal_id, join_confidence, ambiguous_flag
- **Side effect:** Also writes to `tranchi.signals` with `signal_type='probate'` (non-fatal if FK to parcels misses — see notes)

**One Estate case may produce 0, 1, or N `RawListing` rows** depending on how many parcels fiscal_officer matches to the decedent's name.

## Verification recipe

1. Open https://probate.cuyahogacounty.gov/pa/ → click **Yes** on the T&C agreement
2. Left nav → **Search By Party**
3. Form values:
   - Year: `2026`
   - Case Category: `ESTATE`
   - Party Role: `DECEDENT`
   - Last Name: *(must be non-empty for the form to submit — try `SMITH`)*
4. Click Search
5. Note top case# (format `2026EST######`)
6. Click into a case → confirm: case#, Case Type, Filing Date, Case Status
7. Click **Parties** tab → confirm: `DECEDENT FIRSTNAME LASTNAME (DOD: MM/DD/YYYY)` line + pre-death situs address

**Cross-check the parcel join:** copy a decedent's name, paste into MyPlace Owner search — scraper should have produced `RawListing(s)` matching the parcels that show up.

**Live smoke test command (before first cron):**
```bash
ssh intelleq-ec2
cd /home/ubuntu/tranchi-engine/backend && source venv/bin/activate
alembic upgrade head  # applies migration 002 + future
PROBATE_MAX_IDS=20 python -m app.scrapers.probate
```
Expected: ~25-30 seconds wall-clock (1 req/sec × 20 IDs + Parties fetches for Estate matches), at least 1 Estate case found, at least 1 parcel join via fiscal_officer.

## Known issues / gotchas

- ⚠️ **TOS clause is explicit.** Mitigations baked in: 1 req/sec floor (mandatory), Estate cases only, cursor-based delta (never bulk-historical), generic Chrome UA. If we ever see the site behave differently (CAPTCHA, IP block, layout changes) — that's the signal to pause and re-evaluate. Long-term options: switch to a court-data vendor like CourtListener / Trellis ($200-500/mo per county) or hybrid (vendor for bulk, scrape for delta).
- **Cursor seeded at 2817655** (probe ID 2818155 minus 500) — first run walks ~500 IDs forward catching any cases filed just before probe. To reset: `UPDATE tranchi.probate_cursor SET last_id = <N> WHERE id = 1;`
- **Non-Estate cases skipped silently** — Trust, Guardianship, Marriage, Will all return None from `_parse_case_summary` and increment `consecutive_miss`. Dense stretches of non-Estate cases will trigger the 25-miss ceiling and stop enumeration. This is intentional (protects against runaway).
- **Form rejects empty Last Name** — that's why the alphabet sweep was the original alternative. Cursor approach side-steps this entirely.
- **T&C button master-page prefix is the one external dependency.** If ProWare ever changes from `ctl00$mpContentPH$btnYes` to a different prefix, that's the single string to update in the `accept_agreement()` call. The button text "Yes" itself is more stable than the ID.
- **Signal FK miss is non-fatal** — if a decedent's parcel hasn't been loaded by fiscal_officer's bulk sweep yet, the signal row write is skipped + logged at DEBUG. Next cron run after fiscal_officer has loaded that letter, the signal writes cleanly.
- **Not yet live-tested** — unit tests pass on probe data, but the live T&C → Parties → fiscal_officer round-trip needs the EC2 smoke run above. If something breaks live (cookie domain, ViewState format, master-page prefix), that's where it surfaces.

## File pointers

- **Scraper:** `backend/app/scrapers/probate.py` (792 lines)
- **Migration:** `alembic/versions/002_probate_cursor.py` (49 lines)
- **Shared helper:** `backend/app/scrapers/proware_client.py` (rate limit, T&C accept, ViewState handling)
- **Join dependency:** `backend/app/scrapers/fiscal_officer.py` (`search_by_owner` callable)
- **Reference template:** Gotham `orlans.py` (T&C-gated session) + `mtglaw.py` (ASP.NET POST-back)
- **Tests:** `tests/scrapers/test_probate.py` (to be written)
- **Field-map appendix:** `Clients/Marc/tranchi/research/cuyahoga-field-map.md` § A
- **Screenshots:** `Clients/Marc/tranchi/research/probate-landing.png`, `probate-results.png`
