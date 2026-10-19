#!/bin/sh
set -eu
# Run only against a new cluster. Never print passwords or rendered SQL.
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<'SQL'
\getenv app_password APP_DATABASE_PASSWORD
SELECT format('CREATE ROLE webmonkie LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'app_password')
\gexec
CREATE DATABASE webmonkie OWNER webmonkie;
REVOKE ALL ON DATABASE webmonkie FROM PUBLIC;
SQL
