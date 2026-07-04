"""
Control-plane database access — tenants, per-guild configuration, billing state,
and API keys.

These tables are NOT under row-level security: they describe the tenants
themselves and are managed by the bot's lifecycle handlers and the management
API. All access uses plain pooled connections (no app.current_guild GUC).
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime
from typing import Any

import psycopg_pool

logger = logging.getLogger(__name__)

# Tenant statuses in which the bot actively serves the guild.
SERVICEABLE_STATUSES = ("trialing", "active", "past_due")

# Columns the API/config layer is allowed to write to guild_configs.
_CONFIG_COLUMNS = frozenset(
    {
        "submissions_channel_id",
        "announcements_channel_id",
        "bot_commands_channel_id",
        "leaderboard_channel_id",
        "reaction_roles_channel_id",
        "notification_role_id",
        "dry_run_admin_role_id",
        "point_awarder_role_id",
        "predict_starter_role_id",
        "reaction_role_admin_role_id",
        "points_name",
        "points_emoji",
        "competition_mode",
        "coinflip_max_daily_flips",
        "predictions_seed_liquidity",
        "schedule_timezone",
    }
)

_TENANT_COLUMNS = frozenset(
    {
        "name",
        "owner_user_id",
        "status",
        "plan",
        "bot_enabled",
        "stripe_customer_id",
        "stripe_subscription_id",
        "current_period_end",
        "trial_ends_at",
    }
)


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------

async def get_tenant(pool: psycopg_pool.AsyncConnectionPool, guild_id: int) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM tenants WHERE guild_id = %s", (guild_id,))
            row = await cur.fetchone()
    return dict(row) if row else None


async def list_tenants(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if status:
                await cur.execute(
                    "SELECT * FROM tenants WHERE status = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    (status, limit, offset),
                )
            else:
                await cur.execute(
                    "SELECT * FROM tenants ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    (limit, offset),
                )
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def upsert_tenant_on_join(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    name: str | None,
    owner_user_id: int | None,
) -> dict[str, Any]:
    """
    Called when the bot joins (or is reconciled into) a guild. Creates the tenant
    in ``pending_setup`` if new; on re-join clears ``left_at`` and refreshes the
    cached name/owner. Never downgrades an existing paid status.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO tenants (guild_id, name, owner_user_id, status)
                VALUES (%s, %s, %s, 'pending_setup')
                ON CONFLICT (guild_id) DO UPDATE
                    SET name          = COALESCE(EXCLUDED.name, tenants.name),
                        owner_user_id = COALESCE(EXCLUDED.owner_user_id, tenants.owner_user_id),
                        left_at       = NULL,
                        updated_at    = now()
                RETURNING *
                """,
                (guild_id, name, owner_user_id),
            )
            row = await cur.fetchone()
    return dict(row)


async def mark_tenant_left(pool: psycopg_pool.AsyncConnectionPool, guild_id: int) -> None:
    """Bot was removed from the guild. Soft state only — data is retained."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE tenants SET left_at = now(), updated_at = now() WHERE guild_id = %s",
            (guild_id,),
        )


async def update_tenant(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    fields: dict[str, Any],
) -> dict[str, Any] | None:
    """Patch whitelisted tenant columns. Returns the updated row."""
    updates = {k: v for k, v in fields.items() if k in _TENANT_COLUMNS}
    if not updates:
        return await get_tenant(pool, guild_id)

    set_clause = ", ".join(f"{col} = %s" for col in updates)
    values = list(updates.values())
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE tenants SET {set_clause}, updated_at = now() WHERE guild_id = %s RETURNING *",
                (*values, guild_id),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def mark_provisioned(pool: psycopg_pool.AsyncConnectionPool, guild_id: int) -> None:
    """
    Stamp provisioned_at the first time a guild has a usable config, and promote
    it out of pending_setup into trialing so the bot begins serving it.
    """
    async with pool.connection() as conn:
        await conn.execute(
            """
            UPDATE tenants
            SET provisioned_at = COALESCE(provisioned_at, now()),
                status = CASE WHEN status = 'pending_setup' THEN 'trialing' ELSE status END,
                updated_at = now()
            WHERE guild_id = %s
            """,
            (guild_id,),
        )


