#!/bin/bash
# NEXORA Postgres bootstrap: roles, template extensions, databases, privileges.
# Runs once on first container start (docker-entrypoint-initdb.d) as the superuser.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v app_pw="${NEXORA_APP_PASSWORD}" \
  -v mig_pw="${NEXORA_MIGRATOR_PASSWORD}" \
  -v ops_pw="${NEXORA_OPS_PASSWORD}" \
  -v whl_pw="${WH_LOADER_PASSWORD}" <<-'EOSQL'
  -- Ownership role: owns all nexora_app objects; RLS still applies (FORCE RLS)
  CREATE ROLE nexora_owner NOLOGIN CREATEDB;
  -- Migrations only; connects with "-c role=nexora_owner"
  CREATE ROLE nexora_migrator LOGIN PASSWORD :'mig_pw' CREATEDB IN ROLE nexora_owner;
  -- Application role: DML only, subject to RLS
  CREATE ROLE nexora_app LOGIN PASSWORD :'app_pw' NOBYPASSRLS;
  -- Platform ops role (beat dispatchers, retention, super-admin aggregates)
  CREATE ROLE nexora_ops LOGIN PASSWORD :'ops_pw' BYPASSRLS;
  -- Owner of SECURITY DEFINER pre-tenant lookup functions (must bypass RLS, NOLOGIN)
  CREATE ROLE nexora_definer NOLOGIN BYPASSRLS;
  GRANT nexora_definer TO nexora_migrator;
  GRANT nexora_definer TO nexora_owner;
  -- Warehouse loader: owns per-org schemas in nexora_wh; can create per-org reader roles
  CREATE ROLE wh_loader LOGIN PASSWORD :'whl_pw' CREATEROLE;
  -- Warehouse query executor: NOINHERIT member of every per-org reader role (wh_q_<short_id>).
  -- It has NO privileges itself; each guarded query runs after `SET LOCAL ROLE wh_q_<short_id>`.
  CREATE ROLE wh_executor LOGIN PASSWORD :'whl_pw' NOINHERIT;
  -- Test-suite helper: lets the migrator create test databases owned by nexora_owner
  ALTER ROLE nexora_migrator SET statement_timeout = 0;
EOSQL

# Extensions in template1 so every new DB (incl. pytest test DBs) inherits them
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname template1 <<-'EOSQL'
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE EXTENSION IF NOT EXISTS citext;
  CREATE EXTENSION IF NOT EXISTS pgcrypto;
  REVOKE CREATE ON SCHEMA public FROM PUBLIC;
  GRANT USAGE, CREATE ON SCHEMA public TO nexora_owner;
  GRANT USAGE ON SCHEMA public TO nexora_app, nexora_ops;
  GRANT USAGE, CREATE ON SCHEMA public TO nexora_definer;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-'EOSQL'
  CREATE DATABASE nexora_app OWNER nexora_owner TEMPLATE template1;
  CREATE DATABASE nexora_wh OWNER wh_loader TEMPLATE template1;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname nexora_wh <<-'EOSQL'
  REVOKE ALL ON DATABASE nexora_wh FROM PUBLIC;
  GRANT CONNECT ON DATABASE nexora_wh TO wh_loader, wh_executor;
  REVOKE ALL ON SCHEMA public FROM PUBLIC;
  -- per-org schemas org_<short_id> and reader roles wh_q_<short_id> are created by apps.warehouse
EOSQL

echo "NEXORA bootstrap complete"
