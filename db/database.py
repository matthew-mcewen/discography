"""
Data-plane database access — every function here operates on a single tenant.

Isolation is enforced two ways, belt and suspenders:
  1. Each call opens its connection through ``guild_connection(pool, guild_id)``,
     which sets the ``app.current_guild`` GUC for the transaction. Postgres RLS
     policies (see migrations/0001) filter every row by that GUC, so even a query
     that forgot a WHERE clause cannot touch another tenant's data.
  2. INSERTs still write guild_id explicitly (the RLS WITH CHECK clause rejects a
     mismatched guild_id), and conflict targets are guild-scoped.

Control-plane access (tenants, configs, billing) lives in db/tenancy.py.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import psycopg_pool
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)


async def create_pool(
    dsn: str,
    *,
    min_size: int = 2,
    max_size: int = 10,
) -> psycopg_pool.AsyncConnectionPool:
    pool = psycopg_pool.AsyncConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    await pool.open()
    return pool


@asynccontextmanager
async def guild_connection(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
) -> AsyncIterator[Any]:
    """
    Yield a connection whose transaction is pinned to ``guild_id`` via the
    ``app.current_guild`` setting. RLS then scopes every statement to this tenant.

    ``set_config(..., is_local => true)`` is transaction-scoped, so the setting is
    automatically cleared when the pooled connection is returned — no leakage
    between checkouts.
    """
    async with pool.connection() as conn:
        await conn.execute(
            "SELECT set_config('app.current_guild', %s, true)",
            (str(int(guild_id)),),
        )
        yield conn


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------

def compute_period_bounds(
    reference_dt: datetime,
    tz: timezone = timezone.utc,
) -> tuple[datetime, datetime]:
    """
    Return (monday 00:00, sunday 23:59:59) for the ISO week containing
    ``reference_dt``, anchored to ``tz`` (the guild's schedule timezone).
    """
    local = reference_dt.astimezone(tz)
    monday = local - timedelta(days=local.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    sunday = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return monday, sunday


async def ensure_active_period(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    tz: timezone = timezone.utc,
) -> dict[str, Any]:
    """
    Return the guild's current open submission period, creating one if needed.
    A stale (ended but unannounced) period is returned unchanged with a warning.
    """
    now = datetime.now(timezone.utc)
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, start_time, end_time, winners_announced, leaderboard_message_id
                FROM submission_periods
                WHERE winners_announced = FALSE
                ORDER BY start_time DESC
                LIMIT 1
                """
            )
            row = await cur.fetchone()

        if row is not None:
            if row["end_time"] < now:
                logger.warning(
                    "Stale period detected (guild=%s, id=%s, ended=%s). "
                    "Winners not yet announced.",
                    guild_id,
                    row["id"],
                    row["end_time"],
                )
            return dict(row)

        start, end = compute_period_bounds(now, tz)
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO submission_periods (guild_id, start_time, end_time)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id, start_time, end_time, winners_announced, leaderboard_message_id
                """,
                (guild_id, start, end),
            )
            new_row = await cur.fetchone()

        if new_row is None:
            # Race: another worker created it first — re-fetch.
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, start_time, end_time, winners_announced, leaderboard_message_id
                    FROM submission_periods
                    WHERE winners_announced = FALSE
                    ORDER BY start_time DESC
                    LIMIT 1
                    """
                )
                new_row = await cur.fetchone()

    logger.info("Created submission period guild=%s id=%s (%s → %s)", guild_id, new_row["id"], start, end)
    return dict(new_row)


async def get_ended_unannounced_period(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, start_time, end_time, winners_announced, leaderboard_message_id
                FROM submission_periods
                WHERE winners_announced = FALSE AND end_time < %s
                ORDER BY end_time DESC
                LIMIT 1
                """,
                (now,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_any_unannounced_period(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
) -> dict[str, Any] | None:
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, start_time, end_time, winners_announced, leaderboard_message_id
                FROM submission_periods
                WHERE winners_announced = FALSE
                ORDER BY start_time DESC
                LIMIT 1
                """
            )
            row = await cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def upsert_user(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    user_id: int,
    username: str,
    display_name: str,
) -> None:
    async with guild_connection(pool, guild_id) as conn:
        await conn.execute(
            """
            INSERT INTO users (guild_id, user_id, username, display_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (guild_id, user_id) DO UPDATE
                SET username     = EXCLUDED.username,
                    display_name = EXCLUDED.display_name,
                    updated_at   = now()
            """,
            (guild_id, user_id, username, display_name),
        )


