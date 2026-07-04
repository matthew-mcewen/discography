"""
Process-level settings, loaded from the environment (never from the DB).

These are the operator-owned knobs and secrets: database connection, the bot
token, scheduler cadence, and the management API's auth/billing secrets. Per-guild
product configuration lives in the database (see utils/config.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _csv(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class BotSettings:
    database_url: str
    discord_token: str
    # Optional guild id to also sync commands to for instant propagation in dev.
    dev_guild_id: int | None = None
    count_interval_minutes: int = 10
    announce_interval_minutes: int = 15
    # Seconds a cached GuildConfig is trusted before reload (LISTEN/NOTIFY
    # invalidates sooner when the API changes config).
    config_cache_ttl: int = 60
    db_pool_min: int = 2
    db_pool_max: int = 10

    @classmethod
    def load(cls) -> "BotSettings":
        token = os.environ.get("DISCORD_TOKEN")
        if not token:
            raise EnvironmentError("DISCORD_TOKEN is not set.")
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise EnvironmentError("DATABASE_URL is not set.")
        dev_guild = os.environ.get("DEV_GUILD_ID")
        return cls(
            database_url=dsn,
            discord_token=token,
            dev_guild_id=int(dev_guild) if dev_guild and dev_guild.strip() else None,
            count_interval_minutes=_int("COUNT_INTERVAL_MINUTES", 10),
            announce_interval_minutes=_int("ANNOUNCE_INTERVAL_MINUTES", 15),
            config_cache_ttl=_int("CONFIG_CACHE_TTL", 60),
            db_pool_min=_int("DB_POOL_MIN", 2),
            db_pool_max=_int("DB_POOL_MAX", 10),
        )


@dataclass(frozen=True)
class ApiSettings:
    database_url: str
    # Bootstrap admin key: if set, always accepted as an 'admin'-scope key. Lets
    # you call the API before any api_keys rows exist. Rotate/remove in prod.
    admin_api_key: str | None = None
    # Discord bot token — used by the API to read guild channels/roles and to
    # validate config against what the bot can actually see.
    discord_token: str | None = None
    stripe_api_key: str | None = None
    stripe_webhook_secret: str | None = None
    # Map Stripe price ids -> plan names, e.g. "price_abc=pro,price_def=team".
    stripe_price_plans: dict[str, str] = field(default_factory=dict)
    cors_origins: list[str] = field(default_factory=list)
    db_pool_min: int = 1
    db_pool_max: int = 5

    @classmethod
    def load(cls) -> "ApiSettings":
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise EnvironmentError("DATABASE_URL is not set.")
        price_map: dict[str, str] = {}
        for pair in _csv("STRIPE_PRICE_PLANS"):
            if "=" in pair:
                price_id, plan = pair.split("=", 1)
                price_map[price_id.strip()] = plan.strip()
        return cls(
            database_url=dsn,
            admin_api_key=os.environ.get("ADMIN_API_KEY") or None,
            discord_token=os.environ.get("DISCORD_TOKEN") or None,
            stripe_api_key=os.environ.get("STRIPE_API_KEY") or None,
            stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET") or None,
            stripe_price_plans=price_map,
            cors_origins=_csv("CORS_ORIGINS"),
            db_pool_min=_int("API_DB_POOL_MIN", 1),
            db_pool_max=_int("API_DB_POOL_MAX", 5),
        )
