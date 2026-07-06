"""
Per-guild configuration shapes.

In the multi-tenant build there is no config.yaml. Each guild's settings live in
the ``guild_configs`` / ``guild_multipliers`` control-plane tables and are
materialised into an immutable ``GuildConfig`` by the config cache
(utils/guild_config.py). The dataclasses below mirror the attribute surface the
cogs already use, so cog logic stays unchanged apart from *which* guild's config
it looks up.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class ChannelConfig:
    submissions: int | None = None
    announcements: int | None = None
    bot_commands: int | None = None
    leaderboard: int | None = None
    reaction_roles: int | None = None
    # Channel where /guess submissions are posted for judging (None = disabled).
    guess_output: int | None = None


@dataclass(frozen=True)
class RoleConfig:
    notification: int | None = None
    dry_run_admin: int | None = None
    point_awarder: int | None = None
    predict_starter: int | None = None
    reaction_role_admin: int | None = None
    # Judges /guess: sees guess_output, reacts 🥇🥈🥉, runs /guess-start & /guess-count.
    guess_judge: int | None = None
    # Allowed to run /landing-rate-outcome.
    landing_rate_outcome: int | None = None


@dataclass(frozen=True)
class PointsConfig:
    name: str = "Points"
    emoji: str = "🏆"


@dataclass(frozen=True)
class PointMultiplier:
    role: int
    multiplier: float


@dataclass(frozen=True)
class CoinflipConfig:
    max_daily_flips: int = 5


@dataclass(frozen=True)
class PredictionsConfig:
    seed_liquidity: float = 100.0


@dataclass(frozen=True)
class CompetitionConfig:
    mode: str = "dedicated"


@dataclass(frozen=True)
class GuildConfig:
    guild_id: int
    channels: ChannelConfig
    roles: RoleConfig
    points: PointsConfig
    competition: CompetitionConfig
    coinflip: CoinflipConfig
    predictions: PredictionsConfig
    multiply_points: tuple[PointMultiplier, ...]
    schedule_timezone: str = "UTC"

    def tzinfo(self) -> tzinfo:
        try:
            return ZoneInfo(self.schedule_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return timezone.utc

    @classmethod
    def from_rows(
        cls,
        guild_id: int,
        config_row: dict[str, Any],
        multiplier_rows: list[dict[str, Any]],
    ) -> "GuildConfig":
        """Build a GuildConfig from a guild_configs row + guild_multipliers rows."""
        return cls(
            guild_id=guild_id,
            channels=ChannelConfig(
                submissions=config_row.get("submissions_channel_id"),
                announcements=config_row.get("announcements_channel_id"),
                bot_commands=config_row.get("bot_commands_channel_id"),
                leaderboard=config_row.get("leaderboard_channel_id"),
                reaction_roles=config_row.get("reaction_roles_channel_id"),
                guess_output=config_row.get("guess_output_channel_id"),
            ),
            roles=RoleConfig(
                notification=config_row.get("notification_role_id"),
                dry_run_admin=config_row.get("dry_run_admin_role_id"),
                point_awarder=config_row.get("point_awarder_role_id"),
                predict_starter=config_row.get("predict_starter_role_id"),
                reaction_role_admin=config_row.get("reaction_role_admin_role_id"),
                guess_judge=config_row.get("guess_judge_role_id"),
                landing_rate_outcome=config_row.get("landing_rate_outcome_role_id"),
            ),
            points=PointsConfig(
                name=config_row.get("points_name") or "Points",
                emoji=config_row.get("points_emoji") or "🏆",
            ),
            competition=CompetitionConfig(mode=config_row.get("competition_mode") or "dedicated"),
            coinflip=CoinflipConfig(
                max_daily_flips=int(config_row.get("coinflip_max_daily_flips") or 5),
            ),
            predictions=PredictionsConfig(
                seed_liquidity=float(config_row.get("predictions_seed_liquidity") or 100.0),
            ),
            multiply_points=tuple(
                PointMultiplier(role=int(m["role_id"]), multiplier=float(m["multiplier"]))
                for m in multiplier_rows
            ),
            schedule_timezone=config_row.get("schedule_timezone") or "UTC",
        )
