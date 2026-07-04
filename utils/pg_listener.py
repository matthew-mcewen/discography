"""
Postgres LISTEN/NOTIFY plumbing — the decoupled channel between the API and the
bot (they never talk HTTP to each other; they pass messages through the DB).

Channels:
  * guild_config_changed — payload = "<guild_id>". The API emits this after it
    writes a guild's config so the bot invalidates its cached GuildConfig
    immediately instead of waiting for the TTL.
  * bot_command — payload = JSON {"action": ..., "guild_id": ...}. The API emits
    control commands the bot must execute with its gateway connection, e.g.
    leaving a guild after account deletion.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable

import psycopg
import psycopg_pool

logger = logging.getLogger(__name__)

CHANNEL_CONFIG_CHANGED = "guild_config_changed"
CHANNEL_BOT_COMMAND = "bot_command"

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

Handler = Callable[[str], Awaitable[None] | None]


async def emit(pool: psycopg_pool.AsyncConnectionPool, channel: str, payload: str) -> None:
    """Send a NOTIFY on ``channel`` with ``payload`` using a pooled connection."""
    async with pool.connection() as conn:
        await conn.execute("SELECT pg_notify(%s, %s)", (channel, payload))


class PgListener:
    """
    A single long-lived autocommit connection that LISTENs on one or more
    channels and dispatches payloads to registered async/sync handlers.
    Reconnects with backoff if the connection drops.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._handlers: dict[str, Handler] = {}
        self._task: asyncio.Task | None = None
        self._conn: psycopg.AsyncConnection | None = None
        self._stopped = False

    def on(self, channel: str, handler: Handler) -> None:
        if not _IDENT_RE.match(channel):
            raise ValueError(f"Unsafe NOTIFY channel name: {channel!r}")
        self._handlers[channel] = handler

    async def start(self) -> None:
        self._stopped = False
        self._task = asyncio.create_task(self._run(), name="pg-listener")

    async def stop(self) -> None:
        self._stopped = True
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopped:
            try:
                self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
                for channel in self._handlers:
                    await self._conn.execute(f'LISTEN "{channel}"')
                logger.info("PgListener listening on: %s", ", ".join(self._handlers))
                backoff = 1.0
                async for note in self._conn.notifies():
                    if self._stopped:
                        break
                    handler = self._handlers.get(note.channel)
                    if handler is None:
                        continue
                    try:
                        result = handler(note.payload)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception("PgListener handler for %s failed", note.channel)
            except asyncio.CancelledError:
                break
            except Exception:
                if self._stopped:
                    break
                logger.exception("PgListener connection error; reconnecting in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                if self._conn is not None:
                    try:
                        await self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