async def get_user_points(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    user_id: int,
) -> int | None:
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT points FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = await cur.fetchone()
    return row["points"] if row else None


async def upsert_users_bulk(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    users: list[tuple[int, str, str]],
) -> None:
    """users = [(user_id, username, display_name), ...]"""
    async with guild_connection(pool, guild_id) as conn:
        for user_id, username, display_name in users:
            await conn.execute(
                """
                INSERT INTO users (guild_id, user_id, username, display_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (guild_id, user_id) DO UPDATE
                    SET username     = EXCLUDED.username,
                        display_name = EXCLUDED.display_name,
                        updated_at   = now()
                """,
                (guild_id, user_id, username, display_name),
            )


async def award_points_bulk(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    awards: list[tuple[int, int]],
) -> None:
    """Atomically add points to multiple users. awards = [(user_id, points), ...]"""
    async with guild_connection(pool, guild_id) as conn:
        for user_id, pts in awards:
            await conn.execute(
                "UPDATE users SET points = points + %s, updated_at = now() WHERE user_id = %s",
                (pts, user_id),
            )


# ---------------------------------------------------------------------------
# Submissions
# ---------------------------------------------------------------------------

async def create_submission(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    period_id: int,
    user_id: int,
    original_message_id: int,
    embed_message_id: int,
    image_url: str,
) -> int:
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO submissions
                    (guild_id, period_id, user_id, original_message_id, embed_message_id, image_url)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (guild_id, period_id, user_id, original_message_id, embed_message_id, image_url),
            )
            row = await cur.fetchone()
    return row["id"]


