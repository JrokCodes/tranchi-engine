# tranchi-engine docs

Technical documentation for the Tranchi.ai deal-discovery scraper engine. Covers architecture, per-site scrape mechanics, verification, deployment, and expansion playbooks.

If you are new to this repo, read in this order:

1. **This file** — architecture + module map
2. **`sites/<source>.md`** — the specific source you're touching
3. **`verification.md`** — how to confirm a scraper is correct end-to-end
4. **`deployment.md`** — getting it running on EC2
5. **`expansion.md`** — adding a new county or new source type
6. **`PHASE-2-BACKLOG.md`** — prioritized next work (top: Realauction login-walled tax-deed source)

## What this engine does

Scrapes public real-estate-distress data from Cuyahoga County (Cleveland metro), Ohio, on a 3-hour cron, dedupes across sources, joins signals to a canonical parcel registry, and exposes the result via Postgres (`tranchi` schema). The Tranchi.ai web platform consumes this data to surface motivated-seller deals (probate, tax-delinquent foreclosures, code-violated properties, Land Bank inventory) to its investor user base.

Architecture intentionally mirrors Gotham (sister project for Maryland) — same shape, looser prefilter (Tranchi wants quantity over qualification, filtering happens user-side on the platform).

## Data flow

```
┌────────────────────────────────────────────────────────────────┐
│  CRON (0 */3 * * * UTC, ET date math inside scrapers)          │
│  python -m app.scrapers.run                                    │
└────────────────────────────┬───────────────────────────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
   ListingScrapers      SignalScrapers       (future: enrichment)
   ───────────────      ───────────────
   land_bank            code_violations
   sheriff_sales        (future: vacant registry)
   probate              (future: lis pendens)
   fiscal_officer
        │                    │
        ▼                    ▼
   RawListing[]         RawSignal[]
        │                    │
        ▼                    ▼
   prefilter (state=OH +    (no prefilter — signals
   address is not null)      always relevant if parcel
        │                    matches)
        ▼                    │
   canonicalize_address      │
   canonicalize_city/county  │
        │                    │
        ▼                    ▼
   tranchi.listings    tranchi.signals
   (PK: id; unique     (PK: id; unique
   on source_site +    on parcel_number +
   case_number)        signal_type +
                       source + observed_at::date)
        │                    │
        ▼                    │
   cross-source dedup        │
   (cluster by normalized    │
   _address; mark            │
   duplicate_of)             │
        │                    │
        └──────────┬─────────┘
                   ▼
         tranchi.parcels (identity spine — owner, value, tax balance)
                   │
                   ▼
         per-source stat write to tranchi.scrape_runs
         (found / passed / active / filtered /
          dupes / delisted / expired / new_today —
          shape matches Gotham's Sources dashboard cards)
```

## Schema (tranchi schema in Postgres)

- **`tranchi.listings`** — actual properties surfaced to Tranchi users. One row per (source_site, case_number) or (source_site, normalized_address, sale_date). Stores: source_site, case_number, parcel#, address parts, sale_date, deposit, status, signal_type, first_seen_at, last_seen_at, duplicate_of, normalized_address.
- **`tranchi.parcels`** — canonical parcel registry. PK on `parcel_number` (Cuyahoga `DDD-NN-NNN` format). Populated primarily by `fiscal_officer.py`. Stores owner, market value, tax balance, delinquent flag, building characteristics. **This is the identity spine that lets us cross-join signals to listings.**
- **`tranchi.signals`** — distress signals tagging parcels. One row per (parcel_number, signal_type, source, observed_at::date). Examples: probate filing, tax delinquency flag, code violation, eviction filing. Populated by SignalScrapers (code_violations + future sources). Drives the "this parcel has N signals = HOT lead" UX.
- **`tranchi.scrape_runs`** — per-run audit + stats. Mirrors Gotham's Sources dashboard shape exactly (found / passed / active / filtered / dupes / delisted / expired / new_today / error_message). Drives the future Sources tab on the Tranchi-side dashboard.

## Source taxonomy

