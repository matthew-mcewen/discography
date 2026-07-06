-- Idempotent schema — safe to run on every startup.

CREATE TABLE IF NOT EXISTS users (
    user_id       BIGINT      PRIMARY KEY,
    username      TEXT        NOT NULL,
    display_name  TEXT        NOT NULL,
    points        INTEGER     NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS submission_periods (
    id                            SERIAL      PRIMARY KEY,
    start_time                    TIMESTAMPTZ NOT NULL,
    end_time                      TIMESTAMPTZ NOT NULL,
    winners_announced             BOOLEAN     NOT NULL DEFAULT FALSE,
    leaderboard_message_id        BIGINT      NULL,
    -- Companion message showing the current first-place screenshot(s), pinned
    -- alongside the leaderboard and edited in place after every reaction count.
    leaderboard_images_message_id BIGINT      NULL,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Backfill for databases created before the companion-images column existed.
ALTER TABLE submission_periods
    ADD COLUMN IF NOT EXISTS leaderboard_images_message_id BIGINT NULL;

-- At most one open (unannounced) period can exist per week start.
CREATE UNIQUE INDEX IF NOT EXISTS idx_periods_active
    ON submission_periods (start_time)
    WHERE winners_announced = FALSE;

CREATE TABLE IF NOT EXISTS submissions (
    id                  SERIAL      PRIMARY KEY,
    period_id           INTEGER     NOT NULL REFERENCES submission_periods(id) ON DELETE CASCADE,
    user_id             BIGINT      NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    original_message_id BIGINT      NOT NULL,
    embed_message_id    BIGINT      NOT NULL UNIQUE,
    image_url           TEXT        NOT NULL,
    -- Starts at 1 because the bot immediately adds its own 🏆 reaction.
    -- Net external votes = max(trophy_count - 1, 0).
    trophy_count        INTEGER     NOT NULL DEFAULT 1,
    is_deleted          BOOLEAN     NOT NULL DEFAULT FALSE,
    last_counted_at     TIMESTAMPTZ NULL,
    submitted_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_submissions_period
    ON submissions (period_id)
    WHERE is_deleted = FALSE;

CREATE INDEX IF NOT EXISTS idx_submissions_embed_message
    ON submissions (embed_message_id);

CREATE TABLE IF NOT EXISTS period_results (
    id             SERIAL      PRIMARY KEY,
    period_id      INTEGER     NOT NULL REFERENCES submission_periods(id) ON DELETE CASCADE,
    user_id        BIGINT      NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    rank           INTEGER     NOT NULL,
    trophy_count   INTEGER     NOT NULL,
    points_awarded INTEGER     NOT NULL,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (period_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_period_results_period
    ON period_results (period_id);

CREATE TABLE IF NOT EXISTS coinflip_log (
    id         SERIAL      PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    amount     INTEGER     NOT NULL,
    won        BOOLEAN     NOT NULL,
    flipped_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_coinflip_log_user_date
    ON coinflip_log (user_id, flipped_at);

CREATE TABLE IF NOT EXISTS predictions (
    id               SERIAL      PRIMARY KEY,
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

CREATE TABLE IF NOT EXISTS prediction_bets (
    id            SERIAL           PRIMARY KEY,
    prediction_id INTEGER          NOT NULL REFERENCES predictions(id),
    user_id       BIGINT           NOT NULL,
    outcome       TEXT             NOT NULL,
    amount        INTEGER          NOT NULL,
    -- Decimal odds locked in at the moment the bet was placed, based on the
    -- pool distribution at that time. Payout on a win = round(amount * odds).
    odds          DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    points_change INTEGER          NULL,
    placed_at     TIMESTAMPTZ      NOT NULL DEFAULT now(),
    UNIQUE (prediction_id, user_id)
);

-- Backfill for databases created before the odds column existed.
ALTER TABLE prediction_bets ADD COLUMN IF NOT EXISTS odds DOUBLE PRECISION NOT NULL DEFAULT 1.0;

CREATE TABLE IF NOT EXISTS reaction_roles (
    id          SERIAL      PRIMARY KEY,
    role_id     BIGINT      NOT NULL UNIQUE,
    -- Canonical match key for the emoji: "id:<custom emoji id>" or "name:<unicode char>".
    -- Keyed this way so a renamed custom emote still matches by its stable ID.
    emoji_key   TEXT        NOT NULL UNIQUE,
    -- str(discord.PartialEmoji) — used to re-add the reaction to the panel and for display.
    emoji_str   TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Generic key/value store for small bits of bot state (e.g. the reaction role panel message id).
CREATE TABLE IF NOT EXISTS bot_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Guess game (/guess, /guess-start, /guess-count)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS guess_rounds (
    id         SERIAL      PRIMARY KEY,
    started_by BIGINT      NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    -- 'active' while accepting/awaiting counting, 'closed' once superseded or counted.
    status     TEXT        NOT NULL DEFAULT 'active',
    closed_at  TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_guess_rounds_active
    ON guess_rounds (started_at DESC)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS guesses (
    id                SERIAL      PRIMARY KEY,
    round_id          INTEGER     NOT NULL REFERENCES guess_rounds(id) ON DELETE CASCADE,
    user_id           BIGINT      NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    category          TEXT        NOT NULL,        -- 'airport' | 'aircraft'
    guess_value       TEXT        NOT NULL,
    output_message_id BIGINT      NULL,            -- message posted in the guess-output channel
    points_awarded    INTEGER     NULL,
    submitted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One guess per category per user per round: locks a category in once submitted.
    UNIQUE (round_id, user_id, category)
);

CREATE INDEX IF NOT EXISTS idx_guesses_round
    ON guesses (round_id);

-- ---------------------------------------------------------------------------
-- Landing-rate predictor (/landing-rate, /landing-rate-outcome)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS landing_rate_rounds (
    id         SERIAL      PRIMARY KEY,
    status     TEXT        NOT NULL DEFAULT 'open',   -- 'open' | 'closed'
    outcome    INTEGER     NULL,
    channel_id BIGINT      NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at  TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_landing_rounds_open
    ON landing_rate_rounds (created_at DESC)
    WHERE status = 'open';

CREATE TABLE IF NOT EXISTS landing_rate_bets (
    id            SERIAL      PRIMARY KEY,
    round_id      INTEGER     NOT NULL REFERENCES landing_rate_rounds(id) ON DELETE CASCADE,
    user_id       BIGINT      NOT NULL,
    guess         INTEGER     NOT NULL,
    amount        INTEGER     NOT NULL,
    points_change INTEGER     NULL,
    placed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One bet per user per round.
    UNIQUE (round_id, user_id)
);
