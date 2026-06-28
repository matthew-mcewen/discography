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