async def get_active_submissions(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    period_id: int,
) -> list[dict[str, Any]]:
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.id, s.user_id, s.embed_message_id, s.trophy_count,
                       s.last_counted_at, s.submitted_at, u.display_name
                FROM submissions s
                JOIN users u ON u.user_id = s.user_id AND u.guild_id = s.guild_id
                WHERE s.period_id = %s AND s.is_deleted = FALSE
                ORDER BY s.submitted_at ASC
                """,
                (period_id,),
            )
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def bulk_update_trophy_counts(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    updates: list[tuple[int, int]],
) -> None:
    """updates = [(submission_id, new_count), ...]"""
    async with guild_connection(pool, guild_id) as conn:
        for sub_id, count in updates:
            await conn.execute(
                """
                UPDATE submissions
                SET trophy_count = %s, last_counted_at = now()
                WHERE id = %s
                """,
                (count, sub_id),
            )


async def soft_delete_submission(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    embed_message_id: int,
) -> None:
    async with guild_connection(pool, guild_id) as conn:
        await conn.execute(
            "UPDATE submissions SET is_deleted = TRUE WHERE embed_message_id = %s",
            (embed_message_id,),
        )


async def is_known_embed_message(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    message_id: int,
) -> bool:
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM submissions WHERE embed_message_id = %s",
                (message_id,),
            )
            row = await cur.fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Leaderboard / period results
# ---------------------------------------------------------------------------

async def get_period_leaderboard(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    period_id: int,
    limit: int | None = 10,
) -> list[dict[str, Any]]:
    """One row per user ranked by their single highest-voted submission."""
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            sql = """
                WITH best_per_user AS (
                    SELECT DISTINCT ON (s.user_id)
                        s.user_id,
                        u.display_name,
                        GREATEST(s.trophy_count - 1, 0) AS net_trophy_count,
                        s.embed_message_id AS best_embed_id,
                        s.submitted_at
                    FROM submissions s
                    JOIN users u ON u.user_id = s.user_id AND u.guild_id = s.guild_id
                    WHERE s.period_id = %s AND s.is_deleted = FALSE
                    ORDER BY s.user_id,
                             GREATEST(s.trophy_count - 1, 0) DESC,
                             s.submitted_at ASC
                )
                SELECT user_id, display_name, net_trophy_count, best_embed_id, submitted_at
                FROM best_per_user
                ORDER BY net_trophy_count DESC, submitted_at ASC
            """
            if limit is None:
                await cur.execute(sql, (period_id,))
            else:
                await cur.execute(sql + " LIMIT %s", (period_id, limit))
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_top_submission_per_user(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    period_id: int,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Highest-voted submission per user, ranked globally by that vote count."""
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                WITH best_per_user AS (
                    SELECT DISTINCT ON (s.user_id)
                        s.id AS submission_id,
                        s.user_id,
                        u.display_name,
                        GREATEST(s.trophy_count - 1, 0) AS net_trophy_count,
                        s.image_url,
                        s.embed_message_id
                    FROM submissions s
                    JOIN users u ON u.user_id = s.user_id AND u.guild_id = s.guild_id
                    WHERE s.period_id = %s AND s.is_deleted = FALSE
                    ORDER BY s.user_id,
                             GREATEST(s.trophy_count - 1, 0) DESC,
                             s.submitted_at ASC
                )
                SELECT * FROM best_per_user
                ORDER BY net_trophy_count DESC, submission_id ASC
                LIMIT %s
                """,
                (period_id, limit),
            )
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def save_period_results(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    results: list[dict[str, Any]],
) -> None:
    """results = [{"period_id", "user_id", "rank", "trophy_count", "points_awarded"}, ...]"""
    async with guild_connection(pool, guild_id) as conn:
        for r in results:
            await conn.execute(
                """
                INSERT INTO period_results
                    (guild_id, period_id, user_id, rank, trophy_count, points_awarded)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (period_id, user_id) DO NOTHING
                """,
                (guild_id, r["period_id"], r["user_id"], r["rank"], r["trophy_count"], r["points_awarded"]),
            )


async def mark_winners_announced(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    period_id: int,
) -> None:
    async with guild_connection(pool, guild_id) as conn:
        await conn.execute(
            "UPDATE submission_periods SET winners_announced = TRUE WHERE id = %s",
            (period_id,),
        )


async def set_leaderboard_message_id(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    period_id: int,
    message_id: int,
) -> None:
    async with guild_connection(pool, guild_id) as conn:
        await conn.execute(
            "UPDATE submission_periods SET leaderboard_message_id = %s WHERE id = %s",
            (message_id, period_id),
        )


async def get_leaderboard_message_id(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    period_id: int,
) -> int | None:
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT leaderboard_message_id FROM submission_periods WHERE id = %s",
                (period_id,),
            )
            row = await cur.fetchone()
    return row["leaderboard_message_id"] if row else None


# ---------------------------------------------------------------------------
# Coinflip
# ---------------------------------------------------------------------------

async def get_daily_flip_count(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    user_id: int,
) -> int:
    """Count flips made by user today (UTC date)."""
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM coinflip_log
                WHERE user_id = %s
                  AND (flipped_at AT TIME ZONE 'UTC')::date = (NOW() AT TIME ZONE 'UTC')::date
                """,
                (user_id,),
            )
            row = await cur.fetchone()
    return row["cnt"] if row else 0


async def record_coinflip(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    user_id: int,
    amount: int,
    won: bool,
) -> None:
    async with guild_connection(pool, guild_id) as conn:
        await conn.execute(
            "INSERT INTO coinflip_log (guild_id, user_id, amount, won) VALUES (%s, %s, %s, %s)",
            (guild_id, user_id, amount, won),
        )


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------

async def create_prediction(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    creator_id: int,
    outcome1: str,
    outcome2: str,
    message: str,
    channel_id: int,
) -> int:
    """Insert a new open prediction and return its id."""
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO predictions (guild_id, creator_id, outcome1, outcome2, message, channel_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (guild_id, creator_id, outcome1, outcome2, message, channel_id),
            )
            row = await cur.fetchone()
    return row["id"]


