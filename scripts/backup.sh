#!/bin/sh
# Back up the RouteWise database, including tables, data, views, stored routines and triggers.
#   scripts/backup.sh [output-file]
# Uses the MySQL admin account (default: root with an empty password).
#   MYSQL_ADMIN_USER / MYSQL_ADMIN_PASSWORD   admin account        DB_NAME   database (default public_transport)
set -e
DB="${DB_NAME:-public_transport}"
OUT="${1:-backups/${DB}_$(date +%Y%m%d_%H%M%S).sql}"
mkdir -p "$(dirname "$OUT")"
MYSQL_PWD="${MYSQL_ADMIN_PASSWORD:-}" mysqldump -u "${MYSQL_ADMIN_USER:-root}" \
    --routines --triggers --single-transaction --set-gtid-purged=OFF "$DB" > "$OUT"
echo "Backup written to $OUT ($(wc -c < "$OUT" | tr -d ' ') bytes)"
