from __future__ import annotations

import asyncio
import logging
import os

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord.ext import commands
from dotenv import load_dotenv

import db.database as db
from utils.config import load_config

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

COGS = [
    "cogs.submissions",
    "cogs.competition",
    "cogs.commands",
    "cogs.admin",
    "cogs.predictions",
    "cogs.reaction_roles",
    "cogs.guess",
    "cogs.landing_rate",
]


class DiscographyBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.reactions = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

        self.db_pool = None
        self.config = None
        self.scheduler: AsyncIOScheduler | None = None

    async def setup_hook(self) -> None:
        # Load config
        self.config = load_config()
        logger.info("Config loaded.")

        # Connect to database
        dsn = os.environ["DATABASE_URL"]
        self.db_pool = await db.create_pool(dsn)
        await db.run_migrations(self.db_pool)

        # Load cogs
        for cog in COGS:
            await self.load_extension(cog)
            logger.info("Loaded cog: %s", cog)

        # Sync slash commands globally and to the dev guild for instant propagation
        await self.tree.sync()
        await self.tree.sync(guild=discord.Object(id=1272200520756494416))
        logger.info("Slash commands synced.")

        # Start scheduler
        self.scheduler = AsyncIOScheduler(timezone="UTC")

        competition_cog = self.cogs["CompetitionCog"]

        self.scheduler.add_job(
            competition_cog.count_reactions_task,
            "interval",
            minutes=10,
            id="count_reactions",
            replace_existing=True,
        )
        self.scheduler.add_job(
            competition_cog.announce_winners_task,
            "cron",
            day_of_week="mon",
            hour=7,
            minute=0,
            second=0,
            id="announce_winners",
            replace_existing=True,
        )

        self.scheduler.start()
        logger.info("Scheduler started.")

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (id=%s)", self.user, self.user.id)
        period = await db.ensure_active_period(self.db_pool)
        logger.info(
            "Active submission period: id=%s (%s → %s)",
            period["id"],
            period["start_time"],
            period["end_time"],
        )

    async def close(self) -> None:
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        if self.db_pool:
            await self.db_pool.close()
        await super().close()

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        logger.exception("Unhandled command error in %s: %s", ctx.command, error)


def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise EnvironmentError("DISCORD_TOKEN not set in environment / .env file.")

    # psycopg requires SelectorEventLoop; Windows defaults to ProactorEventLoop
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    bot = DiscographyBot()
    asyncio.run(bot.start(token))


if __name__ == "__main__":
    main()
