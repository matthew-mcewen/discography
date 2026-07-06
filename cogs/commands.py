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
                "`/guess [airports] [aircraft]` — Submit your Airports/Aircraft guess\n"
                "`/landing-rate [landing_rate] [amount]` — Bet on a landing-rate outcome\n"
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

        if self.config.roles.guess_judge and self.config.roles.guess_judge in user_role_ids:
            embed.add_field(
                name="Guess Judge",
                value=(
                    "`/guess-start` — Start a new 24h guess round\n"
                    "`/guess-count [firstplace] [secondplace] [thirdplace] [participation]` "
                    "— Tally 🥇🥈🥉 reactions and award BasicMiles"
                ),
                inline=False,
            )

        if (
            self.config.roles.landing_rate_outcome
            and self.config.roles.landing_rate_outcome in user_role_ids
        ):
            embed.add_field(
                name="Landing Rate",
                value="`/landing-rate-outcome [landing_rate]` — Set the outcome and pay out winners",
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

        await self._upsert_leaderboard_message(period, channel, embed)
        await self._update_first_place_images(period, channel)
        return embed

    async def _upsert_leaderboard_message(
        self,
        period: dict[str, Any],
        channel: discord.abc.Messageable,
        embed: discord.Embed,
    ) -> None:
        """Edit the pinned leaderboard message in-place, or post + pin a new one."""
        stored_id = await db.get_leaderboard_message_id(self._pool, period["id"])

        if stored_id is not None:
            try:
                msg = await channel.fetch_message(stored_id)
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                logger.warning("Leaderboard message %s was deleted; reposting.", stored_id)
            except discord.HTTPException as exc:
                logger.error("Failed to edit leaderboard message: %s", exc)
                return

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

    async def _update_first_place_images(
        self,
        period: dict[str, Any],
        channel: discord.abc.Messageable,
    ) -> None:
        """
        Maintain a companion message, pinned next to the leaderboard, showing the
        current first-place winner's best screenshot. If several users are tied for
        first, each of their screenshots is shown (one embed per winner). The
        message is edited in place after every count and removed when no one has a
        vote yet.
        """
        top_subs = await db.get_top_submission_per_user(self._pool, period["id"], limit=25)
        first_place: list[dict[str, Any]] = []
        if top_subs:
            max_net = top_subs[0]["net_trophy_count"]
            if max_net > 0:
                first_place = [s for s in top_subs if s["net_trophy_count"] == max_net]

        stored_img_id = await db.get_leaderboard_images_message_id(self._pool, period["id"])

        if not first_place:
            # No clear leader yet — remove a stale companion message if present.
            if stored_img_id is not None:
                try:
                    old = await channel.fetch_message(stored_img_id)
                    await old.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                await db.set_leaderboard_images_message_id(self._pool, period["id"], None)
            return

        # Build one image embed per first-place winner, refreshing CDN URLs from
        # the live submission message (deleting a message invalidates its URL).
        submissions_ch = self.bot.get_channel(self.config.channels.submissions)
        image_embeds: list[discord.Embed] = []
        for sub in first_place[:10]:  # Discord allows at most 10 embeds per message
            image_url = sub["image_url"]
            if submissions_ch is not None:
                try:
                    live = await submissions_ch.fetch_message(sub["embed_message_id"])
                    if live.attachments:
                        image_url = live.attachments[0].url
                    elif live.embeds and live.embeds[0].image:
                        image_url = live.embeds[0].image.proxy_url or image_url
                except (discord.NotFound, discord.HTTPException):
                    pass
            img_embed = discord.Embed(
                description=f"🥇 <@{sub['user_id']}> — {sub['net_trophy_count']} 🏆",
                color=discord.Color.gold(),
            )
            img_embed.set_image(url=image_url)
            image_embeds.append(img_embed)

        content = "🥇 **Current first place**" + (" (tied)" if len(image_embeds) > 1 else "")

        if stored_img_id is not None:
            try:
                msg = await channel.fetch_message(stored_img_id)
                await msg.edit(content=content, embeds=image_embeds)
                return
            except discord.NotFound:
                logger.warning(
                    "First-place images message %s was deleted; reposting.", stored_img_id
                )
            except discord.HTTPException as exc:
                logger.error("Failed to edit first-place images message: %s", exc)
                return

        try:
            msg = await channel.send(content=content, embeds=image_embeds)
            try:
                await msg.pin()
            except discord.HTTPException as exc:
                if exc.code == 30003:
                    logger.warning("Pin limit reached; first-place images not pinned.")
                else:
                    raise
            await db.set_leaderboard_images_message_id(self._pool, period["id"], msg.id)
        except discord.Forbidden:
            logger.error("Missing permissions to post/pin first-place images.")
        except discord.HTTPException as exc:
            logger.error("Failed to post first-place images message: %s", exc)

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

        entries = entries[:10]
        lines = []

        if not entries:
            # Empty board — show the podium placeholders.
            for rank in range(1, 4):
                lines.append(f"{RANK_MEDALS[rank]} *crickets* 🏏")
        else:
            # Standard competition ranking: tied scores share a rank (and its
            # medal), and each tied user gets its own duplicated line. The next
            # distinct score jumps to 1 + its index, so a rank is skipped after a
            # tie (e.g. two 🥇 then 🥉, with 🥈 skipped).
            prev_score = None
            rank = 0
            for i, entry in enumerate(entries):
                score = entry["net_trophy_count"]
                if score != prev_score:
                    rank = i + 1
                    prev_score = score
                label = RANK_MEDALS.get(rank, f"{rank}.")
                lines.append(f"{label} <@{entry['user_id']}> — {score_text(entry)}")

            # Preserve the podium look on a small board with no ties: pad the
            # remaining medal spots with placeholders.
            distinct_scores = len({e["net_trophy_count"] for e in entries})
            if len(entries) < 3 and distinct_scores == len(entries):
                for pad_rank in range(len(entries) + 1, 4):
                    lines.append(f"{RANK_MEDALS[pad_rank]} *crickets* 🏏")

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
