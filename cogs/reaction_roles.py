from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import db.database as db
from utils.config import BotConfig

if TYPE_CHECKING:
    from main import DiscographyBot

logger = logging.getLogger(__name__)

PANEL_MESSAGE_SETTING_KEY = "reaction_role_panel_message_id"


def _emoji_key(emoji: discord.PartialEmoji) -> str:
    """Canonical match key for an emoji: stable across custom-emote renames."""
    return f"id:{emoji.id}" if emoji.id else f"name:{emoji.name}"


class ReactionRolesCog(commands.Cog, name="ReactionRolesCog"):
    def __init__(self, bot: DiscographyBot, config: BotConfig) -> None:
        self.bot = bot
        self.config = config
        self._synced_on_ready = False

    @property
    def _pool(self):
        return self.bot.db_pool

    # ------------------------------------------------------------------
    # /reactionrole-add
    # ------------------------------------------------------------------

    @app_commands.command(
        name="reactionrole-add",
        description="[Admin] Add or update a reaction role: react with an emoji to get a role.",
    )
    @app_commands.describe(
        role="The role to grant",
        emoji="The emoji users react with (unicode emoji or a custom server emote)",
    )
    async def reactionrole_add(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        emoji: str,
    ) -> None:
        try:
            partial_emoji = discord.PartialEmoji.from_str(emoji.strip())
        except Exception:
            await interaction.response.send_message(
                "That doesn't look like a valid emoji. Use a unicode emoji or a custom server emote.",
                ephemeral=True,
            )
            return

        if role.is_default() or role.managed:
            await interaction.response.send_message(
                "That role can't be assigned via reactions (it's managed or @everyone).",
                ephemeral=True,
            )
            return

        if interaction.guild.me.top_role <= role:
            await interaction.response.send_message(
                "I can't assign that role — it's above or equal to my highest role. "
                "Move my role above it in Server Settings → Roles.",
                ephemeral=True,
            )
            return

        emoji_key = _emoji_key(partial_emoji)
        emoji_str = str(partial_emoji)

        try:
            await db.upsert_reaction_role(self._pool, role.id, emoji_key, emoji_str)
        except ValueError:
            await interaction.response.send_message(
                f"{emoji_str} is already assigned to a different role. Remove that pairing first.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ {emoji_str} now grants {role.mention}.", ephemeral=True
        )
        await self.sync_panel()

    # ------------------------------------------------------------------
    # /reactionrole-remove
    # ------------------------------------------------------------------

    @app_commands.command(
        name="reactionrole-remove",
        description="[Admin] Remove a reaction role pairing.",
    )
    @app_commands.describe(role="The role to stop granting via reactions")
    async def reactionrole_remove(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
    ) -> None:
        removed = await db.remove_reaction_role(self._pool, role.id)
        if not removed:
            await interaction.response.send_message(
                f"{role.mention} isn't set up as a reaction role.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ Removed reaction role for {role.mention}.", ephemeral=True
        )
        await self.sync_panel()

    # ------------------------------------------------------------------
    # /reactionrole-list
    # ------------------------------------------------------------------

    @app_commands.command(
        name="reactionrole-list",
        description="[Admin] List all configured reaction roles.",
    )
    async def reactionrole_list(self, interaction: discord.Interaction) -> None:
        pairs = await db.get_reaction_roles(self._pool)
        if not pairs:
            await interaction.response.send_message("No reaction roles configured.", ephemeral=True)
            return

        lines = [f"{p['emoji_str']} → <@&{p['role_id']}>" for p in pairs]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ------------------------------------------------------------------
    # Panel embed — posted/edited/removed based on configured pairs
    # ------------------------------------------------------------------

    async def sync_panel(self) -> None:
        """Post, update, or remove the reaction-role panel embed based on configured pairs."""
        channel_id = self.config.channels.reaction_roles
        if not channel_id:
            logger.warning("reaction_roles.sync_panel: no channels.reaction_roles configured.")
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            logger.error("Reaction roles channel %s not found.", channel_id)
            return

        pairs = await db.get_reaction_roles(self._pool)
        stored_id = await db.get_setting(self._pool, PANEL_MESSAGE_SETTING_KEY)

        if not pairs:
            if stored_id is not None:
                try:
                    msg = await channel.fetch_message(int(stored_id))
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                await db.delete_setting(self._pool, PANEL_MESSAGE_SETTING_KEY)
            return

        embed = self._build_panel_embed(pairs)

        msg: discord.Message | None = None
        if stored_id is not None:
            try:
                msg = await channel.fetch_message(int(stored_id))
                await msg.edit(embed=embed)
            except discord.NotFound:
                msg = None
            except discord.HTTPException as exc:
                logger.error("Failed to edit reaction role panel: %s", exc)
                return

        if msg is None:
            try:
                msg = await channel.send(embed=embed)
            except discord.Forbidden:
                logger.error("Missing permissions to post in reaction roles channel %s.", channel_id)
                return
            await db.set_setting(self._pool, PANEL_MESSAGE_SETTING_KEY, str(msg.id))

        await self._sync_panel_reactions(msg, pairs)

    @staticmethod
    def _build_panel_embed(pairs: list[dict]) -> discord.Embed:
        lines = [f"{p['emoji_str']} — <@&{p['role_id']}>" for p in pairs]
        embed = discord.Embed(
            title="Reaction Roles",
            description=(
                "React below to pick up a role. Remove your reaction to remove the role.\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.blurple(),
        )
        return embed

    @staticmethod
    async def _sync_panel_reactions(msg: discord.Message, pairs: list[dict]) -> None:
        wanted_keys = {p["emoji_key"] for p in pairs}

        existing: dict[str, discord.Reaction] = {}
        for reaction in msg.reactions:
            emoji = reaction.emoji
            partial = emoji if isinstance(emoji, discord.PartialEmoji) else discord.PartialEmoji.from_str(str(emoji))
            existing[_emoji_key(partial)] = reaction

        # Remove reactions that are no longer configured
        for key, reaction in existing.items():
            if key not in wanted_keys:
                try:
                    await msg.clear_reaction(reaction.emoji)
                except (discord.Forbidden, discord.HTTPException):
                    pass

        # Add reactions for newly configured pairs
        for p in pairs:
            if p["emoji_key"] not in existing:
                try:
                    await msg.add_reaction(discord.PartialEmoji.from_str(p["emoji_str"]))
                except (discord.Forbidden, discord.HTTPException):
                    pass

    # ------------------------------------------------------------------
    # Recreate the panel on startup if it's missing (e.g. deleted manually)
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._synced_on_ready:
            return
        self._synced_on_ready = True
        await self.sync_panel()

    # ------------------------------------------------------------------
    # Raw reaction listeners — assign/remove role on react/unreact
    # ------------------------------------------------------------------

    async def _panel_message_id(self) -> int | None:
        stored_id = await db.get_setting(self._pool, PANEL_MESSAGE_SETTING_KEY)
        return int(stored_id) if stored_id is not None else None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.user_id == self.bot.user.id:
            return
        if payload.channel_id != self.config.channels.reaction_roles:
            return

        panel_id = await self._panel_message_id()
        if panel_id is None or payload.message_id != panel_id:
            return

        pair = await db.get_reaction_role_by_emoji_key(self._pool, _emoji_key(payload.emoji))
        if pair is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = payload.member or guild.get_member(payload.user_id)
        if member is None:
            return
        role = guild.get_role(pair["role_id"])
        if role is None:
            return

        try:
            await member.add_roles(role, reason="Reaction role")
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Failed to add role %s to member %s", role.id, member.id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.user_id == self.bot.user.id:
            return
        if payload.channel_id != self.config.channels.reaction_roles:
            return

        panel_id = await self._panel_message_id()
        if panel_id is None or payload.message_id != panel_id:
            return

        pair = await db.get_reaction_role_by_emoji_key(self._pool, _emoji_key(payload.emoji))
        if pair is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.NotFound:
                return
        role = guild.get_role(pair["role_id"])
        if role is None:
            return

        try:
            await member.remove_roles(role, reason="Reaction role removed")
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Failed to remove role %s from member %s", role.id, member.id)

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingRole):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "You don't have permission to use this command.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "You don't have permission to use this command.",
                    ephemeral=True,
                )
        else:
            logger.exception("Unhandled reaction roles command error: %s", error)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "An error occurred. Check the bot logs.",
                    ephemeral=True,
                )


async def setup(bot: DiscographyBot) -> None:
    cog = ReactionRolesCog(bot, bot.config)

    admin_role_id = bot.config.roles.reaction_role_admin

    async def reaction_role_admin_check(interaction: discord.Interaction) -> bool:
        if any(r.id == admin_role_id for r in interaction.user.roles):
            return True
        raise app_commands.MissingRole(admin_role_id)

    restricted_commands = {"reactionrole-add", "reactionrole-remove", "reactionrole-list"}
    for cmd in cog.__cog_app_commands__:
        if cmd.name in restricted_commands:
            cmd.add_check(reaction_role_admin_check)

    await bot.add_cog(cog)
