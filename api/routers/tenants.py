"""Tenant lifecycle + admin management endpoints."""
from __future__ import annotations

import json
from typing import Any, Optional

import psycopg_pool
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

import db.tenancy as tenancy
from api.deps import get_pool, get_settings, require_admin
from api.discord_rest import DiscordRest, DiscordRestError
from api.schemas import TenantCreate, TenantOut, TenantUpdate
from utils.pg_listener import CHANNEL_BOT_COMMAND, CHANNEL_CONFIG_CHANGED, emit
from utils.settings import ApiSettings

router = APIRouter(prefix="/v1/tenants", tags=["tenants"], dependencies=[Depends(require_admin)])


@router.get("", response_model=list[TenantOut])
async def list_tenants(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool),
) -> list[dict[str, Any]]:
    return await tenancy.list_tenants(pool, status=status_filter, limit=limit, offset=offset)


@router.post("", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    payload: TenantCreate,
    pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool),
) -> dict[str, Any]:
    await tenancy.upsert_tenant_on_join(pool, payload.guild_id, payload.name, payload.owner_user_id)
    extra = payload.model_dump(exclude_unset=True, exclude={"guild_id"})
    if extra:
        await tenancy.update_tenant(pool, payload.guild_id, extra)
    tenant = await tenancy.get_tenant(pool, payload.guild_id)
    await emit(pool, CHANNEL_CONFIG_CHANGED, str(payload.guild_id))
    return tenant


@router.get("/{guild_id}", response_model=TenantOut)
async def get_tenant(
    guild_id: int,
    pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool),
) -> dict[str, Any]:
    tenant = await tenancy.get_tenant(pool, guild_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    return tenant


@router.patch("/{guild_id}", response_model=TenantOut)
async def update_tenant(
    guild_id: int,
    payload: TenantUpdate,
    pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool),
) -> dict[str, Any]:
    if await tenancy.get_tenant(pool, guild_id) is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    updated = await tenancy.update_tenant(pool, guild_id, payload.model_dump(exclude_unset=True))
    # Status / enablement may change serviceability — nudge the bot to re-evaluate.
    await emit(pool, CHANNEL_CONFIG_CHANGED, str(guild_id))
    return updated


@router.delete("/{guild_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tenant(
    guild_id: int,
    leave: bool = Query(default=True, description="Also make the bot leave the guild."),
    pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool),
) -> Response:
    """Hard-delete a tenant and all its data. Irreversible."""
    if leave:
        # Tell the bot to leave BEFORE the row (and its cascade) disappears.
        await emit(pool, CHANNEL_BOT_COMMAND, json.dumps({"action": "leave", "guild_id": guild_id}))
    deleted = await tenancy.delete_tenant(pool, guild_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{guild_id}/discord-meta")
async def discord_meta(
    guild_id: int,
    pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool),
    settings: ApiSettings = Depends(get_settings),
) -> dict[str, Any]:
    """Channels + roles visible to the bot in this guild, for dashboard pickers."""
    if not settings.discord_token:
        raise HTTPException(status_code=503, detail="DISCORD_TOKEN not configured for the API.")
    if await tenancy.get_tenant(pool, guild_id) is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    client = DiscordRest(settings.discord_token)
    try:
        channels = await client.get_guild_channels(guild_id)
        roles = await client.get_guild_roles(guild_id)
    except DiscordRestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {"channels": channels, "roles": roles}
