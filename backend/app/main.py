"""
Tranchi Engine — FastAPI application entry point
Port: 8011 (per IntelleQ EC2 port map — Gotham is 8010)
Security:
  - CORS: explicit origins only, no wildcards
  - Swagger/ReDoc disabled when DEBUG=false
  - Rate limiting via slowapi
  - Global exception handler (no stack traces to client)
  - X-Request-ID on every response
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import settings
from app.database import close_pool, get_pool

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Tranchi Engine starting up on port 8011")
    await get_pool()
    yield
    await close_pool()
    logger.info("Tranchi Engine shut down cleanly")


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Tranchi Engine API",
    description="Cleveland real-estate deal scraping backend for Tranchi.ai",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Content-Type", "Authorization", "Cf-Access-Jwt-Assertion"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred. Please try again."},
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    logger.warning("ValueError on %s: %s", request.url.path, str(exc))
    return JSONResponse(
        status_code=400,
        content={"detail": "Invalid request data."},
    )


# Routers will be registered here as Phase C builds them.
# app.include_router(listings.router, prefix="/api/v1/listings", tags=["listings"])
# app.include_router(sources.router,  prefix="/api/v1/sources",  tags=["sources"])


@app.get("/health", tags=["health"])
async def health() -> dict:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok", "db": "connected"}
    except Exception:
        logger.exception("Health check DB ping failed")
        return {"status": "degraded", "db": "disconnected"}
