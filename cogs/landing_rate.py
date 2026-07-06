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
    def __init__(self, bot: DiscographyBot, config: BotConfig) -> None:
        self.bot = bot
        self.config = config

    @property
    def _pool(self):
        return self.bot.db_pool

    # ------------------------------------------------------------------
    # /landing-rate
    # ------------------------------------------------------------------

    @app_commands.command(
        name="landing-rate",
        description="Bet BasicMiles on a landing-rate outcome. Closest without going over wins the pot.",
    )
    @app_commands.describe(
        landing_rate="The landing rate (integer) you're betting on",
        amount="Number of BasicMiles to stake",
    )
    async def landing_rate(
        self,
        interaction: discord.Interaction,
        landing_rate: int,
        amount: int,
    ) -> None:
        if amount < 1:
            await interaction.response.send_message("Amount must be at least 1.", ephemeral=True)
            return

        balance = await db.get_user_points(self._pool, interaction.user.id)
        if balance is None:
            balance = 0
        if balance < amount:
            await interaction.response.send_message(
                f"You only have **{balance}** {self.config.points.name} — not enough to stake {amount}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await db.upsert_user(
            self._pool,
            interaction.user.id,
            str(interaction.user),
            interaction.user.display_name,
        )

        round_ = await db.get_or_create_open_landing_round(self._pool, interaction.channel_id)
        placed = await db.place_landing_bet_and_deduct(
            self._pool, round_["id"], interaction.user.id, landing_rate, amount
        )
        if not placed:
            await interaction.followup.send(
                "You've already placed a bet on the current landing-rate round.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Bet placed! You staked **{amount}** {self.config.points.name} on a landing rate "
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
    async def landing_rate_outcome(
        self,
        interaction: discord.Interaction,
        landing_rate: int,
    ) -> None:
        await interaction.response.defer()

        round_ = await db.get_open_landing_round(self._pool)
        if round_ is None:
            await interaction.followup.send(
                "There's no open landing-rate round to resolve.", ephemeral=True
            )
            return

        bets = await db.get_landing_bets(self._pool, round_["id"])
        if not bets:
            await db.close_landing_round(self._pool, round_["id"], landing_rate)
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
            await db.award_points_bulk(self._pool, awards)
        await db.save_landing_results(self._pool, bet_results)
        await db.close_landing_round(self._pool, round_["id"], landing_rate)

        embed = discord.Embed(title="🛬 Landing-rate result", color=discord.Color.gold())
        embed.add_field(name="Actual landing rate", value=str(landing_rate), inline=False)

        if result["refund"]:
            embed.description = "Everyone went over — no winner. All stakes refunded."
        else:
            winners = result["winners"]
            names = ", ".join(f"<@{b['user_id']}>" for b in winners)
            desc = (
                f"Closest without going over: **{result['winning_guess']}**\n"
                f"{names} — won **{result['share']}** {self.config.points.name} "
                f"{self.config.points.emoji}"
            )
            if len(winners) > 1:
                desc += f" (pot of {result['pot']} split {len(winners)} ways)"
            embed.description = desc

        bet_lines = [
            f"<@{b['user_id']}> — {b['guess']} ({b['amount']} {self.config.points.name})"
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
            logger.exception("Unhandled landing-rate command error: %s", error)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "An error occurred. Check the bot logs.",
                    ephemeral=True,
                )


async def setup(bot: DiscographyBot) -> None:
    cog = LandingRateCog(bot, bot.config)

    outcome_role_id = bot.config.roles.landing_rate_outcome

    async def outcome_check(interaction: discord.Interaction) -> bool:
        if any(r.id == outcome_role_id for r in interaction.user.roles):
            return True
        raise app_commands.MissingRole(outcome_role_id)

    for cmd in cog.__cog_app_commands__:
        if cmd.name == "landing-rate-outcome":
            cmd.add_check(outcome_check)

    await bot.add_cog(cog)
