from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ChannelConfig:
    submissions: int
    announcements: int
    bot_commands: int
    leaderboard: int
    reaction_roles: int = 0
    guess_output: int = 0


@dataclass(frozen=True)
class RoleConfig:
    notification: int
    dry_run_admin: int
    point_awarder: int = 0
    predict_starter: int = 0
    reaction_role_admin: int = 0
    guess_judge: int = 0
    landing_rate_outcome: int = 0


@dataclass(frozen=True)
class PointsConfig:
    name: str
    emoji: str


@dataclass(frozen=True)
class PointMultiplier:
    role: int
    multiplier: float


@dataclass(frozen=True)
class CoinflipConfig:
    max_daily_flips: int = 5


@dataclass(frozen=True)
class PredictionsConfig:
    # Virtual liquidity seeded on each outcome when computing dynamic odds.
    # Higher values make odds move more slowly as bets come in; with empty
    # pools both sides start at decimal odds of 2.0. Must be > 0.
    seed_liquidity: float = 100.0


@dataclass(frozen=True)
class CompetitionConfig:
    # "dedicated" — full feature set: invalid posts are rejected, valid posts are
    #               deleted and reposted as a voting embed, only the vote emote
    #               is allowed to remain on it.
    # "basic"     — lightweight mode for smaller servers: the vote emote is added
    #               directly to single-image posts in the submissions channel.
    #               The original message and any other reactions are left alone.
    mode: str = "dedicated"


@dataclass(frozen=True)
class BotConfig:
    channels: ChannelConfig
    roles: RoleConfig
    points: PointsConfig
    multiply_points: tuple[PointMultiplier, ...] = ()
    coinflip: CoinflipConfig = field(default_factory=CoinflipConfig)
    predictions: PredictionsConfig = field(default_factory=PredictionsConfig)
    competition: CompetitionConfig = field(default_factory=CompetitionConfig)


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

    coinflip_data = data.get("coinflip", {})
    coinflip = CoinflipConfig(
        max_daily_flips=int(coinflip_data.get("max_daily_flips", 5)),
    )

    predictions_data = data.get("predictions", {})
    seed_liquidity = float(predictions_data.get("seed_liquidity", 100.0))
    if seed_liquidity <= 0:
        raise ValueError("config.yaml 'predictions.seed_liquidity' must be > 0")
    predictions = PredictionsConfig(seed_liquidity=seed_liquidity)

    competition_data = data.get("competition", {})
    mode = str(competition_data.get("mode", "dedicated"))
    if mode not in ("dedicated", "basic"):
        raise ValueError("config.yaml 'competition.mode' must be 'dedicated' or 'basic'")
    competition = CompetitionConfig(mode=mode)

    roles_data = data["roles"]
    roles = RoleConfig(
        notification=roles_data["notification"],
        dry_run_admin=roles_data["dry_run_admin"],
        point_awarder=roles_data.get("point_awarder", 0),
        predict_starter=roles_data.get("predict_starter", 0),
        reaction_role_admin=roles_data.get("reaction_role_admin", 0),
        guess_judge=roles_data.get("guess_judge", 0),
        landing_rate_outcome=roles_data.get("landing_rate_outcome", 0),
    )

    return BotConfig(
        channels=ChannelConfig(**data["channels"]),
        roles=roles,
        points=PointsConfig(**data["points"]),
        multiply_points=multiply_points,
        coinflip=coinflip,
        predictions=predictions,
        competition=competition,
    )
