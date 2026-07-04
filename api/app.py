"""
Discography management API.

Runs as its own process next to the bot, sharing only the Postgres database.
The dashboard/back-office calls it with an admin API key; Stripe calls the
webhook with a signed payload. It never imports discord.py and never opens a
gateway connection — control messages to the bot travel over Postgres NOTIFY.

Run with:  uvicorn api.app:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import db.database as db
from api.routers import config, keys, stats, stripe_webhook, tenants
from utils.settings import ApiSettings

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def create_app() -> FastAPI:
    settings = ApiSettings.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.pool = await db.create_pool(
            settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
        )
        # Migrations are owned by the bot process / `python -m db.migrate`, not
        # the API, to keep a single writer of schema changes.
        try:
            yield
        finally:
            await app.state.pool.close()

    app = FastAPI(title="Discography Management API", version="1.0.0", lifespan=lifespan)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(tenants.router)
    app.include_router(config.router)
    app.include_router(stats.router)
    app.include_router(keys.router)
    app.include_router(stripe_webhook.router)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
