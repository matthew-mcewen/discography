from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import db.database as db
from cogs.checks import FeatureDisabled, get_config, require_role

if TYPE_CHECKING:
    from main import DiscographyBot

logger = logging.getLogger(__name__)


def resolve_landing(bets: list[dict[str, Any]], outcome: int) -> dict[str, Any]:
    """
    Resolve a landing-rate round, Price-Is-Right style: the winner is the bet
    whose guess is closest to ``outcome`` **without going over** it — i.e. the
    largest guess that is still ``<= outcome``.

    ``bets`` = [{"id", "user_id", "guess", "amount", ...}, ...].

    Returns a dict:
      - ``pot``           — total staked across all bets
      - ``refund``        — True when every guess went over (no eligible winner)
      - ``winners``       — list of winning bet dicts (>1 only on an exact tie)
      - ``winning_guess`` — the winning guess value, or None on refund
      - ``share``         — per-winner payout = pot // len(winners) (0 on refund)
    """
    pot = sum(int(b["amount"]) for b in bets)
    eligible = [b for b in bets if int(b["guess"]) <= outcome]
    if not eligible:
        return {"pot": pot, "refund": True, "winners": [], "winning_guess": None, "share": 0}

    winning_guess = max(int(b["guess"]) for b in eligible)
    winners = [b for b in eligible if int(b["guess"]) == winning_guess]
    share = pot // len(winners)
    return {
        "pot": pot,
        "refund": False,
        "winners": winners,
        "winning_guess": winning_guess,
        "share": share,
    }


class LandingRateCog(commands.Cog, name="LandingRateCog"):
    def __init__(self, bot: DiscographyBot) -> None:
        self.bot = bot

    @property
    def _pool(self):
        return self.bot.db_pool

    # ------------------------------------------------------------------
    # /landing-rate
    # ------------------------------------------------------------------

    @app_commands.command(
        name="landing-rate",
        description="Bet points on a landing-rate outcome. Closest without going over wins the pot.",
    )
    @app_commands.describe(
        landing_rate="The landing rate (integer) you're betting on",
        amount="Number of points to stake",
    )
    async def landing_rate(
        self,
        interaction: discord.Interaction,
        landing_rate: int,
        amount: int,
    ) -> None:
        config = await get_config(interaction)
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
                f"You only have **{balance}** {config.points.name} — not enough to stake {amount}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await db.upsert_user(
            self._pool,
            guild_id,
            interaction.user.id,
            str(interaction.user),
            interaction.user.display_name,
        )

        round_ = await db.get_or_create_open_landing_round(self._pool, guild_id, interaction.channel_id)
        placed = await db.place_landing_bet_and_deduct(
            self._pool, guild_id, round_["id"], interaction.user.id, landing_rate, amount
        )
        if not placed:
            await interaction.followup.send(
                "You've already placed a bet on the current landing-rate round.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Bet placed! You staked **{amount}** {config.points.name} on a landing rate "
            f"of **{landing_rate}**.\nClosest without going over wins the pot. Good luck!",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /landing-rate-outcome
    # ------------------------------------------------------------------

    @app_commands.command(
        name="landing-rate-outcome",
        description="[Landing Rate] Set the actual landing rate and pay out the closest-without-going-over.",
    )
    @app_commands.describe(landing_rate="The actual landing rate that occurred (integer)")
    @require_role("landing_rate_outcome")
    async def landing_rate_outcome(
        self,
        interaction: discord.Interaction,
        landing_rate: int,
    ) -> None:
        config = await get_config(interaction)
        if config is None:
            return
        guild_id = interaction.guild_id

        await interaction.response.defer()

        round_ = await db.get_open_landing_round(self._pool, guild_id)
        if round_ is None:
            await interaction.followup.send(
                "There's no open landing-rate round to resolve.", ephemeral=True
            )
            return

        bets = await db.get_landing_bets(self._pool, guild_id, round_["id"])
        if not bets:
            await db.close_landing_round(self._pool, guild_id, round_["id"], landing_rate)
            await interaction.followup.send(
                "No bets were placed on this round. Round closed.", ephemeral=True
            )
            return

        result = resolve_landing(bets, landing_rate)

        awards: list[tuple[int, int]] = []
        bet_results: list[dict[str, int]] = []
        if result["refund"]:
            # Everyone went over — return each stake, net zero.
            for b in bets:
                awards.append((b["user_id"], b["amount"]))
                bet_results.append({"bet_id": b["id"], "points_change": 0})
        else:
            winner_ids = {b["id"] for b in result["winners"]}
            share = result["share"]
            for b in bets:
                if b["id"] in winner_ids:
                    awards.append((b["user_id"], share))
                    bet_results.append({"bet_id": b["id"], "points_change": share - b["amount"]})
                else:
                    bet_results.append({"bet_id": b["id"], "points_change": -b["amount"]})

        if awards:
            await db.award_points_bulk(self._pool, guild_id, awards)
        await db.save_landing_results(self._pool, guild_id, bet_results)
        await db.close_landing_round(self._pool, guild_id, round_["id"], landing_rate)

        embed = discord.Embed(title="🛬 Landing-rate result", color=discord.Color.gold())
        embed.add_field(name="Actual landing rate", value=str(landing_rate), inline=False)

        if result["refund"]:
            embed.description = "Everyone went over — no winner. All stakes refunded."
        else:
            winners = result["winners"]
            names = ", ".join(f"<@{b['user_id']}>" for b in winners)
            desc = (
                f"Closest without going over: **{result['winning_guess']}**\n"
                f"{names} — won **{result['share']}** {config.points.name} "
                f"{config.points.emoji}"
            )
            if len(winners) > 1:
                desc += f" (pot of {result['pot']} split {len(winners)} ways)"
            embed.description = desc

        bet_lines = [
            f"<@{b['user_id']}> — {b['guess']} ({b['amount']} {config.points.name})"
            for b in bets
        ]
        embed.add_field(name="All bets", value="\n".join(bet_lines)[:1024], inline=False)
        embed.set_footer(text=f"Resolved by {interaction.user.display_name}")
        embed.timestamp = discord.utils.utcnow()

        await interaction.followup.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, FeatureDisabled):
            await self._respond(interaction, "The landing-rate game isn't enabled for this server.")
        elif isinstance(error, (app_commands.MissingRole, app_commands.CheckFailure)):
            await self._respond(interaction, "You don't have permission to use this command.")
        else:
            logger.exception("Unhandled landing-rate command error: %s", error)
            await self._respond(interaction, "An error occurred. Check the bot logs.")

    @staticmethod
    async def _respond(interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: DiscographyBot) -> None:
    await bot.add_cog(LandingRateCog(bot))
