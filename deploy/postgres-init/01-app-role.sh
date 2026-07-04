#!/bin/bash
# Runs once, on first initialisation of the Postgres data directory (compose only).
#
# Creates the non-superuser application role the bot and API connect as. This is
# REQUIRED for row-level security: superusers and BYPASSRLS roles skip RLS, so if
# the app connected as the postgres superuser, tenant isolation would silently be
# off. Migrations run as the superuser and create tables; ALTER DEFAULT
# PRIVILEGES ensures those tables (and their sequences) are usable by the app role.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'discography_app') THEN
            CREATE ROLE discography_app LOGIN PASSWORD '${APP_DB_PASSWORD}';
        END IF;
    END
    \$\$;

    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO discography_app;
    GRANT USAGE ON SCHEMA public TO discography_app;

    -- Existing objects (none yet on first boot, but harmless / idempotent).
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO discography_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO discography_app;

    -- Future objects created by the migration superuser.
    ALTER DEFAULT PRIVILEGES FOR ROLE ${POSTGRES_USER} IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO discography_app;
    ALTER DEFAULT PRIVILEGES FOR ROLE ${POSTGRES_USER} IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO discography_app;
EOSQL
