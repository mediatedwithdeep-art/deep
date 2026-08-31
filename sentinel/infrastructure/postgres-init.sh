#!/bin/bash
# pgvector is not bundled with the postgis image. Installed here on first
# boot so `docker compose up` needs no manual database step.
set -e
apt-get update -qq
apt-get install -y -qq --no-install-recommends postgresql-16-pgvector >/dev/null 2>&1 || {
  echo "WARNING: pgvector unavailable; ReID similarity search will be disabled." >&2
  exit 0
}
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
  CREATE EXTENSION IF NOT EXISTS vector;
SQL
echo "pgvector installed"
