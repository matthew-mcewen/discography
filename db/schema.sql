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
    id                     SERIAL      PRIMARY KEY,
    start_time             TIMESTAMPTZ NOT NULL,
    end_time               TIMESTAMPTZ NOT NULL,
    winners_announced      BOOLEAN     NOT NULL DEFAULT FALSE,
    leaderboard_message_id BIGINT      NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
