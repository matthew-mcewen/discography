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
                "`/guess [airports] [aircraft]` — Submit your Airports/Aircraft guess\n"
                "`/landing-rate [landing_rate] [amount]` — Bet on a landing-rate outcome\n"
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

        if config.roles.guess_judge and config.roles.guess_judge in user_role_ids:
            embed.add_field(
                name="Guess Judge",
                value=(
                    "`/guess-start` — Start a new 24h guess round\n"
                    "`/guess-count [firstplace] [secondplace] [thirdplace] [participation]` "
                    "— Tally 🥇🥈🥉 reactions and award points"
                ),
                inline=False,
            )

        if config.roles.landing_rate_outcome and config.roles.landing_rate_outcome in user_role_ids:
            embed.add_field(
                name="Landing Rate",
                value="`/landing-rate-outcome [landing_rate]` — Set the outcome and pay out winners",
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

        await self._upsert_leaderboard_message(guild_id, period, channel, embed)
        await self._update_first_place_images(guild_id, config, period, channel)
        return embed

    async def _upsert_leaderboard_message(
        self,
        guild_id: int,
        period: dict[str, Any],
        channel: discord.abc.Messageable,
        embed: discord.Embed,
    ) -> None:
        """Edit the pinned leaderboard message in-place, or post + pin a new one."""
        stored_id = await db.get_leaderboard_message_id(self._pool, guild_id, period["id"])

        if stored_id is not None:
            try:
                msg = await channel.fetch_message(stored_id)
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                logger.warning("Guild %s: leaderboard message %s deleted; reposting.", guild_id, stored_id)
            except discord.HTTPException as exc:
                logger.error("Guild %s: failed to edit leaderboard message: %s", guild_id, exc)
                return

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

    async def _update_first_place_images(
        self,
        guild_id: int,
        config: GuildConfig,
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
        top_subs = await db.get_top_submission_per_user(self._pool, guild_id, period["id"], limit=25)
        first_place: list[dict[str, Any]] = []
        if top_subs:
            max_net = top_subs[0]["net_trophy_count"]
            if max_net > 0:
                first_place = [s for s in top_subs if s["net_trophy_count"] == max_net]

        stored_img_id = await db.get_leaderboard_images_message_id(self._pool, guild_id, period["id"])

        if not first_place:
            # No clear leader yet — remove a stale companion message if present.
            if stored_img_id is not None:
                try:
                    old = await channel.fetch_message(stored_img_id)
                    await old.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                await db.set_leaderboard_images_message_id(self._pool, guild_id, period["id"], None)
            return

        # Build one image embed per first-place winner, refreshing CDN URLs from
        # the live submission message (deleting a message invalidates its URL).
        submissions_ch = (
            self.bot.get_channel(config.channels.submissions) if config.channels.submissions else None
        )
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
                    "Guild %s: first-place images message %s was deleted; reposting.",
                    guild_id,
                    stored_img_id,
                )
            except discord.HTTPException as exc:
                logger.error("Guild %s: failed to edit first-place images message: %s", guild_id, exc)
                return

        try:
            msg = await channel.send(content=content, embeds=image_embeds)
            try:
                await msg.pin()
            except discord.HTTPException as exc:
                if exc.code == 30003:
                    logger.warning("Guild %s: pin limit reached; first-place images not pinned.", guild_id)
                else:
                    raise
            await db.set_leaderboard_images_message_id(self._pool, guild_id, period["id"], msg.id)
        except discord.Forbidden:
            logger.error("Guild %s: missing permissions to post/pin first-place images.", guild_id)
        except discord.HTTPException as exc:
            logger.error("Guild %s: failed to post first-place images message: %s", guild_id, exc)

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
    await bot.add_cog(CommandsCog(bot))