async def delete_tenant(pool: psycopg_pool.AsyncConnectionPool, guild_id: int) -> bool:
    """
    Hard-delete a tenant and ALL its data (cascades through every guild-scoped
    table). Irreversible — used for account deletion / GDPR erasure.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM tenants WHERE guild_id = %s RETURNING guild_id", (guild_id,))
            row = await cur.fetchone()
    return row is not None


async def list_serviceable_guild_ids(pool: psycopg_pool.AsyncConnectionPool) -> list[int]:
    """Guilds the bot should actively run scheduled work for."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT guild_id FROM tenants
                WHERE bot_enabled = TRUE
                  AND provisioned_at IS NOT NULL
                  AND left_at IS NULL
                  AND status = ANY(%s)
                """,
                (list(SERVICEABLE_STATUSES),),
            )
            rows = await cur.fetchall()
    return [r["guild_id"] for r in rows]


def is_serviceable(tenant: dict[str, Any]) -> bool:
    """In-memory predicate mirroring list_serviceable_guild_ids (avoids a round trip)."""
    return bool(
        tenant
        and tenant.get("bot_enabled")
        and tenant.get("provisioned_at") is not None
        and tenant.get("left_at") is None
        and tenant.get("status") in SERVICEABLE_STATUSES
    )


# ---------------------------------------------------------------------------
# Guild configuration
# ---------------------------------------------------------------------------

async def get_guild_config_row(pool: psycopg_pool.AsyncConnectionPool, guild_id: int) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM guild_configs WHERE guild_id = %s", (guild_id,))
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_guild_multipliers(pool: psycopg_pool.AsyncConnectionPool, guild_id: int) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT role_id, multiplier FROM guild_multipliers WHERE guild_id = %s ORDER BY id",
                (guild_id,),
            )
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def upsert_guild_config(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """
    Create or patch a guild's config row. Only whitelisted columns are written.
    Requires the tenant row to already exist (FK). Returns the full config row.
    """
    updates = {k: v for k, v in fields.items() if k in _CONFIG_COLUMNS}
    async with pool.connection() as conn:
        if updates:
            cols = list(updates.keys())
            placeholders = ", ".join(["%s"] * len(cols))
            col_list = ", ".join(cols)
            set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
            await conn.execute(
                f"""
                INSERT INTO guild_configs (guild_id, {col_list})
                VALUES (%s, {placeholders})
                ON CONFLICT (guild_id) DO UPDATE
                    SET {set_clause}, updated_at = now()
                """,
                (guild_id, *updates.values()),
            )
        else:
            # Ensure a row exists even with all-default values.
            await conn.execute(
                "INSERT INTO guild_configs (guild_id) VALUES (%s) ON CONFLICT (guild_id) DO NOTHING",
                (guild_id,),
            )
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM guild_configs WHERE guild_id = %s", (guild_id,))
            row = await cur.fetchone()
    return dict(row)


async def set_guild_multipliers(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    items: list[dict[str, Any]],
) -> None:
    """Replace the full set of point multipliers for a guild. items = [{role_id, multiplier}]."""
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM guild_multipliers WHERE guild_id = %s", (guild_id,))
            for item in items:
                await conn.execute(
                    """
                    INSERT INTO guild_multipliers (guild_id, role_id, multiplier)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (guild_id, role_id) DO UPDATE SET multiplier = EXCLUDED.multiplier
                    """,
                    (guild_id, int(item["role_id"]), float(item["multiplier"])),
                )


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

_KEY_PREFIX = "dscg_"


def _hash_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()


async def create_api_key(
    pool: psycopg_pool.AsyncConnectionPool,
    name: str,
    scope: str = "admin",
    guild_id: int | None = None,
) -> dict[str, Any]:
    """
    Create an API key and return the created record PLUS the full secret under
    the ``key`` field (shown once — only the hash is persisted). ``scope`` is
    'admin' (full control plane) or 'tenant' (single guild).
    """
    if scope not in ("admin", "tenant"):
        raise ValueError("scope must be 'admin' or 'tenant'")
    secret = _KEY_PREFIX + secrets.token_urlsafe(32)
    prefix = secret[: len(_KEY_PREFIX) + 6]
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO api_keys (name, key_prefix, key_hash, scope, guild_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, name, key_prefix, scope, guild_id, created_at, last_used_at, revoked_at
                """,
                (name, prefix, _hash_key(secret), scope, guild_id),
            )
            row = await cur.fetchone()
    return {**dict(row), "key": secret}


