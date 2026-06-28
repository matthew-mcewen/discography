from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import db.database as db
from utils.config import BotConfig

if TYPE_CHECKING:
    from main import DiscographyBot

logger = logging.getLogger(__name__)

RANK_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


class CommandsCog(commands.Cog, name="CommandsCog"):
    def __init__(self, bot: DiscographyBot, config: BotConfig) -> None:
        self.bot = bot
        self.config = config

    @property
    def _pool(self):
        return self.bot.db_pool

    # ------------------------------------------------------------------
    # /points  (slash)  +  !points  (prefix)
    # ------------------------------------------------------------------

    @app_commands.command(name="points", description="Check your current points balance.")
    async def slash_points(self, interaction: discord.Interaction) -> None:
        pts = await db.get_user_points(self._pool, interaction.user.id)
        if pts is None:
            msg = (
                f"You haven't made any submissions yet, so you have 0 "
                f"{self.config.points.name}."
            )
        else:
            msg = (
                f"You have **{pts}** {self.config.points.name} "
                f"{self.config.points.emoji}."
            )
        await interaction.response.send_message(msg, ephemeral=True)

    @commands.command(name="points")
    async def prefix_points(self, ctx: commands.Context) -> None:
        pts = await db.get_user_points(self._pool, ctx.author.id)
        if pts is None:
            msg = (
                f"You haven't made any submissions yet, so you have 0 "
                f"{self.config.points.name}."
            )
        else:
            msg = (
                f"You have **{pts}** {self.config.points.name} "
                f"{self.config.points.emoji}."
            )
        await ctx.reply(msg)

    # ------------------------------------------------------------------
    # /leaderboard  — show current standings ephemerally
    # ------------------------------------------------------------------

    @app_commands.command(name="leaderboard", description="View the current weekly leaderboard.")
    async def slash_leaderboard(self, interaction: discord.Interaction) -> None:
        embed = await self.update_leaderboard(dry_run=True)
        if embed is None:
            await interaction.response.send_message("No submissions yet this week.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Leaderboard maintenance — called by competition tasks
    # ------------------------------------------------------------------

    async def update_leaderboard(self, dry_run: bool = False) -> discord.Embed | None:
        """
        Build the leaderboard embed and edit the pinned message in-place.
        Returns the embed (useful for dry-run preview or /leaderboard command).
        """
        period = await db.ensure_active_period(self._pool)
        entries = await db.get_period_leaderboard(self._pool, period["id"], limit=10)

        embed = self._build_leaderboard_embed(period, entries)

        if dry_run:
            return embed

        channel = self.bot.get_channel(self.config.channels.leaderboard)
        if channel is None:
            logger.error("Leaderboard channel %s not found.", self.config.channels.leaderboard)
            return embed

        stored_id = await db.get_leaderboard_message_id(self._pool, period["id"])

        if stored_id is not None:
            try:
                msg = await channel.fetch_message(stored_id)
                await msg.edit(embed=embed)
                return embed
            except discord.NotFound:
                logger.warning("Leaderboard message %s was deleted; reposting.", stored_id)
            except discord.HTTPException as exc:
                logger.error("Failed to edit leaderboard message: %s", exc)
                return embed

        # Post a new leaderboard message
        try:
            msg = await channel.send(embed=embed)
            try:
                await msg.pin()
            except discord.HTTPException as exc:
                if exc.code == 30003:
                    logger.warning("Pin limit reached in leaderboard channel; message not pinned.")
                else:
                    raise
            await db.set_leaderboard_message_id(self._pool, period["id"], msg.id)
        except discord.Forbidden:
            logger.error("Missing permissions to post/pin in leaderboard channel.")
        except discord.HTTPException as exc:
            logger.error("Failed to post leaderboard message: %s", exc)

        return embed

    def _build_leaderboard_embed(
        self,
        period: dict[str, Any],
        entries: list[dict[str, Any]],
    ) -> discord.Embed:
        start = period["start_time"]
        end = period["end_time"]
        period_range = (
            f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
            if hasattr(start, "strftime")
            else f"{start} – {end}"
        )

        lines = []

        # Always show the top 3 rows; fill missing places with a placeholder
        for rank in range(1, 4):
            medal = RANK_MEDALS[rank]
            if len(entries) >= rank:
                entry = entries[rank - 1]
                lines.append(
                    f"{medal} <@{entry['user_id']}> — {entry['net_trophy_count']} 🏆"
                )
            else:
                lines.append(f"{medal} *crickets* 🏏")

        # Ranks 4–10 only if users exist
        for i, entry in enumerate(entries[3:10], start=4):
            lines.append(f"{i}. <@{entry['user_id']}> — {entry['net_trophy_count']} 🏆")

        embed = discord.Embed(
            title="🏆 Weekly Leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.add_field(name="Period", value=period_range, inline=False)
        embed.set_footer(text="Last updated")
        embed.timestamp = discord.utils.utcnow()
        return embed


async def setup(bot: DiscographyBot) -> None:
    await bot.add_cog(CommandsCog(bot, bot.config))
