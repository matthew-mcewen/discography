"""
Versioned SQL migration runner.

Replaces the old "split schema.sql on semicolons at startup" approach, which
corrupted dollar-quoted blocks (e.g. RLS DO-blocks) and raced when more than
one process started at once.

Migrations are plain .sql files in db/migrations/ named ``NNNN_description.sql``.
They are applied in filename order, each inside its own transaction, exactly
once, tracked in the ``schema_migrations`` table. A session-level advisory lock
serialises concurrent runners (bot + API + CLI), so it is safe to call
``run_migrations`` on every process startup.

CLI usage:
    python -m db.migrate                      # apply pending migrations
    python -m db.migrate --status             # list applied / pending
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import psycopg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Arbitrary but fixed key so every process contends on the same advisory lock.
_ADVISORY_LOCK_KEY = 0x0D15C0_09  # "discography"


def _discover_migrations() -> list[tuple[str, Path]]:
    """Return [(version, path), ...] sorted by version (filename stem)."""
    if not MIGRATIONS_DIR.is_dir():
        raise FileNotFoundError(f"Migrations directory not found: {MIGRATIONS_DIR}")
    files = sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)
    return [(p.stem, p) for p in files]


async def _applied_versions(conn: psycopg.AsyncConnection) -> set[str]:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT        PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    async with conn.cursor() as cur:
        await cur.execute("SELECT version FROM schema_migrations")
        rows = await cur.fetchall()
    return {r[0] for r in rows}


async def run_migrations(dsn: str) -> list[str]:
    """
    Apply all pending migrations. Returns the list of versions applied this run.
    Safe to call concurrently from multiple processes.
    """
    applied_now: list[str] = []
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        # Serialise runners. Session-level lock is held until released/disconnect.
        await conn.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        try:
            done = await _applied_versions(conn)
            for version, path in _discover_migrations():
                if version in done:
                    continue
                sql = path.read_text(encoding="utf-8")
                logger.info("Applying migration %s ...", version)
                async with conn.transaction():
                    # No parameters -> psycopg uses the simple protocol and runs
                    # the whole multi-statement script (incl. dollar-quoted blocks).
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)",
                        (version,),
                    )
                applied_now.append(version)
                logger.info("Applied migration %s", version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
    finally:
        await conn.close()

    if applied_now:
        logger.info("Migrations complete: applied %s", ", ".join(applied_now))
    else:
        logger.info("Migrations complete: database already up to date.")
    return applied_now


async def _status(dsn: str) -> None:
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        done = await _applied_versions(conn)
    finally:
        await conn.close()
    print("Migration status:")
    for version, _ in _discover_migrations():
        mark = "applied" if version in done else "PENDING"
        print(f"  [{mark:>7}] {version}")


def _dsn_from_env() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set.")
    return dsn


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Apply Discography database migrations.")
    parser.add_argument("--status", action="store_true", help="Show applied/pending migrations and exit.")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    # psycopg's async mode requires a selector event loop; Windows defaults to
    # the incompatible ProactorEventLoop.
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    dsn = _dsn_from_env()
    if args.status:
        asyncio.run(_status(dsn))
    else:
        asyncio.run(run_migrations(dsn))


if __name__ == "__main__":
    main()