async def verify_api_key(pool: psycopg_pool.AsyncConnectionPool, full_key: str) -> dict[str, Any] | None:
    """Return the active key record for ``full_key`` (touching last_used_at), or None."""
    key_hash = _hash_key(full_key)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, name, scope, guild_id, revoked_at FROM api_keys WHERE key_hash = %s",
                (key_hash,),
            )
            row = await cur.fetchone()
            if row is None or row["revoked_at"] is not None:
                return None
            await cur.execute("UPDATE api_keys SET last_used_at = now() WHERE id = %s", (row["id"],))
    return dict(row)


async def list_api_keys(pool: psycopg_pool.AsyncConnectionPool) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, name, key_prefix, scope, guild_id, created_at, last_used_at, revoked_at
                FROM api_keys ORDER BY created_at DESC
                """
            )
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def revoke_api_key(pool: psycopg_pool.AsyncConnectionPool, key_id: int) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE api_keys SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL RETURNING id",
                (key_id,),
            )
            row = await cur.fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Stripe billing
# ---------------------------------------------------------------------------

async def has_stripe_event(pool: psycopg_pool.AsyncConnectionPool, event_id: str) -> bool:
    """True if this Stripe event has already been processed."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM stripe_events WHERE id = %s", (event_id,))
            return await cur.fetchone() is not None


async def record_stripe_event(pool: psycopg_pool.AsyncConnectionPool, event_id: str, event_type: str) -> bool:
    """
    Record a Stripe event id for idempotency. Returns True if this is the first
    time we've seen it (i.e. the caller should process it), False if duplicate.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO stripe_events (id, type) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING RETURNING id",
                (event_id, event_type),
            )
            row = await cur.fetchone()
    return row is not None


async def find_tenant_by_customer(pool: psycopg_pool.AsyncConnectionPool, customer_id: str) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM tenants WHERE stripe_customer_id = %s", (customer_id,))
            row = await cur.fetchone()
    return dict(row) if row else None


async def find_tenant_by_subscription(
    pool: psycopg_pool.AsyncConnectionPool, subscription_id: str
) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM tenants WHERE stripe_subscription_id = %s", (subscription_id,))
            row = await cur.fetchone()
    return dict(row) if row else None


async def apply_subscription_state(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    *,
    status: str | None = None,
    plan: str | None = None,
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
    current_period_end: datetime | None = None,
) -> None:
    """Apply a billing state change from a Stripe webhook to a tenant."""
    fields: dict[str, Any] = {}
    if status is not None:
        fields["status"] = status
    if plan is not None:
        fields["plan"] = plan
    if stripe_customer_id is not None:
        fields["stripe_customer_id"] = stripe_customer_id
    if stripe_subscription_id is not None:
        fields["stripe_subscription_id"] = stripe_subscription_id
    if current_period_end is not None:
        fields["current_period_end"] = current_period_end
    if fields:
        await update_tenant(pool, guild_id, fields)
