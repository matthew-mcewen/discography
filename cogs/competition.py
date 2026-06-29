from __future__ import annotations

import logging
from datetime import timezone
from typing import Any, TYPE_CHECKING

import discord
from discord.ext import commands

import db.database as db
from utils.config import BotConfig

if TYPE_CHECKING:
    from main import DiscographyBot

logger = logging.getLogger(__name__)

TROPHY = "🏆"
RANK_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
RANK_POINTS = {1: 1000, 2: 500, 3: 250}


class CompetitionCog(commands.Cog):
    def __init__(self, bot: DiscographyBot, config: BotConfig) -> None:
        self.bot = bot
        self.config = config

    @property
    def _pool(self):
        return self.bot.db_pool

    # ------------------------------------------------------------------
    # Scheduled task: count 🏆 reactions on all active submissions
    # ------------------------------------------------------------------

    async def count_reactions_task(self, dry_run: bool = False) -> list[dict[str, Any]]:
        """
        Fetch live 🏆 counts for all active submissions and update the DB.
        Returns a list of result dicts for display/dry-run output.
        """
        period = await db.ensure_active_period(self._pool)
        submissions = await db.get_active_submissions(self._pool, period["id"])

        channel = self.bot.get_channel(self.config.channels.submissions)
        if channel is None:
            logger.error("Submission channel %s not found.", self.config.channels.submissions)
            return []

        updates: list[tuple[int, int]] = []
        results: list[dict[str, Any]] = []

        for sub in submissions:
            try:
                msg = await channel.fetch_message(sub["embed_message_id"])
            except discord.NotFound:
                logger.info("Submission embed %s not found, soft-deleting.", sub["embed_message_id"])
                if not dry_run:
                    await db.soft_delete_submission(self._pool, sub["embed_message_id"])
                continue
            except discord.HTTPException as exc:
                logger.warning("Failed to fetch message %s: %s", sub["embed_message_id"], exc)
                continue

            # Remove any non-🏆 reactions (belt-and-suspenders)
            for reaction in msg.reactions:
                if str(reaction.emoji) != TROPHY:
                    try:
                        await msg.clear_reaction(reaction.emoji)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

            # Find the 🏆 reaction count
            count = 0
            for reaction in msg.reactions:
                if str(reaction.emoji) == TROPHY:
                    count = reaction.count
                    break

            updates.append((sub["id"], count))
            results.append({
                "submission_id": sub["id"],
                "user_id": sub["user_id"],
                "display_name": sub["display_name"],
                "embed_message_id": sub["embed_message_id"],
                "trophy_count": count,
            })

        if not dry_run and updates:
            await db.bulk_update_trophy_counts(self._pool, updates)
            logger.info("Updated trophy counts for %d submissions.", len(updates))

        # Refresh leaderboard after counting (skip during dry-run to avoid side effects)
        if not dry_run:
            commands_cog = self.bot.cogs.get("CommandsCog")
            if commands_cog is not None:
                try:
                    await commands_cog.update_leaderboard()
                except Exception:
                    logger.exception("Failed to update leaderboard after count.")

        return results

    # ------------------------------------------------------------------
    # Scheduled task: announce winners on Monday 07:00 UTC
    # ------------------------------------------------------------------

    async def announce_winners_task(self, dry_run: bool = False) -> dict[str, Any]:
        """
        Announce the top 3 winners, award points to all submitters, and open the next period.
        Returns a dict with the announcement text and award breakdown.
        """
        period = await db.get_ended_unannounced_period(self._pool)
        if period is None:
            logger.info("announce_winners_task: no ended unannounced period found, nothing to do.")
            return {"announcement": None, "awards": []}

        # Final reaction count before announcement
        await self.count_reactions_task(dry_run=dry_run)

        # Aggregate trophies per user (all submitters, no limit)
        all_users = await db.get_period_leaderboard(self._pool, period["id"], limit=None)
        if not all_users:
            logger.info("announce_winners_task: no active submissions for period %s.", period["id"])
            if not dry_run:
                await db.mark_winners_announced(self._pool, period["id"])
                await db.ensure_active_period(self._pool)
            return {"announcement": "No submissions this week.", "awards": []}

        top3 = all_users[:3]
        period_start = period["start_time"]
        week_label = period_start.strftime("%b %d, %Y") if hasattr(period_start, "strftime") else str(period_start)

        # Build announcement lines
        lines = [
            f"<@&{self.config.roles.notification}> {TROPHY} **Week of {week_label} — Results!**\n"
        ]
        rank_results: list[dict[str, Any]] = []
        for rank, user in enumerate(top3, start=1):
            medal = RANK_MEDALS.get(rank, f"{rank}.")
            prize = RANK_POINTS.get(rank, 0)
            suffix = "st" if rank == 1 else "nd" if rank == 2 else "rd"
            lines.append(
                f"{medal} **{rank}{suffix} place**: "
                f"<@{user['user_id']}> — {user['net_trophy_count']} 🏆 "
                f"→ {prize} {self.config.points.name} {self.config.points.emoji}"
            )
            rank_results.append({
                "period_id": period["id"],
                "user_id": user["user_id"],
                "rank": rank,
                "trophy_count": user["net_trophy_count"],
                "points_awarded": prize,
            })

        announcement_text = "\n".join(lines)

        # Build submission embeds: highest-voted submission per user, up to 3
        top_submissions = await db.get_top_submission_per_user(self._pool, period["id"], limit=3)
        submission_embeds: list[discord.Embed] = []
        for rank, sub in enumerate(top_submissions, start=1):
            medal = RANK_MEDALS.get(rank, f"{rank}.")
            embed = discord.Embed(
                description=f"{medal} <@{sub['user_id']}> — {sub['net_trophy_count']} {TROPHY}",
            )
            embed.set_image(url=sub["image_url"])
            submission_embeds.append(embed)

        # Build full awards list: rank prizes + net trophy points for every submitter
        rank_prizes: dict[int, int] = {r["user_id"]: r["points_awarded"] for r in rank_results}

        guild = self.bot.guilds[0] if self.bot.guilds else None
        if guild is None:
            logger.warning("announce_winners_task: bot is in no guilds; point multipliers skipped.")

        all_awards: list[dict[str, Any]] = []
        for user in all_users:
            prize = rank_prizes.get(user["user_id"], 0)
            net = user["net_trophy_count"]
            total = prize + net

            multiplier = 1.0
            if guild is not None and self.config.multiply_points:
                member = guild.get_member(user["user_id"])
                if member is not None:
                    member_role_ids = {r.id for r in member.roles}
                    applicable = [
                        mp.multiplier
                        for mp in self.config.multiply_points
                        if mp.role in member_role_ids
                    ]
                    if applicable:
                        multiplier = max(applicable)

            total = round(total * multiplier)
            all_awards.append({
                "user_id": user["user_id"],
                "display_name": user["display_name"],
                "rank_points": prize,
                "trophy_points": net,
                "multiplier": multiplier,
                "total": total,
            })
        bulk_awards = [(a["user_id"], a["total"]) for a in all_awards if a["total"] > 0]

        if not dry_run:
            announcements_ch = self.bot.get_channel(self.config.channels.announcements)
            if announcements_ch:
                await announcements_ch.send(
                    announcement_text,
                    embeds=submission_embeds,
                    allowed_mentions=discord.AllowedMentions(roles=True, users=True),
                )
            await db.save_period_results(self._pool, rank_results)
            if bulk_awards:
                await db.award_points_bulk(self._pool, bulk_awards)
            await db.mark_winners_announced(self._pool, period["id"])
            await db.ensure_active_period(self._pool)

            commands_cog = self.bot.cogs.get("CommandsCog")
            if commands_cog is not None:
                try:
                    await commands_cog.update_leaderboard()
                except Exception:
                    logger.exception("Failed to update leaderboard after winner announcement.")

        return {"announcement": announcement_text, "awards": all_awards, "submission_embeds": submission_embeds}


async def setup(bot: DiscographyBot) -> None:
    await bot.add_cog(CompetitionCog(bot, bot.config))
