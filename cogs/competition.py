from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

import discord
from discord.ext import commands

import db.database as db

if TYPE_CHECKING:
    from main import DiscographyBot

logger = logging.getLogger(__name__)

TROPHY = "🏆"
RANK_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
RANK_POINTS = {1: 1000, 2: 500, 3: 250}

# How long after a period ends before winners are announced. Mirrors the old
# "Sunday 23:59 close -> Monday 07:00 announce" gap, and because period bounds
# are computed in each guild's own timezone this stays correct per-tenant.
ANNOUNCE_DELAY = timedelta(hours=int(os.environ.get("ANNOUNCE_DELAY_HOURS", "7")))


class CompetitionCog(commands.Cog, name="CompetitionCog"):
    def __init__(self, bot: DiscographyBot) -> None:
        self.bot = bot

    @property
    def _pool(self):
        return self.bot.db_pool

    # ------------------------------------------------------------------
    # Count 🏆 reactions on all active submissions for one guild
    # ------------------------------------------------------------------

    async def count_reactions_task(self, guild_id: int, dry_run: bool = False) -> list[dict[str, Any]]:
        config = await self.bot.get_guild_config(guild_id)
        if config is None or config.channels.submissions is None:
            return []

        period = await db.ensure_active_period(self._pool, guild_id, config.tzinfo())
        submissions = await db.get_active_submissions(self._pool, guild_id, period["id"])

        channel = self.bot.get_channel(config.channels.submissions)
        if channel is None:
            logger.error("Guild %s: submission channel %s not found.", guild_id, config.channels.submissions)
            return []

        updates: list[tuple[int, int]] = []
        results: list[dict[str, Any]] = []

        for sub in submissions:
            try:
                msg = await channel.fetch_message(sub["embed_message_id"])
            except discord.NotFound:
                logger.info("Guild %s: embed %s not found, soft-deleting.", guild_id, sub["embed_message_id"])
                if not dry_run:
                    await db.soft_delete_submission(self._pool, guild_id, sub["embed_message_id"])
                continue
            except discord.HTTPException as exc:
                logger.warning("Guild %s: failed to fetch message %s: %s", guild_id, sub["embed_message_id"], exc)
                continue

            if config.competition.mode != "basic":
                for reaction in msg.reactions:
                    if str(reaction.emoji) != TROPHY:
                        try:
                            await msg.clear_reaction(reaction.emoji)
                        except (discord.Forbidden, discord.HTTPException):
                            pass

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
            await db.bulk_update_trophy_counts(self._pool, guild_id, updates)
            logger.info("Guild %s: updated trophy counts for %d submissions.", guild_id, len(updates))

        if not dry_run:
            commands_cog = self.bot.cogs.get("CommandsCog")
            if commands_cog is not None:
                try:
                    await commands_cog.update_leaderboard(guild_id)
                except Exception:
                    logger.exception("Guild %s: failed to update leaderboard after count.", guild_id)

        return results

    # ------------------------------------------------------------------
    # Announce winners for one guild
    # ------------------------------------------------------------------

    async def maybe_announce_for_guild(self, guild_id: int) -> None:
        """Announce winners if the guild has an ended, unannounced period past the delay."""
        config = await self.bot.get_guild_config(guild_id)
        if config is None:
            return
        period = await db.get_ended_unannounced_period(self._pool, guild_id)
        if period is None:
            return
        if datetime.now(timezone.utc) < period["end_time"] + ANNOUNCE_DELAY:
            return
        await self.announce_winners_task(guild_id, dry_run=False, force=False)

    async def announce_winners_task(
        self,
        guild_id: int,
        dry_run: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Announce the top 3 winners, award points, and open the next period.
        When force=True, operates on the current active period even if unfinished.
        """
        config = await self.bot.get_guild_config(guild_id)
        if config is None:
            return {"announcement": None, "awards": []}

        if force:
            period = await db.get_any_unannounced_period(self._pool, guild_id)
        else:
            period = await db.get_ended_unannounced_period(self._pool, guild_id)
        if period is None:
            logger.info("Guild %s: no unannounced period, nothing to announce.", guild_id)
            return {"announcement": None, "awards": []}

        await self.count_reactions_task(guild_id, dry_run=dry_run)

        all_users = await db.get_period_leaderboard(self._pool, guild_id, period["id"], limit=None)
        if not all_users:
            logger.info("Guild %s: no active submissions for period %s.", guild_id, period["id"])
            if not dry_run:
                await db.mark_winners_announced(self._pool, guild_id, period["id"])
                await db.ensure_active_period(self._pool, guild_id, config.tzinfo())
            return {"announcement": "No submissions this week.", "awards": []}

        top3 = all_users[:3]
        period_start = period["start_time"]
        week_label = period_start.strftime("%b %d, %Y") if hasattr(period_start, "strftime") else str(period_start)

        notif = config.roles.notification
        mention = f"<@&{notif}> " if notif else ""
        lines = [f"{mention}{TROPHY} **Week of {week_label} — Results!**\n"]
        rank_results: list[dict[str, Any]] = []
        for rank, user in enumerate(top3, start=1):
            medal = RANK_MEDALS.get(rank, f"{rank}.")
            prize = RANK_POINTS.get(rank, 0)
            suffix = "st" if rank == 1 else "nd" if rank == 2 else "rd"
            lines.append(
                f"{medal} **{rank}{suffix} place**: "
                f"<@{user['user_id']}> — {user['net_trophy_count']} 🏆 "
                f"→ {prize} {config.points.name} {config.points.emoji}"
            )
            rank_results.append({
                "period_id": period["id"],
                "user_id": user["user_id"],
                "rank": rank,
                "trophy_count": user["net_trophy_count"],
                "points_awarded": prize,
            })

        announcement_text = "\n".join(lines)

        top_submissions = await db.get_top_submission_per_user(self._pool, guild_id, period["id"], limit=3)
        submissions_ch = self.bot.get_channel(config.channels.submissions) if config.channels.submissions else None
        submission_embeds: list[discord.Embed] = []
        for rank, sub in enumerate(top_submissions, start=1):
            medal = RANK_MEDALS.get(rank, f"{rank}.")
            image_url = sub["image_url"]

            if submissions_ch is not None:
                try:
                    live_msg = await submissions_ch.fetch_message(sub["embed_message_id"])
                    if live_msg.attachments:
                        image_url = live_msg.attachments[0].url
                    elif live_msg.embeds and live_msg.embeds[0].image:
                        image_url = live_msg.embeds[0].image.proxy_url or image_url
                except (discord.NotFound, discord.HTTPException):
                    pass

            embed = discord.Embed(
                description=f"{medal} <@{sub['user_id']}> — {sub['net_trophy_count']} {TROPHY}",
            )
            embed.set_image(url=image_url)
            submission_embeds.append(embed)

        rank_prizes: dict[int, int] = {r["user_id"]: r["points_awarded"] for r in rank_results}

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning("Guild %s: not in cache; point multipliers skipped.", guild_id)

        all_awards: list[dict[str, Any]] = []
        for user in all_users:
            prize = rank_prizes.get(user["user_id"], 0)
            net = user["net_trophy_count"]
            total = prize + net

            multiplier = 1.0
            if guild is not None and config.multiply_points:
                member = guild.get_member(user["user_id"])
                if member is not None:
                    member_role_ids = {r.id for r in member.roles}
                    applicable = [
                        mp.multiplier
                        for mp in config.multiply_points
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
            announcements_ch = (
                self.bot.get_channel(config.channels.announcements)
                if config.channels.announcements
                else None
            )
            if announcements_ch:
                await announcements_ch.send(
                    announcement_text,
                    embeds=submission_embeds,
                    allowed_mentions=discord.AllowedMentions(roles=True, users=True),
                )
            await db.save_period_results(self._pool, guild_id, rank_results)
            if bulk_awards:
                await db.award_points_bulk(self._pool, guild_id, bulk_awards)
            await db.mark_winners_announced(self._pool, guild_id, period["id"])
            await db.ensure_active_period(self._pool, guild_id, config.tzinfo())

            commands_cog = self.bot.cogs.get("CommandsCog")
            if commands_cog is not None:
                try:
                    await commands_cog.update_leaderboard(guild_id)
                except Exception:
                    logger.exception("Guild %s: failed to update leaderboard after announcement.", guild_id)

        return {"announcement": announcement_text, "awards": all_awards, "submission_embeds": submission_embeds}


async def setup(bot: DiscographyBot) -> None:
    await bot.add_cog(CompetitionCog(bot))
