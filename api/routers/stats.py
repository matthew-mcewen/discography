"""Read-only per-guild stats + leaderboard for dashboards."""
from __future__ import annotations

from typing import Any

import psycopg_pool
from fastapi import APIRouter, Depends, HTTPException, Query

import db.database as db
import db.tenancy as tenancy
from api.deps import get_pool, require_admin

router = APIRouter(prefix="/v1/tenants/{guild_id}", tags=["stats"], dependencies=[Depends(require_admin)])


@router.get("/stats")
async def get_stats(
    guild_id: int,
    pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool),
) -> dict[str, Any]:
    tenant = await tenancy.get_tenant(pool, guild_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    stats = await db.get_guild_stats(pool, guild_id)
    return {
        "guild_id": guild_id,
        "status": tenant["status"],
        "plan": tenant["plan"],
        "bot_enabled": tenant["bot_enabled"],
        "serviceable": tenancy.is_serviceable(tenant),
        **stats,
    }


@router.get("/leaderboard")
async def get_leaderboard(
    guild_id: int,
    limit: int = Query(default=10, ge=1, le=100),
    pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool),
) -> dict[str, Any]:
    if await tenancy.get_tenant(pool, guild_id) is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    period = await db.get_any_unannounced_period(pool, guild_id)
    if period is None:
        return {"guild_id": guild_id, "period": None, "entries": []}
    entries = await db.get_period_leaderboard(pool, guild_id, period["id"], limit=limit)
    return {"guild_id": guild_id, "period": period, "entries": entries}
