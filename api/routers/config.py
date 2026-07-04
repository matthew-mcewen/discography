"""Per-guild configuration endpoints (replaces config.yaml)."""
from __future__ import annotations

from typing import Any

import psycopg_pool
from fastapi import APIRouter, Depends, HTTPException

import db.tenancy as tenancy
from api.deps import get_pool, require_admin
from api.schemas import ConfigUpdate
from utils.pg_listener import CHANNEL_CONFIG_CHANGED, emit

router = APIRouter(prefix="/v1/tenants/{guild_id}/config", tags=["config"], dependencies=[Depends(require_admin)])

_DEFAULTS: dict[str, Any] = {
    "submissions_channel_id": None,
    "announcements_channel_id": None,
    "bot_commands_channel_id": None,
    "leaderboard_channel_id": None,
    "reaction_roles_channel_id": None,
    "notification_role_id": None,
    "dry_run_admin_role_id": None,
    "point_awarder_role_id": None,
    "predict_starter_role_id": None,
    "reaction_role_admin_role_id": None,
    "points_name": "Points",
    "points_emoji": "🏆",
    "competition_mode": "dedicated",
    "coinflip_max_daily_flips": 5,
    "predictions_seed_liquidity": 100.0,
    "schedule_timezone": "UTC",
}

# The bot begins serving a guild once these channels are set.
_REQUIRED_FOR_SERVICE = ("submissions_channel_id", "announcements_channel_id", "leaderboard_channel_id")


def _is_minimally_configured(row: dict[str, Any]) -> bool:
    return all(row.get(col) for col in _REQUIRED_FOR_SERVICE)


async def _config_response(pool, guild_id: int) -> dict[str, Any]:
    row = await tenancy.get_guild_config_row(pool, guild_id)
    merged = dict(_DEFAULTS)
    if row:
        merged.update({k: v for k, v in row.items() if k in _DEFAULTS})
    merged["guild_id"] = guild_id
    merged["multipliers"] = await tenancy.get_guild_multipliers(pool, guild_id)
    merged["configured"] = _is_minimally_configured(merged)
    return merged


@router.get("")
async def get_config(
    guild_id: int,
    pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool),
) -> dict[str, Any]:
    if await tenancy.get_tenant(pool, guild_id) is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    return await _config_response(pool, guild_id)


@router.put("")
async def put_config(
    guild_id: int,
    payload: ConfigUpdate,
    pool: psycopg_pool.AsyncConnectionPool = Depends(get_pool),
) -> dict[str, Any]:
    if await tenancy.get_tenant(pool, guild_id) is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    data = payload.model_dump(exclude_unset=True)
    multipliers = data.pop("multipliers", None)

    if data:
        await tenancy.upsert_guild_config(pool, guild_id, data)
    else:
        # Ensure a row exists even if only multipliers were sent.
        await tenancy.upsert_guild_config(pool, guild_id, {})

    if multipliers is not None:
        await tenancy.set_guild_multipliers(pool, guild_id, multipliers)

    # Promote out of pending_setup once the essentials are present.
    row = await tenancy.get_guild_config_row(pool, guild_id)
    if row and _is_minimally_configured(row):
        await tenancy.mark_provisioned(pool, guild_id)

    # Push the change to the bot immediately (bypasses the cache TTL).
    await emit(pool, CHANNEL_CONFIG_CHANGED, str(guild_id))

    return await _config_response(pool, guild_id)
