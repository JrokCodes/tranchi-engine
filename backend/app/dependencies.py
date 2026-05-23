"""
Tranchi Engine — FastAPI auth dependencies.
Identity via Cloudflare Zero Trust (Cf-Access-Jwt-Assertion header).
Local dev fallback: set DEV_USER_EMAIL env var.
"""
from __future__ import annotations

import base64
import json
import logging
from uuid import UUID

import asyncpg
from fastapi import Depends, HTTPException, Request, status

from app.config import settings
from app.database import get_db

logger = logging.getLogger(__name__)
security_logger = logging.getLogger("security")


def _decode_cf_jwt_email(token: str) -> str | None:
    """Extract email claim from a CF JWT payload (no signature verification needed)."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("email")
    except Exception:
        return None


async def get_current_user(
    request: Request,
    conn: asyncpg.Connection = Depends(get_db),
) -> dict:
    """Resolve identity in priority order: CF JWT header, then DEV_USER_EMAIL."""
    email: str | None = None

    cf_token = request.headers.get("Cf-Access-Jwt-Assertion")
    if cf_token:
        email = _decode_cf_jwt_email(cf_token)
        if email is None:
            security_logger.warning(
                "CF_JWT_MALFORMED ip=%s path=%s",
                request.client.host if request.client else "unknown",
                request.url.path,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Malformed Cloudflare identity token",
            )

    if email is None and settings.DEV_USER_EMAIL:
        email = settings.DEV_USER_EMAIL

    if email is None:
        security_logger.warning(
            "AUTH_MISSING ip=%s path=%s",
            request.client.host if request.client else "unknown",
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    row = await conn.fetchrow(
        "SELECT id, email, name, role, created_at FROM tranchi.users WHERE email = $1",
        email,
    )
    if row is None:
        security_logger.warning("AUTH_USER_NOT_FOUND email_hash=%s", hash(email))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    return dict(row)


async def require_owner(user: dict = Depends(get_current_user)) -> dict:
    """Restrict endpoint to role='owner' only."""
    if user["role"] != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner access required",
        )
    return user
