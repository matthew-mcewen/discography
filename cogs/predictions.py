from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import db.database as db
from cogs.checks import FeatureDisabled, get_config, require_role

if TYPE_CHECKING:
    from main import DiscographyBot

logger = logging.getLogger(__name__)


class PredictionsCog(commands.Cog, name="PredictionsCog"):
    def __init__(self, bot: DiscographyBot) -> None:
        self.bot = bot

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
    @require_role("predict_starter")
    async def createpredict(
        self,
        interaction: discord.Interaction,
        outcome1: str,
        outcome2: str,
        message: str,
    ) -> None:
        config = await get_config(interaction)
        guild_id = interaction.guild_id

        existing = await db.get_open_prediction(self._pool, guild_id)
        if existing is not None:
            await interaction.response.send_message(
                "There's already an open prediction. Use `/finishpredict` to close it first.",
                ephemeral=True,
            )
            return

        prediction_id = await db.create_prediction(
            self._pool,
            guild_id,
            creator_id=interaction.user.id,
            outcome1=outcome1,
            outcome2=outcome2,
            message=message,
            channel_id=interaction.channel_id,
        )

        embed = discord.Embed(title="Prediction Open!", description=message, color=discord.Color.blurple())
        seed = config.predictions.seed_liquidity
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
        await db.set_prediction_embed_message(self._pool, guild_id, prediction_id, msg.id)

    # ------------------------------------------------------------------
    # /predict
    # ------------------------------------------------------------------

    async def _outcome_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        prediction = await db.get_open_prediction(self._pool, interaction.guild_id)
        if prediction is None:
            return []
        choices = [prediction["outcome1"], prediction["outcome2"]]
        return [
            app_commands.Choice(name=c, value=c)
            for c in choices
            if current.lower() in c.lower()
        ]

    @app_commands.command(name="predict", description="Place a bet on the open prediction.")
    @app_commands.describe(outcome="The outcome you're betting on", amount="Number of points to wager")
    @app_commands.autocomplete(outcome=_outcome_autocomplete)
    async def predict(self, interaction: discord.Interaction, outcome: str, amount: int) -> None:
        config = await get_config(interaction)
        guild_id = interaction.guild_id

        if amount < 1:
            await interaction.response.send_message("Amount must be at least 1.", ephemeral=True)
            return

        prediction = await db.get_open_prediction(self._pool, guild_id)
        if prediction is None:
            await interaction.response.send_message("There's no open prediction right now.", ephemeral=True)
            return

        valid_outcomes = {prediction["outcome1"], prediction["outcome2"]}
        if outcome not in valid_outcomes:
            await interaction.response.send_message(
                f"Invalid outcome. Choose one of: **{prediction['outcome1']}** or **{prediction['outcome2']}**",
                ephemeral=True,
            )
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

        other_outcome = (
            prediction["outcome2"] if outcome == prediction["outcome1"] else prediction["outcome1"]
        )
        odds = await db.place_bet_and_deduct(
            self._pool,
            guild_id,
            prediction_id=prediction["id"],
            user_id=interaction.user.id,
            outcome=outcome,
            other_outcome=other_outcome,
            amount=amount,
            seed=config.predictions.seed_liquidity,
        )
        if odds is None:
            await interaction.response.send_message(
                "You've already placed a bet on this prediction.", ephemeral=True
            )
            return

        payout = round(amount * odds)
        await interaction.response.send_message(
            f"Bet placed! You wagered **{amount}** {config.points.name} on **{outcome}** "
            f"at odds **{odds:.2f}×**.\n"
            f"If you win you'll get back **{payout}** {config.points.emoji} "
            f"(profit **+{payout - amount}**). Good luck!",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /odds
    # ------------------------------------------------------------------

    @app_commands.command(name="odds", description="Show the current live odds for the open prediction.")
    async def odds(self, interaction: discord.Interaction) -> None:
        config = await get_config(interaction)
        guild_id = interaction.guild_id

        prediction = await db.get_open_prediction(self._pool, guild_id)
        if prediction is None:
            await interaction.response.send_message("There's no open prediction right now.", ephemeral=True)
            return

        pools = await db.get_prediction_pools(self._pool, guild_id, prediction["id"])
        seed = config.predictions.seed_liquidity
        o1, o2 = prediction["outcome1"], prediction["outcome2"]
        pool1, pool2 = pools.get(o1, 0), pools.get(o2, 0)
        odds1 = db.decimal_odds(pool1, pool2, seed)
        odds2 = db.decimal_odds(pool2, pool1, seed)

        embed = discord.Embed(title="Live Odds", description=prediction["message"], color=discord.Color.blurple())
        embed.add_field(name=o1, value=f"**{odds1:.2f}×**\n{pool1} {config.points.name} staked", inline=True)
        embed.add_field(name=o2, value=f"**{odds2:.2f}×**\n{pool2} {config.points.name} staked", inline=True)
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
    @require_role("predict_starter")
    async def finishpredict(self, interaction: discord.Interaction, outcome: str) -> None:
        config = await get_config(interaction)
        guild_id = interaction.guild_id
        await interaction.response.defer()

        prediction = await db.get_open_prediction(self._pool, guild_id)
        if prediction is None:
            await interaction.followup.send("There's no open prediction to close.", ephemeral=True)
            return

        valid_outcomes = {prediction["outcome1"], prediction["outcome2"]}
        if outcome not in valid_outcomes:
            await interaction.followup.send(
                f"Invalid outcome. Choose one of: **{prediction['outcome1']}** or **{prediction['outcome2']}**",
                ephemeral=True,
            )
            return

        await db.close_prediction(self._pool, guild_id, prediction["id"], outcome)
        bets = await db.get_bets_for_prediction(self._pool, guild_id, prediction["id"])

        winners = [b for b in bets if b["outcome"] == outcome]
        losers = [b for b in bets if b["outcome"] != outcome]

        bet_results = []
        bulk_awards = []
        for b in winners:
            payout = round(b["amount"] * b["odds"])
            profit = payout - b["amount"]
            bulk_awards.append((b["user_id"], payout))
            bet_results.append({"bet_id": b["id"], "points_change": profit})
        for b in losers:
            bet_results.append({"bet_id": b["id"], "points_change": -b["amount"]})

        if bulk_awards:
            await db.award_points_bulk(self._pool, guild_id, bulk_awards)
        await db.save_bet_results(self._pool, guild_id, bet_results)

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
                    f"won **+{profit}** (total back: {payout}) {config.points.emoji}"
                )
            embed.add_field(name="Congratulations!", value="\n".join(winner_lines) or "*No winners.*", inline=False)
        else:
            embed.add_field(name="Congratulations!", value="*No one bet on this outcome.*", inline=False)

        if losers:
            loser_lines = [
                f"<@{b['user_id']}> bet **{b['amount']}** and lost {config.points.emoji}"
                for b in losers
            ]
            embed.add_field(name="Commiserations", value="\n".join(loser_lines), inline=False)

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
        if isinstance(error, FeatureDisabled):
            await self._respond(interaction, "Predictions aren't enabled for this server.")
        elif isinstance(error, (app_commands.MissingRole, app_commands.CheckFailure)):
            await self._respond(interaction, "You don't have permission to use this command.")
        else:
            logger.exception("Unhandled predictions command error: %s", error)
            await self._respond(interaction, "An error occurred. Check the bot logs.")

    @staticmethod
    async def _respond(interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: DiscographyBot) -> None:
    await bot.add_cog(PredictionsCog(bot))
