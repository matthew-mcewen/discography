"""
Provision a single tenant from the command line — useful for self-hosting your
own guild, or seeding the first customer before the dashboard exists.

Reads a YAML file in the original single-tenant config.yaml format and writes it
into the control-plane tables (tenants + guild_configs + guild_multipliers),
marks the tenant provisioned, and optionally mints an admin API key.

Run migrations first (as a privileged role):  python -m db.migrate

Usage:
    python -m scripts.bootstrap_tenant --guild-id 123456789012345678 \
        --config config.yaml --owner-id 111 --plan pro --status active \
        --create-admin-key
"""
from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

from dotenv import load_dotenv

import db.database as db
import db.tenancy as tenancy


def _id_or_none(value: Any) -> int | None:
    """Old config used 0 to mean 'disabled'; treat 0/absent as NULL."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v or None


def _load_yaml_config(path: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    channels = data.get("channels", {})
    roles = data.get("roles", {})
    points = data.get("points", {})
    competition = data.get("competition", {})
    coinflip = data.get("coinflip", {})
    predictions = data.get("predictions", {})

    config_fields = {
        "submissions_channel_id": _id_or_none(channels.get("submissions")),
        "announcements_channel_id": _id_or_none(channels.get("announcements")),
        "bot_commands_channel_id": _id_or_none(channels.get("bot_commands")),
        "leaderboard_channel_id": _id_or_none(channels.get("leaderboard")),
        "reaction_roles_channel_id": _id_or_none(channels.get("reaction_roles")),
        "notification_role_id": _id_or_none(roles.get("notification")),
        "dry_run_admin_role_id": _id_or_none(roles.get("dry_run_admin")),
        "point_awarder_role_id": _id_or_none(roles.get("point_awarder")),
        "predict_starter_role_id": _id_or_none(roles.get("predict_starter")),
        "reaction_role_admin_role_id": _id_or_none(roles.get("reaction_role_admin")),
        "points_name": points.get("name", "Points"),
        "points_emoji": points.get("emoji", "🏆"),
        "competition_mode": competition.get("mode", "dedicated"),
        "coinflip_max_daily_flips": int(coinflip.get("max_daily_flips", 5)),
        "predictions_seed_liquidity": float(predictions.get("seed_liquidity", 100.0)),
        "schedule_timezone": data.get("schedule_timezone", "UTC"),
    }
    # Drop None channel/role ids so DB defaults/NULLs apply cleanly.
    config_fields = {k: v for k, v in config_fields.items() if v is not None}

    multipliers = [
        {"role_id": int(m["role"]), "multiplier": float(m["multiplier"])}
        for m in data.get("multiplyPoints", [])
    ]
    return config_fields, multipliers


async def _run(args: argparse.Namespace) -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set.")

    config_fields, multipliers = _load_yaml_config(args.config)

    pool = await db.create_pool(dsn, min_size=1, max_size=2)
    try:
        await tenancy.upsert_tenant_on_join(pool, args.guild_id, args.name, args.owner_id)
        await tenancy.update_tenant(pool, args.guild_id, {"status": args.status, "plan": args.plan})
        await tenancy.upsert_guild_config(pool, args.guild_id, config_fields)
        if multipliers:
            await tenancy.set_guild_multipliers(pool, args.guild_id, multipliers)
        await tenancy.mark_provisioned(pool, args.guild_id)

        print(f"Provisioned tenant {args.guild_id} (status={args.status}, plan={args.plan}).")
        print(f"  channels/roles set: {sorted(config_fields)}")
        if multipliers:
            print(f"  multipliers: {multipliers}")

        if args.create_admin_key:
            record = await tenancy.create_api_key(pool, name="bootstrap-cli", scope="admin")
            print("\nAdmin API key (store securely — shown once):")
            print(f"  {record['key']}")
    finally:
        await pool.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Provision a Discography tenant from a YAML config.")
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument("--config", default="config.yaml", help="Path to a config.yaml-format file.")
    parser.add_argument("--owner-id", type=int, default=None, help="Discord user id of the owner.")
    parser.add_argument("--name", default=None, help="Cached guild name.")
    parser.add_argument("--plan", default="pro")
    parser.add_argument(
        "--status",
        default="active",
        choices=["pending_setup", "trialing", "active", "past_due", "canceled", "suspended"],
    )
    parser.add_argument("--create-admin-key", action="store_true", help="Also mint an admin API key.")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
