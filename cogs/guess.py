from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import db.database as db
from utils.config import BotConfig

if TYPE_CHECKING:
    from main import DiscographyBot

logger = logging.getLogger(__name__)

RANK_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
MEDAL_RANKS = {"🥇": 1, "🥈": 2, "🥉": 3}
CATEGORY_LABELS = {"airport": "Airports", "aircraft": "Aircraft"}
ROUND_HOURS = 24


def pick_prize(votes: dict[int, int], amounts: dict[int, int]) -> tuple[int, int]:
    """
    Decide a single guess's award from judge-role medal votes.

    ``votes`` maps rank (1=🥇, 2=🥈, 3=🥉) to the number of judge-role reactions.
    ``amounts`` maps rank 1/2/3 to the place prizes and 0 to the participation amount.

    Returns ``(rank, prize)`` where rank is the winning medal (1/2/3) or 0 for
    participation. The medal with the most votes wins; ties break toward the
    better medal (🥇 > 🥈 > 🥉). If no medal received any votes, participation is
    awarded.
    """
    best_rank = 0
    best_votes = 0
    for rank in (1, 2, 3):
        v = votes.get(rank, 0)
        if v > best_votes:
            best_votes = v
            best_rank = rank
    if best_votes == 0:
        return 0, amounts[0]
    return best_rank, amounts[best_rank]


