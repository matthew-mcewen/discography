-- 0002_games.sql
-- Adds the guess game and landing-rate predictor, plus the leaderboard
-- companion-images message. Every new data-plane table is guild-scoped and
-- placed under the same FORCED row-level security as migration 0001, so tenant
-- isolation holds even if application code forgets a WHERE guild_id = ?.

-- ---------------------------------------------------------------------------
-- Control-plane: new per-guild config columns (see utils/config.GuildConfig)
-- ---------------------------------------------------------------------------

ALTER TABLE guild_configs
    ADD COLUMN IF NOT EXISTS guess_output_channel_id      BIGINT NULL,
    ADD COLUMN IF NOT EXISTS guess_judge_role_id          BIGINT NULL,
    ADD COLUMN IF NOT EXISTS landing_rate_outcome_role_id BIGINT NULL;

-- ---------------------------------------------------------------------------
-- Leaderboard companion message (first-place screenshot(s), pinned + edited)
-- ---------------------------------------------------------------------------

ALTER TABLE submission_periods
    ADD COLUMN IF NOT EXISTS leaderboard_images_message_id BIGINT NULL;

-- ---------------------------------------------------------------------------
-- DATA PLANE: Guess game (/guess, /guess-start, /guess-count)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS guess_rounds (
    id         BIGSERIAL   PRIMARY KEY,
    guild_id   BIGINT      NOT NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,
    started_by BIGINT      NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    -- 'active' while accepting/awaiting counting, 'closed' once superseded or counted.
    status     TEXT        NOT NULL DEFAULT 'active',
    closed_at  TIMESTAMPTZ NULL,
    CONSTRAINT guess_rounds_status_valid CHECK (status IN ('active', 'closed'))
);

CREATE INDEX IF NOT EXISTS idx_guess_rounds_active
    ON guess_rounds (guild_id, started_at DESC)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS guesses (
    id                BIGSERIAL   PRIMARY KEY,
    guild_id          BIGINT      NOT NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,
    round_id          BIGINT      NOT NULL REFERENCES guess_rounds(id) ON DELETE CASCADE,
    user_id           BIGINT      NOT NULL,
    category          TEXT        NOT NULL,        -- 'airport' | 'aircraft'
    guess_value       TEXT        NOT NULL,
    output_message_id BIGINT      NULL,            -- message posted in the guess-output channel
    points_awarded    INTEGER     NULL,
    submitted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (guild_id, user_id) REFERENCES users(guild_id, user_id) ON DELETE CASCADE,
    -- One guess per category per user per round: locks a category in once submitted.
    UNIQUE (round_id, user_id, category)
);

CREATE INDEX IF NOT EXISTS idx_guesses_round ON guesses (guild_id, round_id);

-- ---------------------------------------------------------------------------
-- DATA PLANE: Landing-rate predictor (/landing-rate, /landing-rate-outcome)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS landing_rate_rounds (
    id         BIGSERIAL   PRIMARY KEY,
    guild_id   BIGINT      NOT NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,
    status     TEXT        NOT NULL DEFAULT 'open',   -- 'open' | 'closed'
    outcome    INTEGER     NULL,
    channel_id BIGINT      NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at  TIMESTAMPTZ NULL,
    CONSTRAINT landing_rounds_status_valid CHECK (status IN ('open', 'closed'))
);

CREATE INDEX IF NOT EXISTS idx_landing_rounds_open
    ON landing_rate_rounds (guild_id, created_at DESC)
    WHERE status = 'open';

CREATE TABLE IF NOT EXISTS landing_rate_bets (
    id            BIGSERIAL   PRIMARY KEY,
    guild_id      BIGINT      NOT NULL REFERENCES tenants(guild_id) ON DELETE CASCADE,
    round_id      BIGINT      NOT NULL REFERENCES landing_rate_rounds(id) ON DELETE CASCADE,
    user_id       BIGINT      NOT NULL,
    guess         INTEGER     NOT NULL,
    amount        INTEGER     NOT NULL,
    points_change INTEGER     NULL,
    placed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One bet per user per round.
    UNIQUE (round_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_landing_bets_round ON landing_rate_bets (guild_id, round_id);

-- ---------------------------------------------------------------------------
-- ROW-LEVEL SECURITY for the new data-plane tables (mirrors migration 0001).
-- ---------------------------------------------------------------------------

DO $rls$
DECLARE
    tbl TEXT;
    data_plane_tables TEXT[] := ARRAY[
        'guess_rounds', 'guesses', 'landing_rate_rounds', 'landing_rate_bets'
    ];
BEGIN
    FOREACH tbl IN ARRAY data_plane_tables LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', tbl);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', tbl);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (guild_id = NULLIF(current_setting(''app.current_guild'', true), '''')::bigint) '
            'WITH CHECK (guild_id = NULLIF(current_setting(''app.current_guild'', true), '''')::bigint)',
            tbl
        );
    END LOOP;
END
$rls$;
