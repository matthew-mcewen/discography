"""Pydantic request/response models for the management API."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------

class TenantCreate(BaseModel):
    guild_id: int
    name: Optional[str] = None
    owner_user_id: Optional[int] = None
    plan: Optional[str] = None
    status: Optional[str] = None


class TenantUpdate(BaseModel):
    name: Optional[str] = None
    owner_user_id: Optional[int] = None
    status: Optional[
        Literal["pending_setup", "trialing", "active", "past_due", "canceled", "suspended"]
    ] = None
    plan: Optional[str] = None
    bot_enabled: Optional[bool] = None
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None


class TenantOut(BaseModel):
    guild_id: int
    name: Optional[str] = None
    owner_user_id: Optional[int] = None
    status: str
    plan: str
    bot_enabled: bool
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    current_period_end: Optional[datetime] = None
    trial_ends_at: Optional[datetime] = None
    joined_at: Optional[datetime] = None
    left_at: Optional[datetime] = None
    provisioned_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class MultiplierIn(BaseModel):
    role_id: int
    multiplier: float = Field(gt=0)


class ConfigUpdate(BaseModel):
    submissions_channel_id: Optional[int] = None
    announcements_channel_id: Optional[int] = None
    bot_commands_channel_id: Optional[int] = None
    leaderboard_channel_id: Optional[int] = None
    reaction_roles_channel_id: Optional[int] = None
    notification_role_id: Optional[int] = None
    dry_run_admin_role_id: Optional[int] = None
    point_awarder_role_id: Optional[int] = None
    predict_starter_role_id: Optional[int] = None
    reaction_role_admin_role_id: Optional[int] = None
    points_name: Optional[str] = Field(default=None, max_length=100)
    points_emoji: Optional[str] = Field(default=None, max_length=100)
    competition_mode: Optional[Literal["dedicated", "basic"]] = None
    coinflip_max_daily_flips: Optional[int] = Field(default=None, ge=0)
    predictions_seed_liquidity: Optional[float] = Field(default=None, gt=0)
    schedule_timezone: Optional[str] = None
    # If provided (even as []), replaces the full multiplier set.
    multipliers: Optional[list[MultiplierIn]] = None

    @field_validator("schedule_timezone")
    @classmethod
    def _validate_tz(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"Unknown IANA timezone: {v!r}")
        return v


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scope: Literal["admin", "tenant"] = "admin"
    guild_id: Optional[int] = None


class ApiKeyOut(BaseModel):
    id: int
    name: str
    key_prefix: str
    scope: str
    guild_id: Optional[int] = None
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class ApiKeyCreated(ApiKeyOut):
    # The full secret, returned exactly once at creation time.
    key: str
