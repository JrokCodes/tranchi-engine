# Deployment — EC2, Postgres, cron

## Host

- **Server:** `intelleq-ec2` (18.219.255.79)
- **SSH:** `ssh intelleq-ec2`
- **OS:** Ubuntu (shared with Gotham, Bison, PPG, etc.)
- **Why shared:** Existing infrastructure already has Cloudflare Zero Trust, Postgres, monitoring, backups. Spinning a separate box for Tranchi would add cost + maintenance for no real isolation benefit (we use a separate DB + schema for isolation).

## Postgres

- **Database:** `tranchi` (separate DB, not just a schema in Gotham's DB)
- **Schema:** `tranchi` (default schema for all tables in this engine)
- **Tables:** `tranchi.{listings, parcels, signals, scrape_runs, probate_cursor}`
- **Connection:**
  ```
  DATABASE_URL=postgresql+asyncpg://<user>:<pw>@localhost:5432/tranchi
  ```
  Set in `/home/ubuntu/tranchi-engine/backend/.env` (chmod 600, owner ubuntu).

### Migrations

Standard Alembic. From the deploy target:
```bash
cd /home/ubuntu/tranchi-engine/backend
alembic upgrade head
```

Applied migrations (alembic head = **003**):
- `001_tranchi_schema.py` — listings, parcels, signals, scrape_runs + indexes
- `002_probate_cursor.py` — probate ID-cursor state table
- `003_signals_unique_index.py` — `uq_tranchi_signals_natural_key` on `(parcel_number, signal_type, source, (observed_at::date))`

## Filesystem layout

```
/home/ubuntu/tranchi-engine/    ← git repo root (run `git pull` here)
├── backend/
│   ├── app/
│   ├── alembic/
│   ├── venv/            ← Python venv
│   ├── .env             ← secrets (DATABASE_URL, etc.) — chmod 600
│   └── requirements.txt
├── frontend/            ← React dashboard source (built locally, NOT on EC2)
├── docs/
└── deploy/              ← nginx-tranchi.conf template

/var/www/tranchi/        ← built dashboard (rsync target; served by nginx)
/var/log/tranchi/
└── scrape.log           ← cron stdout/stderr
```

## Initial deploy

```bash
ssh intelleq-ec2
sudo mkdir -p /home/ubuntu/tranchi-engine
sudo chown ubuntu:ubuntu /home/ubuntu/tranchi-engine
sudo mkdir -p /var/log/tranchi
sudo chown ubuntu:ubuntu /var/log/tranchi

cd /home/ubuntu/tranchi-engine
git clone git@github.com:JrokCodes/tranchi-engine.git backend
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# If using fiscal_officer tax-flag enrichment:
playwright install chromium

# Set up .env
cp .env.example .env
nano .env  # fill in DATABASE_URL

# Run migrations
alembic upgrade head

# Smoke test
python -m app.scrapers.run --site land_bank --dry-run
```

## Cron

```cron
0 */3 * * * cd /home/ubuntu/tranchi-engine/backend && /home/ubuntu/tranchi-engine/backend/venv/bin/python -m app.scrapers.run >> /var/log/tranchi/scrape.log 2>&1
```

**Cadence:** every 3 hours (UTC). Same as Gotham. Cleveland is Eastern (UTC-5 winter / UTC-4 summer) — internal `_time.today_et()` handles ET date math regardless of UTC cron firing.

**Fires at:** 00:00, 03:00, 06:00, 09:00, 12:00, 15:00, 18:00, 21:00 UTC (= 8pm, 11pm, 2am, 5am, 8am, 11am, 2pm, 5pm ET in summer)

## Log rotation

```
/var/log/tranchi/scrape.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

Add to `/etc/logrotate.d/tranchi`.

## API service (LIVE — port 8012)

`tranchi-api.service` (systemd) serves the FastAPI read API on `127.0.0.1:8012`. **Port 8012, not 8011** — 8011 is taken by `intelleq-chat.service`.

```ini
# /etc/systemd/system/tranchi-api.service
[Unit]
Description=Tranchi API
After=network.target postgresql.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/tranchi-engine/backend
EnvironmentFile=/home/ubuntu/tranchi-engine/backend/.env
ExecStart=/home/ubuntu/tranchi-engine/backend/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8012
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Redeploy backend code (pull + restart + health check):
```bash
ssh intelleq-ec2 "cd /home/ubuntu/tranchi-engine && git pull && sudo systemctl restart tranchi-api && sleep 2 && curl -s 127.0.0.1:8012/health"
```

## Frontend / dashboard deploy (LIVE)

Static React build, served by nginx from `/var/www/tranchi/`; nginx proxies `/api/` → `127.0.0.1:8012`. nginx site config: `/etc/nginx/sites-available/tranchi` (template kept in `deploy/nginx-tranchi.conf`). Build happens **locally** (not on EC2):

```bash
cd frontend && npm run build
rsync -az --delete -e ssh dist/ intelleq-ec2:/var/www/tranchi/
```

## Public hostname + TLS + auth

- **DNS:** `tranchi.intelleqn8n.net` → `18.219.255.79` (Cloudflare-proxied A record).
- **TLS:** Let's Encrypt via `sudo certbot --nginx -d tranchi.intelleqn8n.net` — same nginx (HTTP-01) authenticator as Gotham. **Not** a cloudflared tunnel.
- **Auth:** ⚠️ Cloudflare Zero Trust Access gate is **NOT yet configured** — the dashboard is currently **public**. To gate it: Zero Trust dashboard → Access → Applications → add a self-hosted app on `tranchi.intelleqn8n.net` → Allow policy listing the permitted emails (Jayden + Marc). Same pattern as Gotham.
- **Note:** the CF API token at `/home/ubuntu/.secrets/cloudflare/api-token` is currently **invalid** (rotated/expired) — DNS/Access changes must be done in the dashboard or with a fresh token. Cert issuance does NOT need it (uses nginx HTTP-01).

## Backup

Inherits the shared backup cron from intelleq-ec2:
- Daily 04:00 UTC: pg_dump of all DBs → `/home/ubuntu/backups/postgres/`
- Tranchi DB gets included automatically since it's on the same Postgres instance

## Common ops

### Manual scrape run (one source)
```bash
ssh intelleq-ec2
cd /home/ubuntu/tranchi-engine/backend && source venv/bin/activate
python -m app.scrapers.run --site land_bank
```

### Manual full run
```bash
python -m app.scrapers.run
```

### Check last run stats
```bash
psql -d tranchi -c "select source_site, started_at, found, passed, active, error_message from tranchi.scrape_runs order by started_at desc limit 10"
```

### Check current listing counts per source
```bash
psql -d tranchi -c "select source_site, status, count(*) from tranchi.listings group by source_site, status order by source_site"
```

### Tail the cron log
```bash
tail -f /var/log/tranchi/scrape.log
```

### Reset a stuck scraper run
```bash
psql -d tranchi -c "update tranchi.scrape_runs set status='error', error_message='manually reset' where status='running' and started_at < now() - interval '30 minutes'"
```
