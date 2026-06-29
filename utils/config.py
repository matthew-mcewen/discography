from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ChannelConfig:
    submissions: int
    announcements: int
    bot_commands: int
    leaderboard: int


@dataclass(frozen=True)
class RoleConfig:
    notification: int
    dry_run_admin: int


@dataclass(frozen=True)
class PointsConfig:
    name: str
    emoji: str


@dataclass(frozen=True)
class PointMultiplier:
    role: int
    multiplier: float


@dataclass(frozen=True)
class BotConfig:
    channels: ChannelConfig
    roles: RoleConfig
    points: PointsConfig
    multiply_points: tuple[PointMultiplier, ...] = ()


def load_config(path: str = "config.yaml") -> BotConfig:
    with open(Path(path), "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    required = {
        "channels": ["submissions", "announcements", "bot_commands", "leaderboard"],
        "roles": ["notification", "dry_run_admin"],
        "points": ["name", "emoji"],
    }
    for section, keys in required.items():
        if section not in data:
            raise ValueError(f"config.yaml missing section: '{section}'")
        for key in keys:
            if key not in data[section]:
                raise ValueError(f"config.yaml missing '{section}.{key}'")

    multiply_points = tuple(
        PointMultiplier(role=int(m["role"]), multiplier=float(m["multiplier"]))
        for m in data.get("multiplyPoints", [])
    )

    return BotConfig(
        channels=ChannelConfig(**data["channels"]),
        roles=RoleConfig(**data["roles"]),
        points=PointsConfig(**data["points"]),
        multiply_points=multiply_points,
    )
