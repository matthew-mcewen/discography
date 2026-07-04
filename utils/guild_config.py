"""
Per-guild config cache.

Cogs call ``await bot.get_guild_config(guild_id)`` on every interaction, so the
lookup must be cheap. This cache holds materialised ``GuildConfig`` objects with
a short TTL; the API invalidates entries the instant it changes them via the
``guild_config_changed`` NOTIFY channel (see utils/pg_listener), so edits from
the dashboard take effect within a heartbeat rather than after the TTL.

A ``None`` result (guild not yet configured) is cached too, to avoid hammering
the DB for guilds the bot is in but that haven't finished setup.
"""
from __future__ import annotations

import logging
import time

import psycopg_pool

import db.tenancy as tenancy
from utils.config import GuildConfig

logger = logging.getLogger(__name__)


class _Entry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: GuildConfig | None, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at


class GuildConfigCache:
    def __init__(self, pool: psycopg_pool.AsyncConnectionPool, ttl: int = 60) -> None:
        self._pool = pool
        self._ttl = ttl
        self._entries: dict[int, _Entry] = {}

    async def get(self, guild_id: int) -> GuildConfig | None:
        entry = self._entries.get(guild_id)
        now = time.monotonic()
        if entry is not None and entry.expires_at > now:
            return entry.value

        value = await self._load(guild_id)
        self._entries[guild_id] = _Entry(value, now + self._ttl)
        return value

    async def _load(self, guild_id: int) -> GuildConfig | None:
        # A guild only yields a usable config if it is BOTH serviceable
        # (active subscription, bot enabled, not left) AND configured. Folding
        # both here means a single cached lookup gates every event handler.
        tenant = await tenancy.get_tenant(self._pool, guild_id)
        if not tenancy.is_serviceable(tenant):
            return None
        config_row = await tenancy.get_guild_config_row(self._pool, guild_id)
        if config_row is None:
            return None
        multipliers = await tenancy.get_guild_multipliers(self._pool, guild_id)
        return GuildConfig.from_rows(guild_id, config_row, multipliers)

    def invalidate(self, guild_id: int) -> None:
        self._entries.pop(guild_id, None)

    def invalidate_all(self) -> None:
        self._entries.clear()

    def handle_notify(self, payload: str) -> None:
        """NOTIFY handler for the ``guild_config_changed`` channel."""
        try:
            guild_id = int(payload)
        except (TypeError, ValueError):
            logger.warning("guild_config_changed with non-int payload: %r", payload)
            return
        self.invalidate(guild_id)
        logger.info("Invalidated cached config for guild %s (NOTIFY)", guild_id)
