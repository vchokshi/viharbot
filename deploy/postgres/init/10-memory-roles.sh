#!/bin/sh
# First-start initialization of the memory database (ADR-009, issue #40). Runs once, when the data
# volume is empty, as the bootstrap superuser. Creates exactly two login roles and one database:
#
#   memory_migrator  owns the database and the schema; used only by the memory-migrate job
#   memory_service   the one role the memory service uses; table privileges are granted by
#                    migration 0001 and it holds no DDL rights
#
# No agent holds a database credential. Passwords come from the environment and are never in
# source. This script does not re-run: to change a password later, ALTER ROLE it.
set -eu

: "${MEMORY_DB_MIGRATOR_PASSWORD:?MEMORY_DB_MIGRATOR_PASSWORD is required (local Compose: run python scripts/dev_service_auth.py)}"
: "${MEMORY_DB_SERVICE_PASSWORD:?MEMORY_DB_SERVICE_PASSWORD is required (local Compose: run python scripts/dev_service_auth.py)}"
if [ "${#MEMORY_DB_MIGRATOR_PASSWORD}" -lt 16 ] || [ "${#MEMORY_DB_SERVICE_PASSWORD}" -lt 16 ]; then
    echo "memory database passwords must be at least 16 characters" >&2
    exit 1
fi
db="${MEMORY_DB_NAME:-viharbot_memory}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v migrator_pw="$MEMORY_DB_MIGRATOR_PASSWORD" \
    -v service_pw="$MEMORY_DB_SERVICE_PASSWORD" \
    -v dbname="$db" <<'SQL'
CREATE ROLE memory_migrator LOGIN PASSWORD :'migrator_pw';
CREATE ROLE memory_service LOGIN PASSWORD :'service_pw';
CREATE DATABASE :"dbname" OWNER memory_migrator;
REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"dbname" TO memory_service;
SQL