def decimal_odds(outcome_pool: float, other_pool: float, seed: float) -> float:
    """
    Dynamic decimal (European) odds for an outcome given the current staked
    pools, smoothed by ``seed`` virtual liquidity on each side.

        odds = (outcome_pool + other_pool + 2*seed) / (outcome_pool + seed)
    """
    return (outcome_pool + other_pool + 2 * seed) / (outcome_pool + seed)


async def get_prediction_pools(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    prediction_id: int,
) -> dict[str, int]:
    """Total amount staked per outcome: {outcome: total_amount}."""
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT outcome, COALESCE(SUM(amount), 0) AS total
                FROM prediction_bets
                WHERE prediction_id = %s
                GROUP BY outcome
                """,
                (prediction_id,),
            )
            rows = await cur.fetchall()
    return {r["outcome"]: int(r["total"]) for r in rows}


async def get_open_prediction(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
) -> dict[str, Any] | None:
    """Return the guild's single open prediction, or None."""
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, creator_id, outcome1, outcome2, message, channel_id, embed_message_id
                FROM predictions
                WHERE status = 'open'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def set_prediction_embed_message(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    prediction_id: int,
    message_id: int,
) -> None:
    async with guild_connection(pool, guild_id) as conn:
        await conn.execute(
            "UPDATE predictions SET embed_message_id = %s WHERE id = %s",
            (message_id, prediction_id),
        )


async def place_bet_and_deduct(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    prediction_id: int,
    user_id: int,
    outcome: str,
    other_outcome: str,
    amount: int,
    seed: float,
) -> float | None:
    """
    Deduct points and record a bet atomically, locking in the dynamic odds for
    ``outcome`` based on the pool distribution *before* this bet is added.

    Returns the locked decimal odds, or ``None`` if the user already bet.
    """
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM prediction_bets WHERE prediction_id = %s AND user_id = %s",
                (prediction_id, user_id),
            )
            if await cur.fetchone():
                return None

            await cur.execute(
                """
                SELECT outcome, COALESCE(SUM(amount), 0) AS total
                FROM prediction_bets
                WHERE prediction_id = %s
                GROUP BY outcome
                """,
                (prediction_id,),
            )
            pools = {r["outcome"]: int(r["total"]) for r in await cur.fetchall()}

        outcome_pool = pools.get(outcome, 0)
        other_pool = pools.get(other_outcome, 0)
        odds = decimal_odds(outcome_pool, other_pool, seed)

        await conn.execute(
            "UPDATE users SET points = points - %s, updated_at = now() WHERE user_id = %s",
            (amount, user_id),
        )
        await conn.execute(
            """
            INSERT INTO prediction_bets (guild_id, prediction_id, user_id, outcome, amount, odds)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (guild_id, prediction_id, user_id, outcome, amount, odds),
        )
    return odds


async def get_bets_for_prediction(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    prediction_id: int,
) -> list[dict[str, Any]]:
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT b.id, b.user_id, b.outcome, b.amount, b.odds, u.display_name
                FROM prediction_bets b
                JOIN users u ON u.user_id = b.user_id AND u.guild_id = b.guild_id
                WHERE b.prediction_id = %s
                ORDER BY b.placed_at ASC
                """,
                (prediction_id,),
            )
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def close_prediction(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    prediction_id: int,
    winning_outcome: str,
) -> None:
    async with guild_connection(pool, guild_id) as conn:
        await conn.execute(
            """
            UPDATE predictions
            SET status = 'closed', winning_outcome = %s
            WHERE id = %s
            """,
            (winning_outcome, prediction_id),
        )


async def save_bet_results(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    results: list[dict[str, Any]],
) -> None:
    """results = [{"bet_id": int, "points_change": int}, ...]"""
    async with guild_connection(pool, guild_id) as conn:
        for r in results:
            await conn.execute(
                "UPDATE prediction_bets SET points_change = %s WHERE id = %s",
                (r["points_change"], r["bet_id"]),
            )


# ---------------------------------------------------------------------------
# Reaction roles
# ---------------------------------------------------------------------------