class GuessCog(commands.Cog, name="GuessCog"):
    def __init__(self, bot: DiscographyBot, config: BotConfig) -> None:
        self.bot = bot
        self.config = config

    @property
    def _pool(self):
        return self.bot.db_pool

    # ------------------------------------------------------------------
    # /guess
    # ------------------------------------------------------------------

    @app_commands.command(
        name="guess",
        description="Submit your guess for the Airports and/or Aircraft. Each locks once submitted.",
    )
    @app_commands.describe(
        airports="Your guess for the airport (optional if you're guessing aircraft)",
        aircraft="Your guess for the aircraft (optional if you're guessing airports)",
    )
    async def guess(
        self,
        interaction: discord.Interaction,
        airports: Optional[str] = None,
        aircraft: Optional[str] = None,
    ) -> None:
        submissions: list[tuple[str, str]] = []
        if airports and airports.strip():
            submissions.append(("airport", airports.strip()))
        if aircraft and aircraft.strip():
            submissions.append(("aircraft", aircraft.strip()))

        if not submissions:
            await interaction.response.send_message(
                "Provide a guess for **Airports**, **Aircraft**, or both.", ephemeral=True
            )
            return

        round_ = await db.get_active_guess_round(self._pool)
        if round_ is None:
            # No active round: respond ephemerally so it's invisible to everyone else.
            await interaction.response.send_message(
                "There's no active guess round right now.", ephemeral=True
            )
            return

        if not self.config.channels.guess_output:
            await interaction.response.send_message(
                "The guess output channel isn't configured yet. Ask an admin to set `channels.guess_output`.",
                ephemeral=True,
            )
            return
        output_ch = self.bot.get_channel(self.config.channels.guess_output)
        if output_ch is None:
            await interaction.response.send_message(
                "I can't find the configured guess output channel.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await db.upsert_user(
            self._pool,
            interaction.user.id,
            str(interaction.user),
            interaction.user.display_name,
        )

        already = await db.get_user_guess_categories(self._pool, round_["id"], interaction.user.id)

        submitted: list[str] = []
        locked: list[str] = []
        for category, value in submissions:
            if category in already:
                locked.append(category)
                continue
            try:
                guess_id = await db.create_guess(
                    self._pool, round_["id"], interaction.user.id, category, value
                )
            except Exception:
                # Most likely the UNIQUE (round, user, category) guard firing on a
                # concurrent submission — treat it as already locked.
                logger.exception("Failed to create guess for user %s", interaction.user.id)
                locked.append(category)
                continue

            embed = discord.Embed(
                title=f"{CATEGORY_LABELS[category]} guess",
                description=value,
                color=discord.Color.blurple(),
            )
            embed.set_author(
                name=interaction.user.display_name,
                icon_url=interaction.user.display_avatar.url,
            )
            embed.set_footer(text=f"{CATEGORY_LABELS[category]} · guess round #{round_['id']}")
            embed.timestamp = discord.utils.utcnow()

            try:
                out_msg = await output_ch.send(embed=embed)
                for medal in ("🥇", "🥈", "🥉"):
                    await out_msg.add_reaction(medal)
                await db.set_guess_output_message(self._pool, guess_id, out_msg.id)
            except discord.Forbidden:
                logger.error(
                    "Missing permissions to post/react in guess output channel %s.",
                    self.config.channels.guess_output,
                )
            submitted.append(category)

        lines: list[str] = []
        if submitted:
            lines.append(
                "✅ Locked in: " + ", ".join(CATEGORY_LABELS[c] for c in submitted) + "."
            )
        if locked:
            lines.append(
                "🔒 Already submitted (can't change): "
                + ", ".join(CATEGORY_LABELS[c] for c in locked)
                + "."
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ------------------------------------------------------------------
    # /guess-start
    # ------------------------------------------------------------------

    @app_commands.command(
        name="guess-start",
        description="[Guess Judge] Start a new 24h guess round.",
    )
    async def guess_start(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=ROUND_HOURS)
        round_id = await db.start_guess_round(self._pool, interaction.user.id, expires_at)

        if self.config.channels.guess_output:
            ch = self.bot.get_channel(self.config.channels.guess_output)
            if ch is not None:
                embed = discord.Embed(
                    title="🎯 New guess round started!",
                    description=(
                        "Use `/guess` to submit your **Airports** and/or **Aircraft** guess. "
                        "Each category locks in the moment you submit it.\n\n"
                        f"Open for {ROUND_HOURS}h, or until the judges tally the results."
                    ),
                    color=discord.Color.green(),
                )
                embed.timestamp = discord.utils.utcnow()
                try:
                    await ch.send(embed=embed)
                except discord.Forbidden:
                    logger.error("Missing permissions to post in guess output channel.")

        await interaction.followup.send(
            f"Guess round #{round_id} started — open for {ROUND_HOURS}h "
            f"(or until you run `/guess-count`).",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /guess-count
    # ------------------------------------------------------------------

    @app_commands.command(
        name="guess-count",
        description="[Guess Judge] Tally judge reactions and award BasicMiles, closing the round.",
    )
    @app_commands.describe(
        firstplace="BasicMiles awarded to a guess whose top medal is 🥇",
        secondplace="BasicMiles awarded to a guess whose top medal is 🥈",
        thirdplace="BasicMiles awarded to a guess whose top medal is 🥉",
        participation="BasicMiles awarded to a guess with no medal votes",
    )
    async def guess_count(
        self,
        interaction: discord.Interaction,
        firstplace: int,
        secondplace: int,
        thirdplace: int,
        participation: int,
    ) -> None:
        if min(firstplace, secondplace, thirdplace, participation) < 0:
            await interaction.response.send_message(
                "Amounts can't be negative.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        round_ = await db.get_countable_guess_round(self._pool)
        if round_ is None:
            await interaction.followup.send(
                "There's no active guess round to count.", ephemeral=True
            )
            return

        guesses = await db.get_guesses_for_round(self._pool, round_["id"])
        if not guesses:
            await db.close_guess_round(self._pool, round_["id"])
            await interaction.followup.send(
                "No guesses were submitted this round. Round closed.", ephemeral=True
            )
            return

        output_ch = (
            self.bot.get_channel(self.config.channels.guess_output)
            if self.config.channels.guess_output
            else None
        )
        guild = interaction.guild
        judge_role_id = self.config.roles.guess_judge
        amounts = {1: firstplace, 2: secondplace, 3: thirdplace, 0: participation}

        per_user_award: dict[int, int] = defaultdict(int)
        guess_results: list[dict[str, int]] = []
        detail_lines: list[str] = []

        for g in guesses:
            votes = {1: 0, 2: 0, 3: 0}
            if output_ch is not None and g["output_message_id"]:
                try:
                    msg = await output_ch.fetch_message(g["output_message_id"])
                    votes = await self._count_medal_votes(msg, guild, judge_role_id)
                except (discord.NotFound, discord.HTTPException):
                    pass

            rank, prize = pick_prize(votes, amounts)
            per_user_award[g["user_id"]] += prize
            guess_results.append({"guess_id": g["id"], "points_awarded": prize})

            medal_label = RANK_MEDALS.get(rank, "🎟️ Participation")
            detail_lines.append(
                f"{CATEGORY_LABELS.get(g['category'], g['category'])} — <@{g['user_id']}> "
                f"“{g['guess_value']}” → {medal_label} "
                f"({prize} {self.config.points.name})"
            )

        bulk = [(uid, amt) for uid, amt in per_user_award.items() if amt > 0]
        if bulk:
            await db.award_points_bulk(self._pool, bulk)
        await db.save_guess_results(self._pool, guess_results)
        await db.close_guess_round(self._pool, round_["id"])

        embed = discord.Embed(
            title="🏁 Guess round results",
            description="\n".join(detail_lines)[:4096],
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Counted by {interaction.user.display_name}")
        embed.timestamp = discord.utils.utcnow()

        if output_ch is not None:
            try:
                await output_ch.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.error("Failed to post guess results to output channel.")

        await interaction.followup.send(
            f"Counted {len(guesses)} guess(es) and awarded points. Round closed.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Vote counting helper
    # ------------------------------------------------------------------

    async def _count_medal_votes(
        self,
        msg: discord.Message,
        guild: discord.Guild | None,
        judge_role_id: int,
    ) -> dict[int, int]:
        """Count 🥇🥈🥉 reactions on ``msg`` that came from judge-role members."""
        counts = {1: 0, 2: 0, 3: 0}
        if guild is None:
            return counts
        for reaction in msg.reactions:
            rank = MEDAL_RANKS.get(str(reaction.emoji))
            if rank is None:
                continue
            async for user in reaction.users():
                if user.id == self.bot.user.id:
                    continue
                member = guild.get_member(user.id)
                if member is None:
                    continue
                if any(r.id == judge_role_id for r in member.roles):
                    counts[rank] += 1
        return counts

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
            logger.exception("Unhandled guess command error: %s", error)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "An error occurred. Check the bot logs.",
                    ephemeral=True,
                )


async def setup(bot: DiscographyBot) -> None:
    cog = GuessCog(bot, bot.config)

    judge_role_id = bot.config.roles.guess_judge

    async def guess_judge_check(interaction: discord.Interaction) -> bool:
        if any(r.id == judge_role_id for r in interaction.user.roles):
            return True
        raise app_commands.MissingRole(judge_role_id)

    restricted_commands = {"guess-start", "guess-count"}
    for cmd in cog.__cog_app_commands__:
        if cmd.name in restricted_commands:
            cmd.add_check(guess_judge_check)

    await bot.add_cog(cog)
