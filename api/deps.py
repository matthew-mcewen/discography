"""Shared FastAPI dependencies: DB pool, settings, and admin auth."""
from __future__ import annotations

import secrets
from typing import Any

import psycopg_pool
from fastapi import Depends, HTTPException, Request, status

import db.tenancy as tenancy
from utils.settings import ApiSettings


def get_pool(request: Request) -> psycopg_pool.AsyncConnectionPool:
    return request.app.state.pool


def get_settings(request: Request) -> ApiSettings:
    return request.app.state.settings


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header (expected 'Bearer <key>').",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


async def require_admin(
    request: Request,
    pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool),
    settings: ApiSettings = Depends(get_settings),
) -> dict[str, Any]:
    """
    Require an admin-scoped credential: either the bootstrap ADMIN_API_KEY or an
    'admin'-scope row in api_keys. Returns the caller's key record.
    """
    token = _bearer_token(request)

    if settings.admin_api_key and secrets.compare_digest(token, settings.admin_api_key):
        return {"scope": "admin", "guild_id": None, "name": "bootstrap"}

    record = await tenancy.verify_api_key(pool, token)
    if record is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key.")
    if record["scope"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin scope required.")
    return record