async def upsert_reaction_role(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    role_id: int,
    emoji_key: str,
    emoji_str: str,
) -> None:
    """
    Add a role<->emoji pair, or update the emoji if the role is already configured.
    Raises ValueError if emoji_key is already mapped to a different role.
    """
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT role_id FROM reaction_roles WHERE emoji_key = %s",
                (emoji_key,),
            )
            existing = await cur.fetchone()
            if existing is not None and existing["role_id"] != role_id:
                raise ValueError("emoji_already_mapped")

            await cur.execute(
                """
                INSERT INTO reaction_roles (guild_id, role_id, emoji_key, emoji_str)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (guild_id, role_id) DO UPDATE
                    SET emoji_key = EXCLUDED.emoji_key,
                        emoji_str = EXCLUDED.emoji_str
                """,
                (guild_id, role_id, emoji_key, emoji_str),
            )


async def remove_reaction_role(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    role_id: int,
) -> bool:
    """Delete a reaction role pairing. Returns True if a row was removed."""
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM reaction_roles WHERE role_id = %s RETURNING id",
                (role_id,),
            )
            row = await cur.fetchone()
    return row is not None


async def get_reaction_roles(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
) -> list[dict[str, Any]]:
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT role_id, emoji_key, emoji_str FROM reaction_roles ORDER BY created_at ASC"
            )
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_reaction_role_by_emoji_key(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    emoji_key: str,
) -> dict[str, Any] | None:
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT role_id, emoji_key, emoji_str FROM reaction_roles WHERE emoji_key = %s",
                (emoji_key,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Bot settings (per-guild key/value store)
# ---------------------------------------------------------------------------

async def get_setting(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    key: str,
) -> str | None:
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT value FROM bot_settings WHERE key = %s", (key,))
            row = await cur.fetchone()
    return row["value"] if row else None


async def set_setting(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    key: str,
    value: str,
) -> None:
    async with guild_connection(pool, guild_id) as conn:
        await conn.execute(
            """
            INSERT INTO bot_settings (guild_id, key, value)
            VALUES (%s, %s, %s)
            ON CONFLICT (guild_id, key) DO UPDATE SET value = EXCLUDED.value
            """,
            (guild_id, key, value),
        )


async def delete_setting(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
    key: str,
) -> None:
    async with guild_connection(pool, guild_id) as conn:
        await conn.execute("DELETE FROM bot_settings WHERE key = %s", (key,))


# ---------------------------------------------------------------------------
# Stats (data-plane aggregates, e.g. for the management API)
# ---------------------------------------------------------------------------

async def get_guild_stats(
    pool: psycopg_pool.AsyncConnectionPool,
    guild_id: int,
) -> dict[str, Any]:
    """Per-guild rollup for dashboards: member/points/submission/prediction totals."""
    async with guild_connection(pool, guild_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM users)                                          AS user_count,
                    (SELECT COALESCE(SUM(points), 0) FROM users)                          AS total_points,
                    (SELECT COUNT(*) FROM submissions WHERE is_deleted = FALSE)           AS submission_count,
                    (SELECT COUNT(*) FROM submission_periods)                             AS period_count,
                    (SELECT COUNT(*) FROM predictions WHERE status = 'open')              AS open_predictions,
                    (SELECT COUNT(*) FROM predictions)                                    AS total_predictions
                """
            )
            totals = await cur.fetchone()

            await cur.execute(
                """
                SELECT id, start_time, end_time, winners_announced
                FROM submission_periods
                WHERE winners_announced = FALSE
                ORDER BY start_time DESC
                LIMIT 1
                """
            )
            current_period = await cur.fetchone()

            active_submission_count = 0
            if current_period is not None:
                await cur.execute(
                    "SELECT COUNT(*) AS cnt FROM submissions WHERE period_id = %s AND is_deleted = FALSE",
                    (current_period["id"],),
                )
                row = await cur.fetchone()
                active_submission_count = row["cnt"] if row else 0

    result = dict(totals)
    result["current_period"] = dict(current_period) if current_period else None
    result["active_submission_count"] = active_submission_count
    return result
