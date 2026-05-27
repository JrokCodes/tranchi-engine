"""
Shared async client for ArcGIS FeatureServer REST queries.

Used by:
  - code_violations.py (data.clevelandohio.gov ArcGIS FeatureServer)
  - Future Ohio open-data feeds that expose ArcGIS REST endpoints

ArcGIS FeatureServer paginates via resultOffset + resultRecordCount.
Batch size of 2000 is the typical server maximum; some services cap lower
(the count endpoint reveals the total so we know when to stop).

Usage:
    features = []
    async for batch in query_features(url, where="ViolationStatus='Open'"):
        features.extend(batch)

    total = await count_features(url, where="ViolationStatus='Open'")
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import httpx

from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 2000
_REQUEST_TIMEOUT = 30.0
_RETRY_LIMIT = 3
_RETRY_BACKOFF = 2.0  # seconds, doubles each retry


async def _get_arcgis_json(
    client: httpx.AsyncClient, url: str, params: dict[str, str]
) -> dict:
    """GET an ArcGIS /query endpoint and return parsed JSON, retrying transient failures.

    INVARIANT: ArcGIS FeatureServers return application-level errors as HTTP 200 with an
    `{'error': {...}}` body (e.g. {'code': 400, 'message': 'Invalid URL'}) — NOT as an
    HTTP status code. These are frequently transient (a momentary service reload / route
    hiccup; the identical query succeeds seconds later). So we treat BOTH network/HTTP
    errors AND an ArcGIS error-body as retryable, inside one backoff loop, so a single
    glitch doesn't drop the whole source for a cycle. Raises after _RETRY_LIMIT attempts
    (a genuinely persistent error still fails loud).
    """
    for attempt in range(1, _RETRY_LIMIT + 1):
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except Exception as exc:
            if attempt == _RETRY_LIMIT:
                logger.error("ArcGIS query failed after %d attempts: %s", _RETRY_LIMIT, exc)
                raise
            wait = _RETRY_BACKOFF * attempt
            logger.warning(
                "ArcGIS query attempt %d failed: %s — retrying in %.1fs", attempt, exc, wait,
            )
            await asyncio.sleep(wait)
    raise RuntimeError("unreachable")  # pragma: no cover


async def count_features(
    feature_server_url: str,
    where: str = "1=1",
    *,
    extra_params: dict[str, str] | None = None,
) -> int:
    """Return the total feature count for a where clause without fetching geometry.

    Args:
        feature_server_url: Base URL to the FeatureServer layer endpoint
            (e.g. "https://services.arcgis.com/.../FeatureServer/0").
        where: SQL-style WHERE clause (default: all records).
        extra_params: Additional query parameters to include.

    Returns:
        Integer count of matching features.
    """
    params: dict[str, str] = {
        "where": where,
        "returnCountOnly": "true",
        "f": "json",
    }
    if extra_params:
        params.update(extra_params)

    async with httpx.AsyncClient(headers=default_headers(), timeout=_REQUEST_TIMEOUT) as client:
        data = await _get_arcgis_json(client, f"{feature_server_url}/query", params)
        return data.get("count", 0)


async def query_features(
    feature_server_url: str,
    where: str = "1=1",
    out_fields: str = "*",
    batch_size: int = _DEFAULT_BATCH_SIZE,
    *,
    extra_params: dict[str, str] | None = None,
) -> AsyncIterator[list[dict]]:
    """Paginate through all features on an ArcGIS FeatureServer layer.

    Yields batches of feature attribute dicts (geometry excluded by default).
    Handles resultOffset pagination automatically, stops when fewer results
    than batch_size are returned (signals last page).

    Args:
        feature_server_url: Base URL to the FeatureServer layer endpoint.
        where: SQL-style WHERE clause.
        out_fields: Comma-separated field list or "*" for all.
        batch_size: Records per request (max 2000 on most services).
        extra_params: Additional query parameters.

    Yields:
        list[dict] — each dict is one feature's attributes.
    """
    offset = 0
    query_url = f"{feature_server_url}/query"

    async with httpx.AsyncClient(headers=default_headers(), timeout=_REQUEST_TIMEOUT) as client:
        while True:
            params: dict[str, str] = {
                "where": where,
                "outFields": out_fields,
                "returnGeometry": "false",
                "resultOffset": str(offset),
                "resultRecordCount": str(batch_size),
                "f": "json",
            }
            if extra_params:
                params.update(extra_params)

            resp_data = await _get_arcgis_json(client, query_url, params)

            features = resp_data.get("features", [])
            if not features:
                break

            # Yield attribute dicts only (callers don't need geometry)
            yield [f.get("attributes", {}) for f in features]

            if len(features) < batch_size:
                # Last page — server returned fewer than the batch ceiling
                break

            offset += batch_size
            # Small courtesy pause between pages
            await asyncio.sleep(0.5)
