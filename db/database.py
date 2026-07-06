from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg_pool
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


async def create_pool(dsn: str) -> psycopg_pool.AsyncConnectionPool:
    pool = psycopg_pool.AsyncConnectionPool(
        conninfo=dsn,
        min_size=2,
        max_size=10,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    await pool.open()
    return pool


async def run_migrations(pool: psycopg_pool.AsyncConnectionPool) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    # Strip line comments, split on semicolons
    sql_clean = re.sub(r"--[^\n]*", "", sql)
    statements = [s.strip() for s in sql_clean.split(";") if s.strip()]
    async with pool.connection() as conn:
        for stmt in statements:
            await conn.execute(stmt)
    logger.info("Database schema migration complete.")


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------

def compute_period_bounds(reference_dt: datetime) -> tuple[datetime, datetime]:
    """Return (monday_00:00 UTC, sunday_23:59:59 UTC) for the ISO week containing reference_dt."""
    utc = reference_dt.astimezone(timezone.utc)
    monday = utc - timedelta(days=utc.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    sunday = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return monday, sunday


async def ensure_active_period(pool: psycopg_pool.AsyncConnectionPool) -> dict[str, Any]:
    """
    Return the current open submission period, creating one if needed.
    If a stale period exists (ended but not announced), logs a warning and returns it unchanged.
    """
    now = datetime.now(timezone.utc)
    async with pool.connection() as conn:
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
                    "Stale period detected (id=%s, ended=%s). "
                    "Winners not yet announced. Use /dryrun-announce to preview, "
                    "then /force-announce to post results.",
                    row["id"],
                    row["end_time"],
                )
            return dict(row)

        start, end = compute_period_bounds(now)
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO submission_periods (start_time, end_time)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id, start_time, end_time, winners_announced, leaderboard_message_id
                """,
                (start, end),
            )
            new_row = await cur.fetchone()

        if new_row is None:
            # Race condition: re-fetch
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

    logger.info("Created new submission period id=%s (%s → %s)", new_row["id"], start, end)
    return dict(new_row)


async def get_ended_unannounced_period(
    pool: psycopg_pool.AsyncConnectionPool,
) -> dict[str, Any] | None:
    """Return the most recently ended period that hasn't had its winners announced."""
    now = datetime.now(timezone.utc)
    async with pool.connection() as conn:
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
) -> dict[str, Any] | None:
    """Return the most recent unannounced period regardless of whether it has ended."""
    async with pool.connection() as conn:
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
    user_id: int,
    username: str,
    display_name: str,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, username, display_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE
                SET username     = EXCLUDED.username,
                    display_name = EXCLUDED.display_name,
                    updated_at   = now()
            """,
            (user_id, username, display_name),
        )


async def get_user_points(
    pool: psycopg_pool.AsyncConnectionPool,
    user_id: int,
) -> int | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT points FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = await cur.fetchone()
    return row["points"] if row else None


async def upsert_users_bulk(
    pool: psycopg_pool.AsyncConnectionPool,
    users: list[tuple[int, str, str]],
) -> None:
    """users = [(user_id, username, display_name), ...]"""
    async with pool.connection() as conn:
        for user_id, username, display_name in users:
            await conn.execute(
                """
                INSERT INTO users (user_id, username, display_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                    SET username     = EXCLUDED.username,
                        display_name = EXCLUDED.display_name,
                        updated_at   = now()
                """,
                (user_id, username, display_name),
            )


async def award_points_bulk(
    pool: psycopg_pool.AsyncConnectionPool,
    awards: list[tuple[int, int]],
) -> None:
    """Atomically add points to multiple users. awards = [(user_id, points), ...]"""
    async with pool.connection() as conn:
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
    period_id: int,
    user_id: int,
    original_message_id: int,
    embed_message_id: int,
    image_url: str,
) -> int:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO submissions
                    (period_id, user_id, original_message_id, embed_message_id, image_url)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (period_id, user_id, original_message_id, embed_message_id, image_url),
            )
            row = await cur.fetchone()
    return row["id"]


async def get_active_submissions(
    pool: psycopg_pool.AsyncConnectionPool,
    period_id: int,
) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.id, s.user_id, s.embed_message_id, s.trophy_count,
                       s.last_counted_at, s.submitted_at, u.display_name
                FROM submissions s
                JOIN users u ON u.user_id = s.user_id
                WHERE s.period_id = %s AND s.is_deleted = FALSE
                ORDER BY s.submitted_at ASC
                """,
                (period_id,),
            )
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def bulk_update_trophy_counts(
    pool: psycopg_pool.AsyncConnectionPool,
    updates: list[tuple[int, int]],
) -> None:
    """updates = [(submission_id, new_count), ...]"""
    async with pool.connection() as conn:
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
    embed_message_id: int,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE submissions SET is_deleted = TRUE WHERE embed_message_id = %s",
            (embed_message_id,),
        )


