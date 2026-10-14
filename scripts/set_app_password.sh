#!/bin/sh
# Change the password of the database user the web application connects as, and remember it in the git-ignored .env file.
#   scripts/set_app_password.sh 'a-long-new-password'
# Uses the MySQL administrator account (MYSQL_ADMIN_USER / MYSQL_ADMIN_PASSWORD, default root with an empty password).
set -e
NEW="${1:?usage: set_app_password.sh NEW_PASSWORD (at least 12 characters)}"
[ "${#NEW}" -ge 12 ] || { echo "Choose a password of at least 12 characters." >&2; exit 1; }
case "$NEW" in *"'"*|*'\'*) echo "Please avoid quote and backslash characters in this password." >&2; exit 1;; esac
export MYSQL_PWD="${MYSQL_ADMIN_PASSWORD:-}"
mysql -u "${MYSQL_ADMIN_USER:-root}" -e "ALTER USER 'routewise_app'@'localhost' IDENTIFIED BY '$NEW'"
ENV_FILE="$(dirname "$0")/../.env"
touch "$ENV_FILE"
grep -v '^DB_PASSWORD=' "$ENV_FILE" > "$ENV_FILE.tmp" || true
echo "DB_PASSWORD=$NEW" >> "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE" && chmod 600 "$ENV_FILE"
echo "Database password changed. The application reads it from .env (git-ignored) the next time it starts."
