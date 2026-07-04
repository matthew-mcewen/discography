"""
Dynamic per-guild permission checks for slash commands.

In the single-tenant bot, admin/role checks captured a role id from config.yaml
at cog-load time. That can't work when every guild has different role ids, so
these checks resolve the *current* guild's config at call time instead.

By the time a command check runs, the bot's tree-level gate
(DiscographyBot._app_command_gate) has already confirmed the guild is
serviceable and stashed its GuildConfig in ``interaction.extras['config']``.
"""
from __future__ import annotations

import discord
from discord import app_commands

from utils.config import GuildConfig


class FeatureDisabled(app_commands.CheckFailure):
    """Raised when a command's gating role isn't configured for this guild."""


async def get_config(interaction: discord.Interaction) -> GuildConfig | None:
    cfg = interaction.extras.get("config")
    if cfg is not None:
        return cfg
    return await interaction.client.get_guild_config(interaction.guild_id)


def require_role(attr: str):
    """
    Build a check requiring the user to have the guild's configured role for
    ``attr`` (a field name on RoleConfig, e.g. 'dry_run_admin').
    """

    async def predicate(interaction: discord.Interaction) -> bool:
        config = await get_config(interaction)
        if config is None:
            raise app_commands.CheckFailure("not_configured")
        role_id = getattr(config.roles, attr)
        if not role_id:
            raise FeatureDisabled(f"{attr} role is not configured")
        if any(r.id == role_id for r in interaction.user.roles):
            return True
        raise app_commands.MissingRole(role_id)

    return app_commands.check(predicate)
