"""API key management (admin only)."""
from __future__ import annotations

from typing import Any

import psycopg_pool
from fastapi import APIRouter, Depends, HTTPException, Response, status

import db.tenancy as tenancy
from api.deps import get_pool, require_admin
from api.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"], dependencies=[Depends(require_admin)])


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool)) -> list[dict[str, Any]]:
    return await tenancy.list_api_keys(pool)


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_key(
    payload: ApiKeyCreate,
    pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool),
) -> dict[str, Any]:
    if payload.scope == "tenant" and payload.guild_id is None:
        raise HTTPException(status_code=422, detail="guild_id is required for a tenant-scoped key.")
    if payload.scope == "tenant" and await tenancy.get_tenant(pool, payload.guild_id) is None:
        raise HTTPException(status_code=404, detail="Tenant not found for tenant-scoped key.")
    return await tenancy.create_api_key(pool, payload.name, payload.scope, payload.guild_id)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    key_id: int,
    pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool),
) -> Response:
    if not await tenancy.revoke_api_key(pool, key_id):
        raise HTTPException(status_code=404, detail="Key not found or already revoked.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
