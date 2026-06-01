"""One-off / ad-hoc post-pass runner: ensure-parcels + cross-source dedup.

The per-site (`--site X`) run path skips the post-run passes (those only fire on a
full run). This runs just the ensure-parcels + dedup passes against the live DB so a
targeted multi-source build (e.g. a new market) can be deduped without re-sweeping
every source. The every-3h full cron runs these automatically; this is for manual
verification right after a fresh source build.
"""
import asyncio
import asyncpg

from app.config import settings
from app.scrapers.run import _ensure_parcels_for_listings, _dedup_cross_source_listings


async def main() -> None:
    pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=5)
    try:
        stubs = await _ensure_parcels_for_listings(pool)
        dupes = await _dedup_cross_source_listings(pool)
        print(f"ensure_parcels stubs created: {stubs}")
        print(f"cross-source duplicates flagged: {dupes}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
