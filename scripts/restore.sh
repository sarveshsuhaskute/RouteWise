#!/bin/sh
# Restore a backup made by backup.sh into a database (created if missing).
#   scripts/restore.sh backup-file [target-database]
# The target defaults to public_transport. Restoring over an existing database replaces its tables.
# Database users are not part of a dump: run schema.sql (or its GRANT section) to recreate transitiq_app.
set -e
FILE="${1:?usage: restore.sh backup-file [target-database]}"
TARGET="${2:-${DB_NAME:-public_transport}}"
export MYSQL_PWD="${MYSQL_ADMIN_PASSWORD:-}"
mysql -u "${MYSQL_ADMIN_USER:-root}" -e "CREATE DATABASE IF NOT EXISTS \`$TARGET\`"
mysql -u "${MYSQL_ADMIN_USER:-root}" "$TARGET" < "$FILE"
echo "Restored $FILE into database $TARGET"
