#!/usr/bin/env bash
# One-shot, self-contained: backfill last_sale_date for ALL active NON-PROBATE parcels
# that are missing it, then run the transfer guard so any that have sold get retired.
# Designed to run detached (nohup) on EC2 — no interactive session required. Idempotent:
# re-running only touches parcels still missing last_sale_date.
set -uo pipefail
cd /home/ubuntu/tranchi-engine/backend
set -a; source .env; set +a
LOG=/var/log/tranchi/enrich-nonprobate.log
echo "=== $(date -u) START non-probate last_sale backfill ===" >> "$LOG"

# Targets: active, non-probate, last_sale_date NULL (via DATABASE_URL, no sudo needed).
PARCELS=$(venv/bin/python - <<'PY'
import os, asyncio, asyncpg
async def main():
    pool = await asyncpg.create_pool(os.environ['DATABASE_URL'], min_size=1, max_size=2)
    rows = await pool.fetch("""
        SELECT DISTINCT l.source_listing_id
        FROM tranchi.listings l
        LEFT JOIN tranchi.parcels p ON p.parcel_number = l.source_listing_id
        WHERE l.status='active' AND l.signal_type <> 'probate'
          AND l.source_listing_id IS NOT NULL AND p.last_sale_date IS NULL
    """)
    print(' '.join(r['source_listing_id'] for r in rows))
    await pool.close()
asyncio.run(main())
PY
)
echo "$(date -u) targets: $(echo $PARCELS | wc -w) parcels" >> "$LOG"

if [ -n "$PARCELS" ]; then
  venv/bin/python scripts/enrich_sales.py --parcels $PARCELS --concurrency 4 >> "$LOG" 2>&1
fi

# Clean-up: retire any listing whose parcel now shows a transfer (sold-while-listed).
venv/bin/python - <<'PY' >> "$LOG" 2>&1
import os, asyncio, asyncpg
from app.scrapers.run import _mark_transferred_listings
async def main():
    pool = await asyncpg.create_pool(os.environ['DATABASE_URL'], min_size=1, max_size=2)
    n = await _mark_transferred_listings(pool)
    print(f"transfer guard: {n} newly transferred")
    await pool.close()
asyncio.run(main())
PY
echo "=== $(date -u) DONE non-probate backfill ===" >> "$LOG"
