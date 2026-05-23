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
        resp = await client.get(f"{feature_server_url}/query", params=params)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise ValueError(f"ArcGIS error: {data['error']}")
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

            resp_data: dict | None = None
            for attempt in range(1, _RETRY_LIMIT + 1):
                try:
                    resp = await client.get(query_url, params=params)
                    resp.raise_for_status()
                    resp_data = resp.json()
                    break
                except (httpx.HTTPError, Exception) as exc:
                    if attempt == _RETRY_LIMIT:
                        logger.error(
                            "ArcGIS query failed after %d attempts at offset %d: %s",
                            _RETRY_LIMIT, offset, exc,
                        )
                        raise
                    wait = _RETRY_BACKOFF * attempt
                    logger.warning(
                        "ArcGIS query attempt %d failed (offset=%d): %s — retrying in %.1fs",
                        attempt, offset, exc, wait,
                    )
                    await asyncio.sleep(wait)

            if resp_data is None:
                break

            if "error" in resp_data:
                raise ValueError(f"ArcGIS error at offset {offset}: {resp_data['error']}")

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