async def is_known_embed_message(
    pool: psycopg_pool.AsyncConnectionPool,
    message_id: int,
) -> bool:
    async with pool.connection() as conn:
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
    period_id: int,
    limit: int | None = 10,
) -> list[dict[str, Any]]:
    """
    Returns one row per user ranked by their single highest-voted submission.
    net_trophy_count = max(trophy_count - 1, 0) for the best submission.
    Tiebreaker: earliest submission of the best submission.
    Also returns best_embed_id for linking to the submission.
    """
    async with pool.connection() as conn:
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
                    JOIN users u ON u.user_id = s.user_id
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
    period_id: int,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """
    Returns the single highest-voted submission for each user, ranked globally by that vote count.
    Ties within a user broken by earliest submission; ties across users broken by submission_id.
    """
    async with pool.connection() as conn:
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
                    JOIN users u ON u.user_id = s.user_id
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
    results: list[dict[str, Any]],
) -> None:
    """results = [{"period_id", "user_id", "rank", "trophy_count", "points_awarded"}, ...]"""
    async with pool.connection() as conn:
        for r in results:
            await conn.execute(
                """
                INSERT INTO period_results
                    (period_id, user_id, rank, trophy_count, points_awarded)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (period_id, user_id) DO NOTHING
                """,
                (r["period_id"], r["user_id"], r["rank"], r["trophy_count"], r["points_awarded"]),
            )


async def mark_winners_announced(
    pool: psycopg_pool.AsyncConnectionPool,
    period_id: int,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE submission_periods SET winners_announced = TRUE WHERE id = %s",
            (period_id,),
        )


async def set_leaderboard_message_id(
    pool: psycopg_pool.AsyncConnectionPool,
    period_id: int,
    message_id: int,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE submission_periods SET leaderboard_message_id = %s WHERE id = %s",
            (message_id, period_id),
        )


async def get_leaderboard_message_id(
    pool: psycopg_pool.AsyncConnectionPool,
    period_id: int,
) -> int | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT leaderboard_message_id FROM submission_periods WHERE id = %s",
                (period_id,),
            )
            row = await cur.fetchone()
    return row["leaderboard_message_id"] if row else None


