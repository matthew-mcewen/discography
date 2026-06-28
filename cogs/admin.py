from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from utils.config import BotConfig

if TYPE_CHECKING:
    from main import DiscographyBot

logger = logging.getLogger(__name__)


class AdminCog(commands.Cog, name="AdminCog"):
    def __init__(self, bot: DiscographyBot, config: BotConfig) -> None:
        self.bot = bot
        self.config = config

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
    async def dryrun_count(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        results = await self._competition_cog().count_reactions_task(dry_run=True)

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
    async def dryrun_announce(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await self._competition_cog().announce_winners_task(dry_run=True)

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
                    f"**{a['total']}** {self.config.points.name}"
                )
        else:
            lines.append("*No awards to distribute.*")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ------------------------------------------------------------------
    # /dryrun-leaderboard
    # ------------------------------------------------------------------

    @app_commands.command(
        name="dryrun-leaderboard",
        description="[Admin] Preview the leaderboard embed without posting it.",
    )
    async def dryrun_leaderboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = await self._commands_cog().update_leaderboard(dry_run=True)

        if embed is None:
            await interaction.followup.send("**[DRY RUN]** No leaderboard data.", ephemeral=True)
            return

        embed.set_footer(text="[DRY RUN] Last updated")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /force-announce — for when the bot missed the Monday window
    # ------------------------------------------------------------------

    @app_commands.command(
        name="force-announce",
        description="[Admin] Run winner announcement now (use after bot missed Monday 07:00 UTC).",
    )
    async def force_announce(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await self._competition_cog().announce_winners_task(dry_run=False)

        if result.get("announcement"):
            await interaction.followup.send(
                f"Winners announced.\n\n{result['announcement']}",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "No ended unannounced period found — nothing to announce.",
                ephemeral=True,
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
            logger.exception("Unhandled admin command error: %s", error)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "An error occurred. Check the bot logs.",
                    ephemeral=True,
                )


async def setup(bot: DiscographyBot) -> None:
    cog = AdminCog(bot, bot.config)
    role_id = bot.config.roles.dry_run_admin

    async def role_check(interaction: discord.Interaction) -> bool:
        if any(r.id == role_id for r in interaction.user.roles):
            return True
        raise app_commands.MissingRole(role_id)

    for cmd in cog.__cog_app_commands__:
        cmd.add_check(role_check)

    await bot.add_cog(cog)
