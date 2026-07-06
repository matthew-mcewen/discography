from __future__ import annotations

import asyncio
import json
import logging
import os

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord.ext import commands
from dotenv import load_dotenv

import db.database as db
import db.tenancy as tenancy
from db.migrate import run_migrations
from utils.config import GuildConfig
from utils.guild_config import GuildConfigCache
from utils.pg_listener import CHANNEL_BOT_COMMAND, CHANNEL_CONFIG_CHANGED, PgListener
from utils.settings import BotSettings

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
    def __init__(self, settings: BotSettings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.reactions = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

        self.settings = settings
        self.db_pool = None
        self.config_cache: GuildConfigCache | None = None
        self.scheduler: AsyncIOScheduler | None = None
        self.listener: PgListener | None = None
        self._reconciled = False

    # ------------------------------------------------------------------
    # Config access
    # ------------------------------------------------------------------

    async def get_guild_config(self, guild_id: int | None) -> GuildConfig | None:
        """
        The single gate for cog logic: returns a GuildConfig only when the guild
        is serviceable (active subscription + enabled) AND configured, else None.
        """
        if guild_id is None or self.config_cache is None:
            return None
        return await self.config_cache.get(guild_id)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        self.db_pool = await db.create_pool(
            self.settings.database_url,
            min_size=self.settings.db_pool_min,
            max_size=self.settings.db_pool_max,
        )
        # Apply pending migrations (advisory-locked; safe alongside the API).
        await run_migrations(self.settings.database_url)

        self.config_cache = GuildConfigCache(self.db_pool, ttl=self.settings.config_cache_ttl)

        # Real-time invalidation + control commands from the API, over Postgres.
        self.listener = PgListener(self.settings.database_url)
        self.listener.on(CHANNEL_CONFIG_CHANGED, self.config_cache.handle_notify)
        self.listener.on(CHANNEL_BOT_COMMAND, self._handle_bot_command)
        await self.listener.start()

        # Gate every prefix command on serviceability.
        self.add_check(self._prefix_command_gate)
        # Gate every slash command on serviceability.
        self.tree.interaction_check = self._app_command_gate

        for cog in COGS:
            await self.load_extension(cog)
            logger.info("Loaded cog: %s", cog)

        await self.tree.sync()
        if self.settings.dev_guild_id:
            await self.tree.sync(guild=discord.Object(id=self.settings.dev_guild_id))
        logger.info("Slash commands synced.")

        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.scheduler.add_job(
            self._count_tick,
            "interval",
            minutes=self.settings.count_interval_minutes,
            id="count_reactions",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self._announce_tick,
            "interval",
            minutes=self.settings.announce_interval_minutes,
            id="announce_winners",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        logger.info("Scheduler started.")

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (id=%s), in %d guild(s).", self.user, self.user.id, len(self.guilds))
        if self._reconciled:
            return
        self._reconciled = True
        await self._reconcile_guilds()

    async def _reconcile_guilds(self) -> None:
        """Ensure a tenant row exists for every guild the bot is currently in."""
        for guild in self.guilds:
            try:
                await tenancy.upsert_tenant_on_join(
                    self.db_pool, guild.id, guild.name, getattr(guild, "owner_id", None)
                )
                config = await self.get_guild_config(guild.id)
                if config is not None:
                    await db.ensure_active_period(self.db_pool, guild.id, config.tzinfo())
            except Exception:
                logger.exception("Failed to reconcile guild %s", guild.id)

    # ------------------------------------------------------------------
    # Guild lifecycle
    # ------------------------------------------------------------------

    async def on_guild_join(self, guild: discord.Guild) -> None:
        logger.info("Joined guild %s (%s).", guild.id, guild.name)
        try:
            await tenancy.upsert_tenant_on_join(
                self.db_pool, guild.id, guild.name, getattr(guild, "owner_id", None)
            )
        except Exception:
            logger.exception("Failed to register tenant for guild %s", guild.id)
        if self.config_cache:
            self.config_cache.invalidate(guild.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        logger.info("Removed from guild %s (%s).", guild.id, guild.name)
        try:
            await tenancy.mark_tenant_left(self.db_pool, guild.id)
        except Exception:
            logger.exception("Failed to mark tenant left for guild %s", guild.id)
        if self.config_cache:
            self.config_cache.invalidate(guild.id)

    # ------------------------------------------------------------------
    # Command gates
    # ------------------------------------------------------------------

    async def _prefix_command_gate(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False
        return await self.get_guild_config(ctx.guild.id) is not None

    async def _app_command_gate(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None:
            await self._deny(interaction, "This bot only works inside a server.")
            return False
        config = await self.get_guild_config(interaction.guild_id)
        if config is not None:
            # Stash so command bodies can reuse it without a second lookup.
            interaction.extras["config"] = config
            return True
        await self._deny(
            interaction,
            "This server isn't fully set up yet, or its subscription is inactive. "
            "Visit the dashboard to finish setup or renew.",
        )
        return False

    @staticmethod
    async def _deny(interaction: discord.Interaction, message: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # Scheduled fan-out over all serviceable tenants
    # ------------------------------------------------------------------

    async def _count_tick(self) -> None:
        cog = self.cogs.get("CompetitionCog")
        if cog is None:
            return
        guild_ids = await tenancy.list_serviceable_guild_ids(self.db_pool)
        for guild_id in guild_ids:
            try:
                await cog.count_reactions_task(guild_id)
            except Exception:
                logger.exception("count_reactions_task failed for guild %s", guild_id)

    async def _announce_tick(self) -> None:
        cog = self.cogs.get("CompetitionCog")
        if cog is None:
            return
        guild_ids = await tenancy.list_serviceable_guild_ids(self.db_pool)
        for guild_id in guild_ids:
            try:
                await cog.maybe_announce_for_guild(guild_id)
            except Exception:
                logger.exception("maybe_announce_for_guild failed for guild %s", guild_id)

    # ------------------------------------------------------------------
    # Control commands from the API (over the bot_command NOTIFY channel)
    # ------------------------------------------------------------------

    async def _handle_bot_command(self, payload: str) -> None:
        try:
            data = json.loads(payload)
            action = data.get("action")
            guild_id = int(data["guild_id"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("Ignoring malformed bot_command payload: %r", payload)
            return

        if self.config_cache:
            self.config_cache.invalidate(guild_id)

        if action == "invalidate_config":
            return
        if action == "leave":
            guild = self.get_guild(guild_id)
            if guild is not None:
                try:
                    await guild.leave()
                    logger.info("Left guild %s on API command.", guild_id)
                except discord.HTTPException:
                    logger.exception("Failed to leave guild %s", guild_id)
        else:
            logger.warning("Unknown bot_command action: %r", action)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def close(self) -> None:
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        if self.listener:
            await self.listener.stop()
        if self.db_pool:
            await self.db_pool.close()
        await super().close()

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
            return
        logger.exception("Unhandled command error in %s: %s", ctx.command, error)


def main() -> None:
    settings = BotSettings.load()

    # psycopg requires SelectorEventLoop; Windows defaults to ProactorEventLoop.
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    bot = DiscographyBot(settings)
    asyncio.run(bot.start(settings.discord_token))


if __name__ == "__main__":
    main()
