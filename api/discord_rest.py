"""
Thin async Discord REST client used by the API to read what the bot can see in a
guild (channels + roles), so the dashboard can populate config pickers. Uses the
same bot token as the gateway process.
"""
from __future__ import annotations

import httpx

API_BASE = "https://discord.com/api/v10"


class DiscordRestError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


class DiscordRest:
    def __init__(self, token: str) -> None:
        self._headers = {"Authorization": f"Bot {token}"}

    async def _get(self, path: str) -> list[dict] | dict:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{API_BASE}{path}", headers=self._headers)
        if resp.status_code == 404:
            raise DiscordRestError(404, "Guild not found or bot is not a member.")
        if resp.status_code in (401, 403):
            raise DiscordRestError(resp.status_code, "Bot lacks access to this guild.")
        resp.raise_for_status()
        return resp.json()

    async def get_guild_channels(self, guild_id: int) -> list[dict]:
        data = await self._get(f"/guilds/{guild_id}/channels")
        # 0=text, 2=voice, 4=category, 5=announcement, 15=forum ...
        return [
            {"id": str(c["id"]), "name": c.get("name"), "type": c.get("type"), "position": c.get("position")}
            for c in data
        ]

    async def get_guild_roles(self, guild_id: int) -> list[dict]:
        data = await self._get(f"/guilds/{guild_id}/roles")
        return [
            {
                "id": str(r["id"]),
                "name": r.get("name"),
                "position": r.get("position"),
                "managed": r.get("managed", False),
            }
            for r in data
        ]
