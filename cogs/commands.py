from __future__ import annotations

import logging
import random
from typing import Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import db.database as db
from utils.config import GuildConfig

if TYPE_CHECKING:
    from main import DiscographyBot

logger = logging.getLogger(__name__)

RANK_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


class CommandsCog(commands.Cog, name="CommandsCog"):
    def __init__(self, bot: DiscographyBot) -> None:
        self.bot = bot

    @property
    def _pool(self):
        return self.bot.db_pool

    async def _config(self, interaction: discord.Interaction) -> GuildConfig | None:
        """Reuse the config the gate stashed, or fetch it (cache-cheap)."""
        cfg = interaction.extras.get("config")
        if cfg is not None:
            return cfg
        return await self.bot.get_guild_config(interaction.guild_id)

    # ------------------------------------------------------------------
    # /help
    # ------------------------------------------------------------------

    @app_commands.command(name="help", description="List all commands available to you.")
    async def slash_help(self, interaction: discord.Interaction) -> None:
        config = await self._config(interaction)
        if config is None:
            return
        user_role_ids = {r.id for r in interaction.user.roles}

        embed = discord.Embed(title="Commands", color=discord.Color.blurple())
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

        if config.roles.predict_starter and config.roles.predict_starter in user_role_ids:
            embed.add_field(
                name="Prediction Starter",
                value=(
                    "`/createpredict [outcome1] [outcome2] [message]` — Open a new prediction\n"
                    "`/finishpredict [outcome]` — Close the prediction and pay out winners"
                ),
                inline=False,
            )

        if config.roles.point_awarder and config.roles.point_awarder in user_role_ids:
            embed.add_field(
                name="Point Awarder",
                value="`/awardpoints [user] [amount]` — Directly award points to a user",
                inline=False,
            )

        if config.roles.reaction_role_admin and config.roles.reaction_role_admin in user_role_ids:
            embed.add_field(
                name="Reaction Roles Admin",
                value=(
                    "`/reactionrole-add [role] [emoji]` — Add or update a reaction role\n"
                    "`/reactionrole-remove [role]` — Remove a reaction role\n"
                    "`/reactionrole-list` — List configured reaction roles"
                ),
                inline=False,
            )

        if config.roles.dry_run_admin and config.roles.dry_run_admin in user_role_ids:
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
        config = await self._config(interaction)
        if config is None:
            return
        pts = await db.get_user_points(self._pool, interaction.guild_id, interaction.user.id)
        if pts is None:
            msg = f"You haven't made any submissions yet, so you have 0 {config.points.name}."
        else:
            msg = f"You have **{pts}** {config.points.name} {config.points.emoji}."
        await interaction.response.send_message(msg, ephemeral=True)

    @commands.command(name="points")
    async def prefix_points(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            return
        config = await self.bot.get_guild_config(ctx.guild.id)
        if config is None:
            return
        pts = await db.get_user_points(self._pool, ctx.guild.id, ctx.author.id)
        if pts is None:
            msg = f"You haven't made any submissions yet, so you have 0 {config.points.name}."
        else:
            msg = f"You have **{pts}** {config.points.name} {config.points.emoji}."
        await ctx.reply(msg)

    # ------------------------------------------------------------------
    # /leaderboard
    # ------------------------------------------------------------------

    @app_commands.command(name="leaderboard", description="View the current weekly leaderboard.")
    async def slash_leaderboard(self, interaction: discord.Interaction) -> None:
        embed = await self.update_leaderboard(interaction.guild_id, dry_run=True)
        if embed is None:
            await interaction.response.send_message("No submissions yet this week.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Leaderboard maintenance — called by competition tasks (per guild)
    # ------------------------------------------------------------------

    async def update_leaderboard(self, guild_id: int, dry_run: bool = False) -> discord.Embed | None:
        config = await self.bot.get_guild_config(guild_id)
        if config is None:
            return None

        period = await db.ensure_active_period(self._pool, guild_id, config.tzinfo())
        entries = await db.get_period_leaderboard(self._pool, guild_id, period["id"], limit=10)

        guild = self.bot.get_guild(guild_id)
        if guild and config.channels.submissions:
            sub_url_base = f"https://discord.com/channels/{guild.id}/{config.channels.submissions}"
        else:
            sub_url_base = None

        embed = self._build_leaderboard_embed(period, entries, sub_url_base)

        if dry_run:
            return embed

        if config.channels.leaderboard is None:
            return embed
        channel = self.bot.get_channel(config.channels.leaderboard)
        if channel is None:
            logger.error("Guild %s: leaderboard channel %s not found.", guild_id, config.channels.leaderboard)
            return embed

        stored_id = await db.get_leaderboard_message_id(self._pool, guild_id, period["id"])

        if stored_id is not None:
            try:
                msg = await channel.fetch_message(stored_id)
                await msg.edit(embed=embed)
                return embed
            except discord.NotFound:
                logger.warning("Guild %s: leaderboard message %s deleted; reposting.", guild_id, stored_id)
            except discord.HTTPException as exc:
                logger.error("Guild %s: failed to edit leaderboard message: %s", guild_id, exc)
                return embed

        try:
            msg = await channel.send(embed=embed)
            try:
                await msg.pin()
            except discord.HTTPException as exc:
                if exc.code == 30003:
                    logger.warning("Guild %s: pin limit reached in leaderboard channel.", guild_id)
                else:
                    raise
            await db.set_leaderboard_message_id(self._pool, guild_id, period["id"], msg.id)
        except discord.Forbidden:
            logger.error("Guild %s: missing permissions to post/pin in leaderboard channel.", guild_id)
        except discord.HTTPException as exc:
            logger.error("Guild %s: failed to post leaderboard message: %s", guild_id, exc)

        return embed

    # ------------------------------------------------------------------
    # /coinflip
    # ------------------------------------------------------------------

    @app_commands.command(name="coinflip", description="Bet points on a coin flip.")
    @app_commands.describe(amount="Number of points to wager")
    async def slash_coinflip(self, interaction: discord.Interaction, amount: int) -> None:
        config = await self._config(interaction)
        if config is None:
            return
        guild_id = interaction.guild_id
        if amount < 1:
            await interaction.response.send_message("Amount must be at least 1.", ephemeral=True)
            return

        balance = await db.get_user_points(self._pool, guild_id, interaction.user.id)
        if balance is None:
            balance = 0
        if balance < amount:
            await interaction.response.send_message(
                f"You only have **{balance}** {config.points.name} — not enough to wager {amount}.",
                ephemeral=True,
            )
            return

        max_flips = config.coinflip.max_daily_flips
        daily_count = await db.get_daily_flip_count(self._pool, guild_id, interaction.user.id)
        if daily_count >= max_flips:
            await interaction.response.send_message(
                f"You've hit your daily limit of **{max_flips}** coin flip(s). Try again tomorrow!",
                ephemeral=True,
            )
            return

        won = random.random() < 0.5
        delta = amount if won else -amount
        await db.award_points_bulk(self._pool, guild_id, [(interaction.user.id, delta)])
        await db.record_coinflip(self._pool, guild_id, interaction.user.id, amount, won)
        new_balance = balance + delta

        if won:
            embed = discord.Embed(
                title="🪙 Heads — You Win!",
                description=f"You won **+{amount}** {config.points.name} {config.points.emoji}",
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="🪙 Tails — You Lose!",
                description=f"You lost **{amount}** {config.points.name} {config.points.emoji}",
                color=discord.Color.red(),
            )
        embed.add_field(name="New Balance", value=f"{new_balance} {config.points.name}", inline=True)
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
        for rank in range(1, 4):
            medal = RANK_MEDALS[rank]
            if len(entries) >= rank:
                entry = entries[rank - 1]
                lines.append(f"{medal} <@{entry['user_id']}> — {score_text(entry)}")
            else:
                lines.append(f"{medal} *crickets* 🏏")

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
    await bot.add_cog(CommandsCog(bot))
