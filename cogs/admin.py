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


class AdminCog(commands.Cog, name="AdminCog"):
    def __init__(self, bot: DiscographyBot) -> None:
        self.bot = bot

    @property
    def _pool(self):
        return self.bot.db_pool

    def _competition_cog(self):
        cog = self.bot.cogs.get("CompetitionCog")
        if cog is None:
            raise RuntimeError("CompetitionCog not loaded.")
        return cog

    def _commands_cog(self):
        cog = self.bot.cogs.get("CommandsCog")
        if cog is None:
            raise RuntimeError("CommandsCog not loaded.")
        return cog

    # ------------------------------------------------------------------
    # /dryrun-count
    # ------------------------------------------------------------------

    @app_commands.command(
        name="dryrun-count",
        description="[Admin] Preview reaction count results without writing to the database.",
    )
    @require_role("dry_run_admin")
    async def dryrun_count(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        results = await self._competition_cog().count_reactions_task(interaction.guild_id, dry_run=True)

        if not results:
            await interaction.followup.send("**[DRY RUN]** No active submissions found.", ephemeral=True)
            return

        lines = ["**[DRY RUN] Reaction Count Preview** *(no DB writes)*\n"]
        for r in results:
            net = max(r["trophy_count"] - 1, 0)
            lines.append(
                f"• Submission #{r['submission_id']} — "
                f"{r['display_name']} (<@{r['user_id']}>): "
                f"{r['trophy_count']} total 🏆 ({net} net)"
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ------------------------------------------------------------------
    # /dryrun-announce
    # ------------------------------------------------------------------

    @app_commands.command(
        name="dryrun-announce",
        description="[Admin] Preview the winner announcement without posting or awarding points.",
    )
    @require_role("dry_run_admin")
    async def dryrun_announce(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        config = await get_config(interaction)
        result = await self._competition_cog().announce_winners_task(
            interaction.guild_id, dry_run=True, force=True
        )

        lines = ["**[DRY RUN] Winner Announcement Preview** *(no posts, no DB writes)*\n"]
        if result.get("announcement"):
            lines.append("**Announcement text:**")
            lines.append(result["announcement"])
            lines.append("")

        awards = result.get("awards", [])
        if awards:
            lines.append("**Points breakdown:**")
            for a in awards:
                lines.append(
                    f"• {a['display_name']}: "
                    f"{a['rank_points']} rank + {a['trophy_points']} trophy = "
                    f"**{a['total']}** {config.points.name}"
                )
        else:
            lines.append("*No awards to distribute.*")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

        submission_embeds = result.get("submission_embeds", [])
        if submission_embeds:
            await interaction.followup.send(
                "**Submission embeds (as they will appear in the announcement):**",
                embeds=submission_embeds,
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /dryrun-leaderboard
    # ------------------------------------------------------------------

    @app_commands.command(
        name="dryrun-leaderboard",
        description="[Admin] Preview the leaderboard embed without posting it.",
    )
    @require_role("dry_run_admin")
    async def dryrun_leaderboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = await self._commands_cog().update_leaderboard(interaction.guild_id, dry_run=True)

        if embed is None:
            await interaction.followup.send("**[DRY RUN]** No leaderboard data.", ephemeral=True)
            return

        embed.set_footer(text="[DRY RUN] Last updated")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /force-announce
    # ------------------------------------------------------------------

    @app_commands.command(
        name="force-announce",
        description="[Admin] Run winner announcement now (use after bot missed the scheduled window).",
    )
    @require_role("dry_run_admin")
    async def force_announce(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await self._competition_cog().announce_winners_task(interaction.guild_id, dry_run=False)

        if result.get("announcement"):
            await interaction.followup.send(f"Winners announced.\n\n{result['announcement']}", ephemeral=True)
        else:
            await interaction.followup.send(
                "No ended unannounced period found — nothing to announce.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # /dryrun-simulate
    # ------------------------------------------------------------------

    @app_commands.command(
        name="dryrun-simulate",
        description="[Admin] Run the full Monday pipeline for real (counts + awards). For testing only.",
    )
    @require_role("dry_run_admin")
    async def dryrun_simulate(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        config = await get_config(interaction)

        count_results = await self._competition_cog().count_reactions_task(interaction.guild_id, dry_run=False)

        lines = ["**[SIMULATE] Full Monday pipeline — data committed to DB.**\n"]
        if count_results:
            lines.append(f"**Reactions counted:** {len(count_results)} submission(s) updated.")
        else:
            lines.append("**Reactions counted:** No active submissions.")

        result = await self._competition_cog().announce_winners_task(
            interaction.guild_id, dry_run=False, force=True
        )

        if result.get("announcement"):
            lines.append("\n**Announcement posted.**")
            awards = result.get("awards", [])
            if awards:
                lines.append("\n**Points awarded:**")
                for a in awards:
                    lines.append(
                        f"• {a['display_name']}: "
                        f"{a['rank_points']} rank + {a['trophy_points']} trophy = "
                        f"**{a['total']}** {config.points.name}"
                    )
        else:
            lines.append("\nNo ended unannounced period found — nothing to announce.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ------------------------------------------------------------------
    # /awardpoints
    # ------------------------------------------------------------------

    @app_commands.command(name="awardpoints", description="[Point Awarder] Directly award points to a user.")
    @app_commands.describe(user="The user to award points to", amount="Number of points to award")
    @require_role("point_awarder")
    async def awardpoints(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: int,
    ) -> None:
        config = await get_config(interaction)
        if amount <= 0:
            await interaction.response.send_message("Amount must be a positive number.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        await db.upsert_user(self._pool, guild_id, user.id, user.name, user.display_name)
        await db.award_points_bulk(self._pool, guild_id, [(user.id, amount)])
        new_balance = await db.get_user_points(self._pool, guild_id, user.id)

        embed = discord.Embed(title="Points Awarded", color=discord.Color.green())
        embed.add_field(name="Recipient", value=user.mention, inline=True)
        embed.add_field(
            name="Awarded",
            value=f"+{amount} {config.points.name} {config.points.emoji}",
            inline=True,
        )
        embed.add_field(name="New Balance", value=f"{new_balance} {config.points.name}", inline=True)
        embed.set_footer(text=f"Awarded by {interaction.user.display_name}")
        embed.timestamp = discord.utils.utcnow()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /awardpoints-everyone
    # ------------------------------------------------------------------

    @app_commands.command(
        name="awardpoints-everyone",
        description="[Point Awarder] Award points to every member in the server.",
    )
    @app_commands.describe(amount="Number of points to award to each member")
    @require_role("point_awarder")
    async def awardpoints_everyone(self, interaction: discord.Interaction, amount: int) -> None:
        config = await get_config(interaction)
        if amount <= 0:
            await interaction.response.send_message("Amount must be a positive number.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        members = [m for m in interaction.guild.members if not m.bot]

        await db.upsert_users_bulk(self._pool, guild_id, [(m.id, m.name, m.display_name) for m in members])
        await db.award_points_bulk(self._pool, guild_id, [(m.id, amount) for m in members])

        embed = discord.Embed(title="Points Awarded to Everyone", color=discord.Color.green())
        embed.add_field(name="Recipients", value=str(len(members)), inline=True)
        embed.add_field(
            name="Per Person",
            value=f"+{amount} {config.points.name} {config.points.emoji}",
            inline=True,
        )
        embed.add_field(
            name="Total Distributed",
            value=f"{len(members) * amount} {config.points.name}",
            inline=True,
        )
        embed.set_footer(text=f"Awarded by {interaction.user.display_name}")
        embed.timestamp = discord.utils.utcnow()
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, FeatureDisabled):
            await self._respond(interaction, "That feature isn't enabled for this server.")
        elif isinstance(error, (app_commands.MissingRole, app_commands.CheckFailure)):
            await self._respond(interaction, "You don't have permission to use this command.")
        else:
            logger.exception("Unhandled admin command error: %s", error)
            await self._respond(interaction, "An error occurred. Check the bot logs.")

    @staticmethod
    async def _respond(interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: DiscographyBot) -> None:
    await bot.add_cog(AdminCog(bot))