async def set_leaderboard_images_message_id(
    pool: psycopg_pool.AsyncConnectionPool,
    period_id: int,
    message_id: int | None,
) -> None:
    """Store (or clear, with None) the id of the first-place companion message."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE submission_periods SET leaderboard_images_message_id = %s WHERE id = %s",
            (message_id, period_id),
        )


async def get_leaderboard_images_message_id(
    pool: psycopg_pool.AsyncConnectionPool,
    period_id: int,
) -> int | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT leaderboard_images_message_id FROM submission_periods WHERE id = %s",
                (period_id,),
            )
            row = await cur.fetchone()
    return row["leaderboard_images_message_id"] if row else None


# ---------------------------------------------------------------------------
# Coinflip
# ---------------------------------------------------------------------------

async def get_daily_flip_count(
    pool: psycopg_pool.AsyncConnectionPool,
    user_id: int,
) -> int:
    """Count flips made by user today (UTC date)."""
    async with pool.connection() as conn:
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
    user_id: int,
    amount: int,
    won: bool,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO coinflip_log (user_id, amount, won) VALUES (%s, %s, %s)",
            (user_id, amount, won),
        )


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------

async def create_prediction(
    pool: psycopg_pool.AsyncConnectionPool,
    creator_id: int,
    outcome1: str,
    outcome2: str,
    message: str,
    channel_id: int,
) -> int:
    """Insert a new open prediction and return its id."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO predictions (creator_id, outcome1, outcome2, message, channel_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (creator_id, outcome1, outcome2, message, channel_id),
            )
            row = await cur.fetchone()
    return row["id"]


def decimal_odds(outcome_pool: float, other_pool: float, seed: float) -> float:
    """
    Dynamic decimal (European) odds for an outcome given the current staked
    pools, smoothed by ``seed`` virtual liquidity on each side.

        odds = (outcome_pool + other_pool + 2*seed) / (outcome_pool + seed)

    With empty pools both sides start at 2.0. As money piles onto an outcome
    its odds fall (toward 1.0) while the opposing outcome's odds rise. A win
    pays ``round(stake * odds)``, i.e. stake returned plus ``odds - 1`` profit.
    """
    return (outcome_pool + other_pool + 2 * seed) / (outcome_pool + seed)


async def get_prediction_pools(
    pool: psycopg_pool.AsyncConnectionPool,
    prediction_id: int,
) -> dict[str, int]:
    """Total amount staked per outcome for a prediction: {outcome: total_amount}."""
    async with pool.connection() as conn:
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
) -> dict[str, Any] | None:
    """Return the single open (status='open') prediction, or None."""
    async with pool.connection() as conn:
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
    prediction_id: int,
    message_id: int,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE predictions SET embed_message_id = %s WHERE id = %s",
            (message_id, prediction_id),
        )


async def place_bet_and_deduct(
    pool: psycopg_pool.AsyncConnectionPool,
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

    Returns the locked decimal odds, or ``None`` if the user has already bet on
    this prediction.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM prediction_bets WHERE prediction_id = %s AND user_id = %s",
                (prediction_id, user_id),
            )
            if await cur.fetchone():
                return None

            # Pre-bet pools, read inside the transaction so the locked odds
            # reflect a consistent snapshot.
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
            INSERT INTO prediction_bets (prediction_id, user_id, outcome, amount, odds)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (prediction_id, user_id, outcome, amount, odds),
        )
    return odds


async def get_bets_for_prediction(
    pool: psycopg_pool.AsyncConnectionPool,
    prediction_id: int,
) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT b.id, b.user_id, b.outcome, b.amount, b.odds, u.display_name
                FROM prediction_bets b
                JOIN users u ON u.user_id = b.user_id
                WHERE b.prediction_id = %s
                ORDER BY b.placed_at ASC
                """,
                (prediction_id,),
            )
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def close_prediction(
    pool: psycopg_pool.AsyncConnectionPool,
    prediction_id: int,
    winning_outcome: str,
) -> None:
    async with pool.connection() as conn:
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
    results: list[dict[str, Any]],
) -> None:
    """results = [{"bet_id": int, "points_change": int}, ...]"""
    async with pool.connection() as conn:
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
    role_id: int,
    emoji_key: str,
    emoji_str: str,
) -> None:
    """
    Add a role<->emoji pair, or update the emoji if the role is already configured.
    Raises ValueError if emoji_key is already mapped to a different role.
    """
    async with pool.connection() as conn:
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
                INSERT INTO reaction_roles (role_id, emoji_key, emoji_str)
                VALUES (%s, %s, %s)
                ON CONFLICT (role_id) DO UPDATE
                    SET emoji_key = EXCLUDED.emoji_key,
                        emoji_str = EXCLUDED.emoji_str
                """,
                (role_id, emoji_key, emoji_str),
            )


