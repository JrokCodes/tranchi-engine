"""
Tranchi Engine — asyncpg connection pool.
min_size=2, max_size=10. All queries use parameterized SQL — no string concatenation.
"""
from __future__ import annotations

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.DATABASE_URL,
            min_size=2,
            max_size=10,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def get_db():
    """FastAPI dependency — yields a single connection from the pool."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
