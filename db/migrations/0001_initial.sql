-- 0001_initial.sql
-- Consolidated multi-tenant schema for Discography-as-a-Service.
--
-- Two planes:
--   * CONTROL PLANE (tenants, guild_configs, guild_multipliers, api_keys,
--     stripe_events) — owned by the management API and the bot's lifecycle
--     handlers. No row-level security; these describe *who* the tenants are.
--   * DATA PLANE (everything keyed by guild_id) — per-tenant runtime state.
--     Every data-plane table carries guild_id and is protected by FORCED
--     row-level security so a missing `WHERE guild_id = ?` in application code
--     can never leak one tenant's rows to another.
--
-- The application must connect as a NON-superuser, non-BYPASSRLS role for the
-- RLS policies below to take effect. See DEPLOY.md.

-- ---------------------------------------------------------------------------
-- CONTROL PLANE
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenants (
    guild_id               BIGINT      PRIMARY KEY,
    name                   TEXT        NULL,               -- cached guild name
    owner_user_id          BIGINT      NULL,               -- Discord user who installed/subscribed
    -- Lifecycle: pending_setup -> trialing/active -> past_due -> canceled/suspended
    status                 TEXT        NOT NULL DEFAULT 'pending_setup',
    plan                   TEXT        NOT NULL DEFAULT 'free',
    -- Hard kill-switch independent of billing status (ops / abuse).
    bot_enabled            BOOLEAN     NOT NULL DEFAULT TRUE,
    -- Stripe linkage
    stripe_customer_id     TEXT        NULL,
    stripe_subscription_id TEXT        NULL,
    current_period_end     TIMESTAMPTZ NULL,
    trial_ends_at          TIMESTAMPTZ NULL,
    -- Lifecycle timestamps
    joined_at              TIMESTAMPTZ NOT NULL DEFAULT now(),  -- bot joined the guild
    left_at                TIMESTAMPTZ NULL,                    -- bot removed from guild
    provisioned_at         TIMESTAMPTZ NULL,                    -- config first completed
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenants_status_valid CHECK (
        status IN ('pending_setup', 'trialing', 'active', 'past_due', 'canceled', 'suspended')
    )
);

CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants (status);
CREATE INDEX IF NOT EXISTS idx_tenants_stripe_customer ON tenants (stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_tenants_stripe_subscription ON tenants (stripe_subscription_id);

-- Per-guild configuration — replaces the single-tenant config.yaml.
CREATE TABLE IF NOT EXISTS guild_configs (
    guild_id                    BIGINT PRIMARY KEY REFERENCES tenants(guild_id) ON DELETE CASCADE,
    -- Channels (NULL = not configured / feature disabled)
    submissions_channel_id      BIGINT NULL,
    announcements_channel_id    BIGINT NULL,
    bot_commands_channel_id     BIGINT NULL,
    leaderboard_channel_id      BIGINT NULL,
    reaction_roles_channel_id   BIGINT NULL,
    -- Roles (NULL = feature disabled)
    notification_role_id        BIGINT NULL,
    dry_run_admin_role_id       BIGINT NULL,
    point_awarder_role_id       BIGINT NULL,
    predict_starter_role_id     BIGINT NULL,
    reaction_role_admin_role_id BIGINT NULL,
    -- Points economy
    points_name                 TEXT   NOT NULL DEFAULT 'Points',
    points_emoji                TEXT   NOT NULL DEFAULT U&'\+01F3C6',   -- 🏆
    -- Competition
    competition_mode            TEXT   NOT NULL DEFAULT 'dedicated',
    -- Coinflip
    coinflip_max_daily_flips    INTEGER NOT NULL DEFAULT 5,
    -- Predictions
    predictions_seed_liquidity  DOUBLE PRECISION NOT NULL DEFAULT 100.0,
    -- Weekly schedule anchor timezone (IANA name). Period bounds & announce
    -- timing are computed relative to this.
    schedule_timezone           TEXT   NOT NULL DEFAULT 'UTC',
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT guild_configs_mode_valid CHECK (competition_mode IN ('dedicated', 'basic')),
    CONSTRAINT guild_configs_seed_positive CHECK (predictions_seed_liquidity > 0),
    CONSTRAINT guild_configs_flips_nonneg CHECK (coinflip_max_daily_flips >= 0)
);

-- Optional per-role point multipliers (was config.yaml `multiplyPoints`).
CREATE TABLE IF NOT EXISTS guild_multipliers (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,
    role_id    BIGINT NOT NULL,
    multiplier DOUBLE PRECISION NOT NULL,
    UNIQUE (guild_id, role_id),
    CONSTRAINT guild_multipliers_positive CHECK (multiplier > 0)
);

-- API keys for the management API. Only a hash is stored.
CREATE TABLE IF NOT EXISTS api_keys (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT        NOT NULL,
    key_prefix   TEXT        NOT NULL,             -- non-secret identifier shown in UIs
    key_hash     TEXT        NOT NULL UNIQUE,      -- sha256(full key)
    scope        TEXT        NOT NULL DEFAULT 'admin',  -- 'admin' | 'tenant'
    guild_id     BIGINT      NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,  -- set when scope='tenant'
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ NULL,
    revoked_at   TIMESTAMPTZ NULL,
    CONSTRAINT api_keys_scope_valid CHECK (scope IN ('admin', 'tenant'))
);

CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys (key_prefix);

-- Stripe webhook idempotency ledger.
CREATE TABLE IF NOT EXISTS stripe_events (
    id          TEXT        PRIMARY KEY,           -- Stripe event id (evt_...)
    type        TEXT        NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- DATA PLANE (guild-scoped, RLS-protected)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    guild_id     BIGINT      NOT NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,
    user_id      BIGINT      NOT NULL,
    username     TEXT        NOT NULL,
    display_name TEXT        NOT NULL,
    points       INTEGER     NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS submission_periods (
    id                     BIGSERIAL   PRIMARY KEY,
    guild_id               BIGINT      NOT NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,
    start_time             TIMESTAMPTZ NOT NULL,
    end_time               TIMESTAMPTZ NOT NULL,
    winners_announced      BOOLEAN     NOT NULL DEFAULT FALSE,
    leaderboard_message_id BIGINT      NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one open (unannounced) period per guild per week start.
CREATE UNIQUE INDEX IF NOT EXISTS idx_periods_active
    ON submission_periods (guild_id, start_time)
    WHERE winners_announced = FALSE;
CREATE INDEX IF NOT EXISTS idx_periods_guild ON submission_periods (guild_id);

CREATE TABLE IF NOT EXISTS submissions (
    id                  BIGSERIAL   PRIMARY KEY,
    guild_id            BIGINT      NOT NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,
    period_id           BIGINT      NOT NULL REFERENCES submission_periods(id) ON DELETE CASCADE,
    user_id             BIGINT      NOT NULL,
    original_message_id BIGINT      NOT NULL,
    embed_message_id    BIGINT      NOT NULL,
    image_url           TEXT        NOT NULL,
    -- Starts at 1 because the bot immediately adds its own 🏆 reaction.
    -- Net external votes = max(trophy_count - 1, 0).
    trophy_count        INTEGER     NOT NULL DEFAULT 1,
    is_deleted          BOOLEAN     NOT NULL DEFAULT FALSE,
    last_counted_at     TIMESTAMPTZ NULL,
    submitted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (guild_id, user_id) REFERENCES users(guild_id, user_id) ON DELETE CASCADE,
    UNIQUE (guild_id, embed_message_id)
);

CREATE INDEX IF NOT EXISTS idx_submissions_period
    ON submissions (period_id)
    WHERE is_deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_submissions_embed_message
    ON submissions (guild_id, embed_message_id);

CREATE TABLE IF NOT EXISTS period_results (
    id             BIGSERIAL   PRIMARY KEY,
    guild_id       BIGINT      NOT NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,
    period_id      BIGINT      NOT NULL REFERENCES submission_periods(id) ON DELETE CASCADE,
    user_id        BIGINT      NOT NULL,
    rank           INTEGER     NOT NULL,
    trophy_count   INTEGER     NOT NULL,
    points_awarded INTEGER     NOT NULL,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (period_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_period_results_period ON period_results (period_id);

CREATE TABLE IF NOT EXISTS coinflip_log (
    id         BIGSERIAL   PRIMARY KEY,
    guild_id   BIGINT      NOT NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,
    user_id    BIGINT      NOT NULL,
    amount     INTEGER     NOT NULL,
    won        BOOLEAN     NOT NULL,
    flipped_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_coinflip_log_user_date
    ON coinflip_log (guild_id, user_id, flipped_at);

CREATE TABLE IF NOT EXISTS predictions (
    id               BIGSERIAL   PRIMARY KEY,
    guild_id         BIGINT      NOT NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,
    creator_id       BIGINT      NOT NULL,
    outcome1         TEXT        NOT NULL,
    outcome2         TEXT        NOT NULL,
    message          TEXT        NOT NULL,
    status           TEXT        NOT NULL DEFAULT 'open',
    winning_outcome  TEXT        NULL,
    channel_id       BIGINT      NOT NULL,
    embed_message_id BIGINT      NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Enforce the "single open prediction per guild" invariant at the DB level.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_prediction_per_guild
    ON predictions (guild_id)
    WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_predictions_guild_status ON predictions (guild_id, status);

CREATE TABLE IF NOT EXISTS prediction_bets (
    id            BIGSERIAL        PRIMARY KEY,
    guild_id      BIGINT           NOT NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,
    prediction_id BIGINT           NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,
    user_id       BIGINT           NOT NULL,
    outcome       TEXT             NOT NULL,
    amount        INTEGER          NOT NULL,
    -- Decimal odds locked in at bet time. Payout on a win = round(amount * odds).
    odds          DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    points_change INTEGER          NULL,
    placed_at     TIMESTAMPTZ      NOT NULL DEFAULT now(),
    UNIQUE (prediction_id, user_id)
);

CREATE TABLE IF NOT EXISTS reaction_roles (
    id          BIGSERIAL   PRIMARY KEY,
    guild_id    BIGINT      NOT NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,
    role_id     BIGINT      NOT NULL,
    -- Canonical match key: "id:<custom emoji id>" or "name:<unicode char>".
    emoji_key   TEXT        NOT NULL,
    -- str(discord.PartialEmoji) — used to re-add the reaction and for display.
    emoji_str   TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guild_id, role_id),
    UNIQUE (guild_id, emoji_key)
);

-- Generic per-guild key/value store (e.g. reaction-role panel message id).
CREATE TABLE IF NOT EXISTS bot_settings (
    guild_id BIGINT NOT NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,
    key      TEXT   NOT NULL,
    value    TEXT   NOT NULL,
    PRIMARY KEY (guild_id, key)
);

-- ---------------------------------------------------------------------------
-- ROW-LEVEL SECURITY
--
-- Every data-plane table only exposes rows whose guild_id matches the
-- `app.current_guild` setting established per transaction by the application's
-- guild_connection() helper. FORCE makes this apply to the table owner too, so
-- there is no way to accidentally read across tenants — even a query that
-- forgot its WHERE clause returns only the active tenant's rows.
-- ---------------------------------------------------------------------------

DO $rls$
DECLARE
    tbl TEXT;
    data_plane_tables TEXT[] := ARRAY[
        'users', 'submission_periods', 'submissions', 'period_results',
        'coinflip_log', 'predictions', 'prediction_bets', 'reaction_roles',
        'bot_settings'
    ];
BEGIN
    FOREACH tbl IN ARRAY data_plane_tables LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', tbl);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', tbl);
        -- NULLIF(..., '') is essential: once the custom GUC placeholder is
        -- registered in a session, an unset read returns '' (not NULL), and
        -- ''::bigint would ERROR. Coercing '' -> NULL makes an unset guild
        -- match no rows (default-deny) instead of raising.
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (guild_id = NULLIF(current_setting(''app.current_guild'', true), '''')::bigint) '
            'WITH CHECK (guild_id = NULLIF(current_setting(''app.current_guild'', true), '''')::bigint)',
            tbl
        );
    END LOOP;
END
$rls$;