async def remove_reaction_role(
    pool: psycopg_pool.AsyncConnectionPool,
    role_id: int,
) -> bool:
    """Delete a reaction role pairing. Returns True if a row was removed."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM reaction_roles WHERE role_id = %s RETURNING id",
                (role_id,),
            )
            row = await cur.fetchone()
    return row is not None


async def get_reaction_roles(
    pool: psycopg_pool.AsyncConnectionPool,
) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT role_id, emoji_key, emoji_str FROM reaction_roles ORDER BY created_at ASC"
            )
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_reaction_role_by_emoji_key(
    pool: psycopg_pool.AsyncConnectionPool,
    emoji_key: str,
) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT role_id, emoji_key, emoji_str FROM reaction_roles WHERE emoji_key = %s",
                (emoji_key,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Bot settings (generic key/value store)
# ---------------------------------------------------------------------------

async def get_setting(
    pool: psycopg_pool.AsyncConnectionPool,
    key: str,
) -> str | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT value FROM bot_settings WHERE key = %s", (key,))
            row = await cur.fetchone()
    return row["value"] if row else None


async def set_setting(
    pool: psycopg_pool.AsyncConnectionPool,
    key: str,
    value: str,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO bot_settings (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, value),
        )


async def delete_setting(
    pool: psycopg_pool.AsyncConnectionPool,
    key: str,
) -> None:
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM bot_settings WHERE key = %s", (key,))


# ---------------------------------------------------------------------------
# Guess game
# ---------------------------------------------------------------------------

async def get_active_guess_round(
    pool: psycopg_pool.AsyncConnectionPool,
) -> dict[str, Any] | None:
    """The current guess round accepting submissions: active and not yet expired."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, started_by, started_at, expires_at, status
                FROM guess_rounds
                WHERE status = 'active' AND expires_at > now()
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_countable_guess_round(
    pool: psycopg_pool.AsyncConnectionPool,
) -> dict[str, Any] | None:
    """
    The latest still-open round for counting purposes, regardless of expiry.
    A judge can /guess-count even after the 24h submission window has lapsed.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, started_by, started_at, expires_at, status
                FROM guess_rounds
                WHERE status = 'active'
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def start_guess_round(
    pool: psycopg_pool.AsyncConnectionPool,
    started_by: int,
    expires_at: datetime,
) -> int:
    """Close any currently active round(s) and open a fresh one, returning its id."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE guess_rounds SET status = 'closed', closed_at = now() WHERE status = 'active'"
        )
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO guess_rounds (started_by, expires_at)
                VALUES (%s, %s)
                RETURNING id
                """,
                (started_by, expires_at),
            )
            row = await cur.fetchone()
    return row["id"]


async def get_user_guess_categories(
    pool: psycopg_pool.AsyncConnectionPool,
    round_id: int,
    user_id: int,
) -> set[str]:
    """Categories this user has already submitted (and thus locked) in the round."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT category FROM guesses WHERE round_id = %s AND user_id = %s",
                (round_id, user_id),
            )
            rows = await cur.fetchall()
    return {r["category"] for r in rows}


async def create_guess(
    pool: psycopg_pool.AsyncConnectionPool,
    round_id: int,
    user_id: int,
    category: str,
    guess_value: str,
) -> int:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO guesses (round_id, user_id, category, guess_value)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (round_id, user_id, category, guess_value),
            )
            row = await cur.fetchone()
    return row["id"]


async def set_guess_output_message(
    pool: psycopg_pool.AsyncConnectionPool,
    guess_id: int,
    message_id: int,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE guesses SET output_message_id = %s WHERE id = %s",
            (message_id, guess_id),
        )


