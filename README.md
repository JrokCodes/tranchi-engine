# Tranchi Engine

Cleveland (Cuyahoga County, OH) real-estate deal scraping backend for Tranchi.ai.

**Client:** Marc Munoz / Tranchi.ai  
**EC2 port:** 8011  
**Schema:** `tranchi` on the `tranchi` database  
**Cron:** `0 */3 * * *` (every 3h UTC)  
**Log:** `/var/log/tranchi/scrape.log`

## Sources (Phase B)

| Key | Site | Signal type | Table |
|-----|------|-------------|-------|
| `land_bank` | Cuyahoga Land Bank | `land_bank_inventory` | listings |
| `code_violations` | Cleveland ArcGIS | `code_violation` | signals |
| `sheriff_sales` | Cuyahoga Sheriff | `tax_delinquent_foreclosure` | listings |
| `fiscal_officer` | Cuyahoga Fiscal Office | — | parcels |
| `probate` | Cuyahoga Probate Court | `probate` | listings |

## Running scrapers

```bash
cd /home/ubuntu/tranchi-engine/backend
python -m app.scrapers.run              # all scrapers
python -m app.scrapers.run --site land_bank
python -m app.scrapers.run --dry-run
```

## Schema migration

```bash
cd backend
DATABASE_URL=postgresql://... alembic upgrade head
```

## Architecture

```
EC2 :8011
└── FastAPI (main.py)
    └── asyncpg pool → tranchi DB
        ├── tranchi.listings     (sheriff, land bank, probate)
        ├── tranchi.parcels      (fiscal officer identity spine)
        ├── tranchi.signals      (cross-source distress signals)
        └── tranchi.scrape_runs  (per-source run stats)

Cron (every 3h UTC)
└── python -m app.scrapers.run
    ├── LandBankScraper      → listings
    ├── CodeViolationsScraper → signals (via ArcGIS)
    ├── SheriffSalesScraper  → listings (via ProWare)
    ├── FiscalOfficerScraper → parcels
    └── ProbateScraper       → listings (via ProWare + fiscal join)
```
