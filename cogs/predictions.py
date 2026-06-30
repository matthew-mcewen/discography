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


class PredictionsCog(commands.Cog, name="PredictionsCog"):
    def __init__(self, bot: DiscographyBot, config: BotConfig) -> None:
        self.bot = bot
        self.config = config

    @property
    def _pool(self):
        return self.bot.db_pool

    # ------------------------------------------------------------------
    # /createpredict
    # ------------------------------------------------------------------

    @app_commands.command(
        name="createpredict",
        description="[Predict Starter] Open a prediction bet with two possible outcomes.",
    )
    @app_commands.describe(
        outcome1="First possible outcome",
        outcome2="Second possible outcome",
        message="The question or event being predicted",
    )
    async def createpredict(
        self,
        interaction: discord.Interaction,
        outcome1: str,
        outcome2: str,
        message: str,
    ) -> None:
        existing = await db.get_open_prediction(self._pool)
        if existing is not None:
            await interaction.response.send_message(
                "There's already an open prediction. Use `/finishpredict` to close it first.",
                ephemeral=True,
            )
            return

        prediction_id = await db.create_prediction(
            self._pool,
            creator_id=interaction.user.id,
            outcome1=outcome1,
            outcome2=outcome2,
            message=message,
            channel_id=interaction.channel_id,
        )

        embed = discord.Embed(
            title="Prediction Open!",
            description=message,
            color=discord.Color.blurple(),
        )
        seed = self.config.predictions.seed_liquidity
        start_odds = db.decimal_odds(0, 0, seed)
        embed.add_field(name="Option A", value=f"{outcome1}\n`{start_odds:.2f}×`", inline=True)
        embed.add_field(name="Option B", value=f"{outcome2}\n`{start_odds:.2f}×`", inline=True)
        embed.add_field(
            name="How to bet",
            value=(
                "Use `/predict` to place your wager on either outcome. Your points are "
                "deducted immediately, and a win pays back your stake **× the odds** "
                "locked in when you bet.\n"
                "Odds are dynamic: the more money on a side, the lower its odds — so "
                "betting the underdog early earns the biggest returns. Check live odds "
                "any time with `/odds`."
            ),
            inline=False,
        )
        embed.set_footer(
            text=f"Started by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        embed.timestamp = discord.utils.utcnow()

        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        await db.set_prediction_embed_message(self._pool, prediction_id, msg.id)

    # ------------------------------------------------------------------
    # /predict
    # ------------------------------------------------------------------

    async def _outcome_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        prediction = await db.get_open_prediction(self._pool)
        if prediction is None:
            return []
        choices = [prediction["outcome1"], prediction["outcome2"]]
        return [
            app_commands.Choice(name=c, value=c)
            for c in choices
            if current.lower() in c.lower()
        ]

    @app_commands.command(name="predict", description="Place a bet on the open prediction.")
    @app_commands.describe(
        outcome="The outcome you're betting on",
        amount="Number of points to wager",
    )
    @app_commands.autocomplete(outcome=_outcome_autocomplete)
    async def predict(
        self,
        interaction: discord.Interaction,
        outcome: str,
        amount: int,
    ) -> None:
        if amount < 1:
            await interaction.response.send_message("Amount must be at least 1.", ephemeral=True)
            return

        prediction = await db.get_open_prediction(self._pool)
        if prediction is None:
            await interaction.response.send_message(
                "There's no open prediction right now.", ephemeral=True
            )
            return

        valid_outcomes = {prediction["outcome1"], prediction["outcome2"]}
        if outcome not in valid_outcomes:
            await interaction.response.send_message(
                f"Invalid outcome. Choose one of: **{prediction['outcome1']}** or **{prediction['outcome2']}**",
                ephemeral=True,
            )
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

        other_outcome = (
            prediction["outcome2"] if outcome == prediction["outcome1"] else prediction["outcome1"]
        )
        odds = await db.place_bet_and_deduct(
            self._pool,
            prediction_id=prediction["id"],
            user_id=interaction.user.id,
            outcome=outcome,
            other_outcome=other_outcome,
            amount=amount,
            seed=self.config.predictions.seed_liquidity,
        )
        if odds is None:
            await interaction.response.send_message(
                "You've already placed a bet on this prediction.", ephemeral=True
            )
            return

        payout = round(amount * odds)
        await interaction.response.send_message(
            f"Bet placed! You wagered **{amount}** {self.config.points.name} on **{outcome}** "
            f"at odds **{odds:.2f}×**.\n"
            f"If you win you'll get back **{payout}** {self.config.points.emoji} "
            f"(profit **+{payout - amount}**). Good luck!",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /odds
    # ------------------------------------------------------------------

    @app_commands.command(
        name="odds",
        description="Show the current live odds for the open prediction.",
    )
    async def odds(self, interaction: discord.Interaction) -> None:
        prediction = await db.get_open_prediction(self._pool)
        if prediction is None:
            await interaction.response.send_message(
                "There's no open prediction right now.", ephemeral=True
            )
            return

        pools = await db.get_prediction_pools(self._pool, prediction["id"])
        seed = self.config.predictions.seed_liquidity
        o1, o2 = prediction["outcome1"], prediction["outcome2"]
        pool1, pool2 = pools.get(o1, 0), pools.get(o2, 0)
        odds1 = db.decimal_odds(pool1, pool2, seed)
        odds2 = db.decimal_odds(pool2, pool1, seed)

        embed = discord.Embed(
            title="Live Odds",
            description=prediction["message"],
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name=o1,
            value=f"**{odds1:.2f}×**\n{pool1} {self.config.points.name} staked",
            inline=True,
        )
        embed.add_field(
            name=o2,
            value=f"**{odds2:.2f}×**\n{pool2} {self.config.points.name} staked",
            inline=True,
        )
        embed.set_footer(text="Odds shift as bets come in. Lock yours in with /predict.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /finishpredict
    # ------------------------------------------------------------------

    @app_commands.command(
        name="finishpredict",
        description="[Predict Starter] Close the open prediction and declare a winner.",
    )
    @app_commands.describe(outcome="The outcome that actually happened")
    @app_commands.autocomplete(outcome=_outcome_autocomplete)
    async def finishpredict(
        self,
        interaction: discord.Interaction,
        outcome: str,
    ) -> None:
        await interaction.response.defer()

        prediction = await db.get_open_prediction(self._pool)
        if prediction is None:
            await interaction.followup.send(
                "There's no open prediction to close.", ephemeral=True
            )
            return

        valid_outcomes = {prediction["outcome1"], prediction["outcome2"]}
        if outcome not in valid_outcomes:
            await interaction.followup.send(
                f"Invalid outcome. Choose one of: **{prediction['outcome1']}** or **{prediction['outcome2']}**",
                ephemeral=True,
            )
            return

        await db.close_prediction(self._pool, prediction["id"], outcome)
        bets = await db.get_bets_for_prediction(self._pool, prediction["id"])

        winners = [b for b in bets if b["outcome"] == outcome]
        losers = [b for b in bets if b["outcome"] != outcome]

        bet_results = []
        bulk_awards = []

        for b in winners:
            # Payout is weighted by the dynamic odds locked in at bet time.
            payout = round(b["amount"] * b["odds"])
            profit = payout - b["amount"]
            bulk_awards.append((b["user_id"], payout))
            bet_results.append({"bet_id": b["id"], "points_change": profit})

        for b in losers:
            bet_results.append({"bet_id": b["id"], "points_change": -b["amount"]})

        if bulk_awards:
            await db.award_points_bulk(self._pool, bulk_awards)
        await db.save_bet_results(self._pool, bet_results)

        # Build results embed
        embed = discord.Embed(
            title=f"Prediction Resolved: {outcome}",
            description=prediction["message"],
            color=discord.Color.gold(),
        )

        if winners:
            winner_lines = []
            for b, (_, payout) in zip(winners, bulk_awards):
                profit = payout - b["amount"]
                winner_lines.append(
                    f"<@{b['user_id']}> bet **{b['amount']}** @ **{b['odds']:.2f}×** → "
                    f"won **+{profit}** (total back: {payout}) {self.config.points.emoji}"
                )
            embed.add_field(
                name="Congratulations!",
                value="\n".join(winner_lines) or "*No winners.*",
                inline=False,
            )
        else:
            embed.add_field(name="Congratulations!", value="*No one bet on this outcome.*", inline=False)

        if losers:
            loser_lines = [
                f"<@{b['user_id']}> bet **{b['amount']}** and lost {self.config.points.emoji}"
                for b in losers
            ]
            embed.add_field(
                name="Commiserations",
                value="\n".join(loser_lines),
                inline=False,
            )

        embed.set_footer(
            text=f"Closed by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        embed.timestamp = discord.utils.utcnow()

        channel = self.bot.get_channel(prediction["channel_id"])
        if channel is not None:
            await channel.send(embed=embed)
            await interaction.followup.send("Prediction closed and results posted.", ephemeral=True)
        else:
            await interaction.followup.send(embed=embed)

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
            logger.exception("Unhandled predictions command error: %s", error)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "An error occurred. Check the bot logs.",
                    ephemeral=True,
                )


async def setup(bot: DiscographyBot) -> None:
    cog = PredictionsCog(bot, bot.config)

    predict_starter_role_id = bot.config.roles.predict_starter

    async def predict_starter_check(interaction: discord.Interaction) -> bool:
        if any(r.id == predict_starter_role_id for r in interaction.user.roles):
            return True
        raise app_commands.MissingRole(predict_starter_role_id)

    restricted_commands = {"createpredict", "finishpredict"}
    for cmd in cog.__cog_app_commands__:
        if cmd.name in restricted_commands:
            cmd.add_check(predict_starter_check)

    await bot.add_cog(cog)