async def get_guesses_for_round(
    pool: psycopg_pool.AsyncConnectionPool,
    round_id: int,
) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT g.id, g.user_id, g.category, g.guess_value, g.output_message_id,
                       u.display_name
                FROM guesses g
                JOIN users u ON u.user_id = g.user_id
                WHERE g.round_id = %s
                ORDER BY g.submitted_at ASC
                """,
                (round_id,),
            )
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def close_guess_round(
    pool: psycopg_pool.AsyncConnectionPool,
    round_id: int,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE guess_rounds SET status = 'closed', closed_at = now() WHERE id = %s",
            (round_id,),
        )


async def save_guess_results(
    pool: psycopg_pool.AsyncConnectionPool,
    results: list[dict[str, Any]],
) -> None:
    """results = [{"guess_id": int, "points_awarded": int}, ...]"""
    async with pool.connection() as conn:
        for r in results:
            await conn.execute(
                "UPDATE guesses SET points_awarded = %s WHERE id = %s",
                (r["points_awarded"], r["guess_id"]),
            )


# ---------------------------------------------------------------------------
# Landing-rate predictor
# ---------------------------------------------------------------------------

async def get_open_landing_round(
    pool: psycopg_pool.AsyncConnectionPool,
) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, status, outcome, channel_id
                FROM landing_rate_rounds
                WHERE status = 'open'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_or_create_open_landing_round(
    pool: psycopg_pool.AsyncConnectionPool,
    channel_id: int,
) -> dict[str, Any]:
    """Return the open landing-rate round, opening one if none exists."""
    existing = await get_open_landing_round(pool)
    if existing is not None:
        return existing
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO landing_rate_rounds (channel_id)
                VALUES (%s)
                RETURNING id, status, outcome, channel_id
                """,
                (channel_id,),
            )
            row = await cur.fetchone()
    return dict(row)


async def place_landing_bet_and_deduct(
    pool: psycopg_pool.AsyncConnectionPool,
    round_id: int,
    user_id: int,
    guess: int,
    amount: int,
) -> bool:
    """
    Deduct the stake and record a landing-rate bet atomically.
    Returns False (without changes) if the user has already bet on this round.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM landing_rate_bets WHERE round_id = %s AND user_id = %s",
                (round_id, user_id),
            )
            if await cur.fetchone():
                return False

        await conn.execute(
            "UPDATE users SET points = points - %s, updated_at = now() WHERE user_id = %s",
            (amount, user_id),
        )
        await conn.execute(
            """
            INSERT INTO landing_rate_bets (round_id, user_id, guess, amount)
            VALUES (%s, %s, %s, %s)
            """,
            (round_id, user_id, guess, amount),
        )
    return True


async def get_landing_bets(
    pool: psycopg_pool.AsyncConnectionPool,
    round_id: int,
) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT b.id, b.user_id, b.guess, b.amount, u.display_name
                FROM landing_rate_bets b
                JOIN users u ON u.user_id = b.user_id
                WHERE b.round_id = %s
                ORDER BY b.placed_at ASC
                """,
                (round_id,),
            )
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def close_landing_round(
    pool: psycopg_pool.AsyncConnectionPool,
    round_id: int,
    outcome: int,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            """
            UPDATE landing_rate_rounds
            SET status = 'closed', outcome = %s, closed_at = now()
            WHERE id = %s
            """,
            (outcome, round_id),
        )


async def save_landing_results(
    pool: psycopg_pool.AsyncConnectionPool,
    results: list[dict[str, Any]],
) -> None:
    """results = [{"bet_id": int, "points_change": int}, ...]"""
    async with pool.connection() as conn:
        for r in results:
            await conn.execute(
                "UPDATE landing_rate_bets SET points_change = %s WHERE id = %s",
                (r["points_change"], r["bet_id"]),
            )
