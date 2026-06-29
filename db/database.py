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
    Returns one row per user with their aggregated net trophy count across all submissions.
    net_trophy_count = SUM(max(trophy_count - 1, 0)) — excludes the bot's own reaction per submission.
    Ranked by net_trophy_count DESC, then earliest first submission as tiebreaker.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if limit is None:
                await cur.execute(
                    """
                    SELECT u.user_id, u.display_name,
                           SUM(GREATEST(s.trophy_count - 1, 0)) AS net_trophy_count,
                           COUNT(s.id) AS submission_count,
                           MIN(s.submitted_at) AS first_submitted_at
                    FROM submissions s
                    JOIN users u ON u.user_id = s.user_id
                    WHERE s.period_id = %s AND s.is_deleted = FALSE
                    GROUP BY u.user_id, u.display_name
                    ORDER BY net_trophy_count DESC, first_submitted_at ASC
                    """,
                    (period_id,),
                )
            else:
                await cur.execute(
                    """
                    SELECT u.user_id, u.display_name,
                           SUM(GREATEST(s.trophy_count - 1, 0)) AS net_trophy_count,
                           COUNT(s.id) AS submission_count,
                           MIN(s.submitted_at) AS first_submitted_at
                    FROM submissions s
                    JOIN users u ON u.user_id = s.user_id
                    WHERE s.period_id = %s AND s.is_deleted = FALSE
                    GROUP BY u.user_id, u.display_name
                    ORDER BY net_trophy_count DESC, first_submitted_at ASC
                    LIMIT %s
                    """,
                    (period_id, limit),
                )
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
                        s.image_url
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
