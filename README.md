# Tranchi Engine

Cleveland (Cuyahoga County, OH) real-estate deal scraping backend **+ dashboard** for Tranchi.ai.

**Client:** Marc Munoz / Tranchi.ai
**Live dashboard:** https://tranchi.intelleqn8n.net  _(currently public — Cloudflare Zero Trust gate pending)_
**API port:** `8012` — `tranchi-api.service` (NOT 8011; that's `intelleq-chat`)
**Schema:** `tranchi` on the `tranchi` database
**Scraper cron:** `0 */3 * * *` (every 3h UTC)
**Log:** `/var/log/tranchi/scrape.log`

## Sources

| Key | Site | Role | Output |
|-----|------|------|--------|
| `land_bank` | Cuyahoga Land Bank | deal | listings |
| `sheriff_sales` | Cuyahoga Sheriff (ProWare) | deal (tax-delinquent foreclosure) | listings |
| `probate` | Cuyahoga Probate Court (ProWare) | deal (name/address → parcel join) | listings + signals |
| `code_violations` | Cleveland ArcGIS | signal | signals |
| `fiscal_officer` | Cuyahoga Fiscal Office (MyPlace) | registry / enrichment spine | parcels |

Only the 3 **deal** sources produce browseable listings. Code violations are distress **signals**; fiscal officer is the parcel **registry**. See `docs/README.md`.

## Read API (`:8012`, served at `/api/v1` via nginx)

- `GET /api/v1/sources` — per-source `scrape_runs` stat cards + `source_url` + `category` (deal|signal|registry)
- `GET /api/v1/listings` — paginated/filterable; signal stacking is **distinct-distress-type** based, `is_hot` = 2+ dimensions
- `GET /api/v1/listings/{id}` — listing + parcel enrichment + full signal list

## Dashboard (`frontend/`)

React 19 + Vite + Tailwind v4 + React Query, Tranchi navy/gold light theme. Two tabs:
- **Listings** — filterable/sortable table + slide-in detail drawer; typed signal chips (e.g. `Probate · Code Violation`); gold HOT badge when `is_hot`.
- **Sources** — split into **Deal Sources** (the listing scrapers) and **Data & Signal Sources** (code violations + fiscal officer), each with a link to the real source site.

Build + deploy: `cd frontend && npm run build` → rsync `dist/` to `/var/www/tranchi/` on EC2. See `docs/deployment.md`.

> ⚠️ **Tailwind v4 gotcha:** use the parentheses CSS-var syntax `bg-(--color-navy)`, NOT the v3 bracket form `bg-[--color-navy]` (v4 compiles the bracket form to invalid CSS → transparent).

## Running scrapers

```bash
cd /home/ubuntu/tranchi-engine/backend
python -m app.scrapers.run              # all scrapers
python -m app.scrapers.run --site land_bank
python -m app.scrapers.run --dry-run
SHERIFF_BACKFILL=1 python -m app.scrapers.run --site sheriff_sales   # all historical sheriff dates
```

## Schema migration

```bash
cd backend
DATABASE_URL=postgresql://... alembic upgrade head
```

## Architecture

```
Browser → https://tranchi.intelleqn8n.net  (Cloudflare proxy → nginx on EC2, Let's Encrypt TLS)
  ├── /            → /var/www/tranchi        (React dashboard, static)
  └── /api/v1/*    → 127.0.0.1:8012          (tranchi-api.service, FastAPI)
                         └── asyncpg pool → tranchi DB
                             ├── tranchi.listings    (sheriff, land bank, probate)
                             ├── tranchi.parcels     (fiscal officer identity spine)
                             ├── tranchi.signals     (cross-source distress signals)
                             └── tranchi.scrape_runs  (per-source run stats)

Cron (every 3h UTC) → python -m app.scrapers.run
  ├── LandBankScraper       → listings
  ├── CodeViolationsScraper → signals (ArcGIS)
  ├── SheriffSalesScraper   → listings (ProWare)
  ├── FiscalOfficerScraper  → parcels
  └── ProbateScraper        → listings + signals (ProWare + fiscal-officer name/address join)
```

## Roadmap

Phase 2 backlog (prioritized) in `docs/PHASE-2-BACKLOG.md` — top item: **Realauction** (login-walled *upcoming* tax-deed auctions).