| Source | Type | Output table | Phase | Status |
|---|---|---|---|---|
| Cuyahoga Land Bank | Listing | listings | 1 | ✅ shipped |
| Cuyahoga Sheriff Sales | Listing | listings | 1 | ✅ shipped |
| Cuyahoga Fiscal Officer (MyPlace) | Parcel registry / enrichment spine | parcels (+ optional tax signals) | 1 | ✅ shipped |
| Cuyahoga Probate Court | Listing (via fiscal_officer name/address join) | listings + signals | 1 | ✅ shipped |
| Cleveland Code Violations | Signal | signals | 1 | ✅ shipped |
| Cuyahoga Common Pleas (lis pendens) | Listing | listings | 2 | spec'd |
| Cleveland Vacant Property Registry | Signal | signals | 2 | spec'd |
| Cleveland Municipal Court evictions | Signal | signals | 2 | spec'd |
| HUD Home Store REO | Listing | listings | 2 | spec'd |
| Cuyahoga Annual Delinquent Tax PDF | Signal | signals | 2 | spec'd |
| Realauction (upcoming sheriff sales) | Listing | listings | 2 | spec'd |

## Module map

```
backend/app/scrapers/
├── base.py                    ListingScraper + SignalScraper ABCs
├── models.py                  RawListing + RawSignal + ScrapeResult pydantic
├── db.py                      Canonicalization helpers + upsert + dedup
├── prefilter.py               Hard filters (loose for Tranchi: OH + valid addr)
├── _time.py                   today_et / n_days_ago_et (America/New_York)
├── user_agents.py             Generic Chrome UA rotation (no bot identifier)
├── oh_cities.py               Cuyahoga 59-municipality allowlist + adjacent county map
├── address_validator.py       (ported from Gotham)
├── proware_client.py          Shared httpx client for ProWare ASP.NET WebForms
├── arcgis_client.py           Shared client for ArcGIS FeatureServer REST
├── run.py                     CLI orchestrator + cron entry point
├── landbank.py                Land Bank scraper
├── sheriff.py                 Sheriff Sales scraper
├── code_violations.py         Code Violations signal scraper
├── fiscal_officer.py          MyPlace scraper + owner-search callable
└── probate.py                 Probate Court scraper (uses fiscal_officer for join)
```

## Cross-source parcel-number gotcha

Cuyahoga has **two parcel-number formats in active use**:

- Display format: `DDD-NN-NNN` (e.g. `140-17-057`) — used by Sheriff Sales, Fiscal Officer (MyPlace), Land Bank
- Compact format: 8 digits no hyphens (e.g. `14017057`) — used by Cleveland Open Data (code violations)

Both refer to the same parcel. The canonicalization helper in `db.py` normalizes both to the display format on upsert. **If you write a new scraper, always normalize to display format** so cross-source joins work.

## Scraper identity / posture

Per Marc's directive: low-detection generic browser identity, NOT identified bot. See `user_agents.py` for the rotation. Rate limits:

- **Probate court:** 1 req/sec floor (only site with explicit anti-mining ToS clause)
- **Everything else:** 0.5-1.5s jitter between requests to avoid burst patterns
- No headless-Playwright unless absolutely required (Playwright fingerprint != real Chrome)

## Where things live outside this repo

- **Engagement state + Marc context:** `Intelleq-Library-Of-Babel/Clients/Marc/` (README, SOUL.md, call transcripts)
- **Field-map research artifacts:** `Intelleq-Library-Of-Babel/Clients/Marc/tranchi/research/cuyahoga-field-map.md` (+ screenshots)
- **Live dashboard:** https://tranchi.intelleqn8n.net (nginx static + `/api/` → API)
- **Read API:** `tranchi-api.service` on `127.0.0.1:8012` (NOT 8011 — that's intelleq-chat)
- **EC2 deploy:** `/home/ubuntu/tranchi-engine/` (backend); frontend build served from `/var/www/tranchi/`
- **Logs:** `/var/log/tranchi/scrape.log`
- **Database:** `tranchi` DB on intelleq-ec2 Postgres
- **Phase 2 backlog:** `docs/PHASE-2-BACKLOG.md`
