# Deploying Discography as a Service

Multi-tenant deployment guide: one bot process serves many Discord guilds, a
management API drives onboarding/billing, and Postgres holds everything with
per-tenant row-level security.

```
        ┌──────────────┐         ┌──────────────────┐
Discord │  bot process │◀───────▶│                  │
gateway │ (discord.py) │         │    PostgreSQL    │
        └──────┬───────┘         │  control plane   │
               │  LISTEN/NOTIFY  │  + data plane    │
               ▼                 │  (RLS enforced)  │
        ┌──────────────┐         │                  │
Dashboard│ FastAPI mgmt │◀──────▶│                  │
/Stripe  │     API      │         └──────────────────┘
        └──────────────┘
```

The bot and API never talk HTTP to each other — the API sends control messages
(config-changed, leave-guild) over Postgres `NOTIFY`, and the bot listens.

---

## 1. Architecture at a glance

- **One Discord application / bot token** serves every guild (like MEE6/Carl-bot).
  Customers invite *your* bot; they don't run their own.
- **Control-plane tables** (`tenants`, `guild_configs`, `guild_multipliers`,
  `api_keys`, `stripe_events`) describe who the tenants are. No RLS.
- **Data-plane tables** (users, submissions, predictions, …) carry `guild_id`
  and are protected by **forced row-level security**. Every query is pinned to
  one tenant via the `app.current_guild` setting; a missing `WHERE guild_id`
  cannot leak across tenants.
- A tenant is **serviceable** when `bot_enabled = true`, it is provisioned, the
  bot hasn't left, and its status is `trialing`/`active`/`past_due`. The bot
  ignores events for any non-serviceable guild.

> ⚠️ **Security-critical:** the bot and API MUST connect to Postgres as a
> **non-superuser role without BYPASSRLS**. Superusers skip RLS entirely, which
> silently disables tenant isolation. The compose stack and the SQL in §4 set
> this up for you.

---

## 2. Discord application setup

1. Create one application at <https://discord.com/developers/applications>.
2. Add a bot; copy the token → `DISCORD_TOKEN`.
3. Enable privileged intents: **Server Members** and **Message Content**.
4. Build an invite URL (OAuth2 → URL Generator) with scopes `bot` +
   `applications.commands` and permissions: View Channels, Send Messages,
   Manage Messages, Add Reactions, Embed Links, Read Message History, Mention
   Everyone, Pin Messages, Manage Roles.
5. This invite link is what customers click to add the bot to their server.

When the bot joins a guild it auto-creates a `pending_setup` tenant row. It
starts serving that guild only after its config is completed (via the API or the
bootstrap CLI) and, if you gate on billing, an active subscription.

---

## 3. Option A — Docker Compose (recommended for a single droplet)

```bash
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD, APP_DB_PASSWORD, DATABASE_URL (app role),
# MIGRATION_DATABASE_URL (superuser), DISCORD_TOKEN, ADMIN_API_KEY.
docker compose up -d --build
```

Services:
- `db` — Postgres 16; on first boot it runs `deploy/postgres-init/01-app-role.sh`
  to create the non-superuser `discography_app` role.
- `migrate` — one-shot; applies migrations as the superuser, then exits.
- `bot` — the gateway process (connects as the app role).
- `api` — the management API on port 8080 (connects as the app role).

Put nginx/Caddy in front of the API for TLS. Check health: `curl localhost:8080/health`.

---

## 4. Option B — Managed Postgres + systemd (e.g. DigitalOcean Managed DB)

For a managed database you create the app role yourself. Connect as the admin
user and run:

```sql
CREATE ROLE discography_app LOGIN PASSWORD 'a-strong-password';
GRANT CONNECT ON DATABASE discography TO discography_app;
GRANT USAGE ON SCHEMA public TO discography_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO discography_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO discography_app;
-- so tables created by future migrations are usable by the app role:
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO discography_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO discography_app;
```

Then on the droplet:

```bash
git clone <repo> ~/discography && cd ~/discography
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # DATABASE_URL uses discography_app; run migrate as the admin DSN

# Apply migrations (use a privileged DSN that can CREATE TABLE):
DATABASE_URL='postgresql://admin:...@host:5432/discography' .venv/bin/python -m db.migrate

# Install services (user units):
mkdir -p ~/.config/systemd/user
cp deploy/discography-bot.service deploy/discography-api.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now discography-bot discography-api
loginctl enable-linger "$USER"   # keep user services running after logout
```

The app processes read `DATABASE_URL` (the app role) from `.env`. Migrations are
the only step that needs the privileged DSN.

---

## 5. Onboarding a tenant

**Via the API** (what the dashboard will call):

```bash
# The bot must already be in the guild (tenant row exists as pending_setup).
curl -X PUT localhost:8080/v1/tenants/<guild_id>/config \
  -H "Authorization: Bearer $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{
        "submissions_channel_id": 111, "announcements_channel_id": 222,
        "leaderboard_channel_id": 333, "bot_commands_channel_id": 444,
        "notification_role_id": 555, "dry_run_admin_role_id": 555,
        "points_name": "Points", "points_emoji": "🏆",
        "competition_mode": "dedicated", "schedule_timezone": "UTC"
      }'
```

Setting the three required channels (submissions, announcements, leaderboard)
marks the tenant provisioned and promotes it from `pending_setup` to `trialing`.
`GET /v1/tenants/<id>/discord-meta` returns the guild's channels/roles so a
dashboard can populate pickers.

**Via the CLI** (self-hosting your own guild, reuses the old config.yaml format):

```bash
python -m scripts.bootstrap_tenant --guild-id <id> --config config.yaml \
    --status active --plan pro --create-admin-key
```

---

## 6. Stripe billing (optional)

1. Set `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`, and `STRIPE_PRICE_PLANS`
   (e.g. `price_123=pro,price_456=team`).
2. Point a Stripe webhook at `https://your-api/v1/stripe/webhook` for events:
   `checkout.session.completed`, `customer.subscription.created|updated|deleted`,
   `invoice.paid`, `invoice.payment_failed`.
3. When creating the Checkout Session, set **`client_reference_id` = the Discord
   `guild_id`** (or put `guild_id` in subscription metadata). The webhook uses it
   to link the Stripe customer/subscription to the tenant.

Subscription status maps to tenant status (`active`, `past_due`, `canceled`, …).
The webhook is idempotent (events are recorded only after successful handling)
and nudges the bot to re-evaluate serviceability immediately via NOTIFY.

---

## 7. Operations & scaling

- **Migrations** are advisory-locked, so it's safe for the bot to run them on
  startup while the API is also up. Add new `db/migrations/NNNN_*.sql` files;
  they apply in order, once each.
- **Config changes** take effect within a heartbeat (NOTIFY) or `CONFIG_CACHE_TTL`
  seconds at worst.
- **Single bot instance.** discord.py handles thousands of guilds per process.
  Beyond ~2,500 guilds Discord requires gateway sharding — run multiple bot
  processes with `AutoShardedBot`; the DB layer is already shard-agnostic. Do
  **not** naively run two identical bot processes on the same token/guild set.
- **Backups:** the whole product state is in Postgres — back it up.
- **Key rotation:** issue real keys via `POST /v1/api-keys`, then remove
  `ADMIN_API_KEY` from the environment.
