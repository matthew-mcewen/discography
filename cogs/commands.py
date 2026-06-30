from __future__ import annotations

import logging
import random
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

    # ------------------------------------------------------------------
    # /help
    # ------------------------------------------------------------------

    @app_commands.command(name="help", description="List all commands available to you.")
    async def slash_help(self, interaction: discord.Interaction) -> None:
        user_role_ids = {r.id for r in interaction.user.roles}

        embed = discord.Embed(
            title="Commands",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="General",
            value=(
                "`/points` — Check your points balance\n"
                "`/leaderboard` — View the current weekly leaderboard\n"
                "`/coinflip [amount]` — Bet points on a coin flip\n"
                "`/predict [outcome] [amount]` — Bet on the open prediction\n"
                "`/help` — Show this message"
            ),
            inline=False,
        )

        if self.config.roles.predict_starter and self.config.roles.predict_starter in user_role_ids:
            embed.add_field(
                name="Prediction Starter",
                value=(
                    "`/createpredict [outcome1] [outcome2] [message]` — Open a new prediction\n"
                    "`/finishpredict [outcome]` — Close the prediction and pay out winners"
                ),
                inline=False,
            )

        if self.config.roles.point_awarder and self.config.roles.point_awarder in user_role_ids:
            embed.add_field(
                name="Point Awarder",
                value="`/awardpoints [user] [amount]` — Directly award points to a user",
                inline=False,
            )

        if (
            self.config.roles.reaction_role_admin
            and self.config.roles.reaction_role_admin in user_role_ids
        ):
            embed.add_field(
                name="Reaction Roles Admin",
                value=(
                    "`/reactionrole-add [role] [emoji]` — Add or update a reaction role\n"
                    "`/reactionrole-remove [role]` — Remove a reaction role\n"
                    "`/reactionrole-list` — List configured reaction roles"
                ),
                inline=False,
            )

        if self.config.roles.dry_run_admin and self.config.roles.dry_run_admin in user_role_ids:
            embed.add_field(
                name="Admin",
                value=(
                    "`/dryrun-count` — Preview reaction counts (no DB writes)\n"
                    "`/dryrun-announce` — Preview the winner announcement\n"
                    "`/dryrun-leaderboard` — Preview the leaderboard embed\n"
                    "`/dryrun-simulate` — Run the full Monday pipeline for real\n"
                    "`/force-announce` — Post results now (use if bot missed Monday)"
                ),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

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

        # Build a base URL so entries can link to their best submission
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if guild:
            sub_url_base = f"https://discord.com/channels/{guild.id}/{self.config.channels.submissions}"
        else:
            sub_url_base = None

        embed = self._build_leaderboard_embed(period, entries, sub_url_base)

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

    # ------------------------------------------------------------------
    # /coinflip
    # ------------------------------------------------------------------

    @app_commands.command(name="coinflip", description="Bet points on a coin flip.")
    @app_commands.describe(amount="Number of points to wager")
    async def slash_coinflip(self, interaction: discord.Interaction, amount: int) -> None:
        if amount < 1:
            await interaction.response.send_message("Amount must be at least 1.", ephemeral=True)
            return

        balance = await db.get_user_points(self._pool, interaction.user.id)
        if balance is None:
            balance = 0
        if balance < amount:
            await interaction.response.send_message(
                f"You only have **{balance}** {self.config.points.name} — not enough to wager {amount}.",
                ephemeral=True,
            )
            return

        max_flips = self.config.coinflip.max_daily_flips
        daily_count = await db.get_daily_flip_count(self._pool, interaction.user.id)
        if daily_count >= max_flips:
            await interaction.response.send_message(
                f"You've hit your daily limit of **{max_flips}** coin flip(s). Try again tomorrow!",
                ephemeral=True,
            )
            return

        won = random.random() < 0.5
        delta = amount if won else -amount
        await db.award_points_bulk(self._pool, [(interaction.user.id, delta)])
        await db.record_coinflip(self._pool, interaction.user.id, amount, won)
        new_balance = (balance or 0) + delta

        if won:
            embed = discord.Embed(
                title="🪙 Heads — You Win!",
                description=f"You won **+{amount}** {self.config.points.name} {self.config.points.emoji}",
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="🪙 Tails — You Lose!",
                description=f"You lost **{amount}** {self.config.points.name} {self.config.points.emoji}",
                color=discord.Color.red(),
            )
        embed.add_field(name="New Balance", value=f"{new_balance} {self.config.points.name}", inline=True)
        flips_left = max_flips - daily_count - 1
        embed.add_field(name="Flips Remaining Today", value=str(flips_left), inline=True)
        embed.set_footer(text=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.timestamp = discord.utils.utcnow()
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------

    def _build_leaderboard_embed(
        self,
        period: dict[str, Any],
        entries: list[dict[str, Any]],
        sub_url_base: str | None = None,
    ) -> discord.Embed:
        start = period["start_time"]
        end = period["end_time"]
        period_range = (
            f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
            if hasattr(start, "strftime")
            else f"{start} – {end}"
        )

        def score_text(entry: dict[str, Any]) -> str:
            score = f"{entry['net_trophy_count']} 🏆"
            embed_id = entry.get("best_embed_id")
            if sub_url_base and embed_id:
                return f"[{score}]({sub_url_base}/{embed_id})"
            return score

        lines = []

        # Always show the top 3 rows; fill missing places with a placeholder
        for rank in range(1, 4):
            medal = RANK_MEDALS[rank]
            if len(entries) >= rank:
                entry = entries[rank - 1]
                lines.append(f"{medal} <@{entry['user_id']}> — {score_text(entry)}")
            else:
                lines.append(f"{medal} *crickets* 🏏")

        # Ranks 4–10 only if users exist
        for i, entry in enumerate(entries[3:10], start=4):
            lines.append(f"{i}. <@{entry['user_id']}> — {score_text(entry)}")

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
