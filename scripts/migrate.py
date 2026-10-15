"""Upgrade an existing RouteWise database to the latest schema version without losing data.

    .venv/bin/python scripts/migrate.py                 # upgrade the database named by DB_NAME (default public_transport)
    .venv/bin/python scripts/migrate.py --db my_db      # another database
    .venv/bin/python scripts/migrate.py --status        # show the current and the latest version, change nothing
    .venv/bin/python scripts/migrate.py --no-backup     # skip the automatic backup (not recommended)

How it works: the database records its version in the SchemaVersion table (a database from before versioning counts as
version 1). Every file in migrations/ named NNNN_description.sql with NNNN above the current version is applied in order.
A backup is taken first (scripts/backup.sh), because MySQL cannot roll back structural changes.
A brand-new database does not need this: import schema.sql, which already is the latest version.
Uses the MySQL administrator account: MYSQL_ADMIN_USER / MYSQL_ADMIN_PASSWORD (default root, empty password).
"""
import argparse
import glob
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_USER = os.environ.get('MYSQL_ADMIN_USER', 'root')
ADMIN_ENV = dict(os.environ, MYSQL_PWD=os.environ.get('MYSQL_ADMIN_PASSWORD', ''))
HOST = os.environ.get('DB_HOST', 'localhost')


def mysql(args, stdin=None):
    return subprocess.run(['mysql', '-u', ADMIN_USER, '-h', HOST] + args, input=stdin, text=True, env=ADMIN_ENV,
                          capture_output=True)


def scalar(db, sql):
    r = mysql(['-N', '-B', db, '-e', sql])
    return r.stdout.strip() if r.returncode == 0 else None


def current_version(db):
    if scalar(db, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'Ticket'") != '1':
        sys.exit(f'Database {db} has no RouteWise tables. Import schema.sql first.')
    has_table = scalar(db, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'SchemaVersion'")
    if has_table == '0':
        return 1                                   # created before versioning existed
    return int(scalar(db, 'SELECT COALESCE(MAX(Version), 1) FROM SchemaVersion') or 1)


def migrations():
    found = []
    for path in sorted(glob.glob(os.path.join(ROOT, 'migrations', '[0-9][0-9][0-9][0-9]_*.sql'))):
        found.append((int(re.match(r'(\d{4})_', os.path.basename(path)).group(1)), path))
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default=os.environ.get('DB_NAME', 'public_transport'))
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--no-backup', action='store_true')
    args = ap.parse_args()
    db = args.db

    now = current_version(db)
    latest = max([v for v, _ in migrations()] + [1])
    pending = [(v, p) for v, p in migrations() if v > now]
    print(f'Database {db}: version {now}; latest available version {latest}.')
    if args.status or not pending:
        print('Up to date.' if not pending else f'{len(pending)} migration(s) pending.')
        return

    if not args.no_backup:
        backup = os.path.join(ROOT, 'backups', f'{db}_before_v{latest}.sql')
        r = subprocess.run([os.path.join(ROOT, 'scripts', 'backup.sh'), backup], env=dict(ADMIN_ENV, DB_NAME=db), capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit('Backup failed, nothing was changed:\n' + r.stderr)
        print(' ', r.stdout.strip())

    for version, path in pending:
        print(f'Applying {os.path.basename(path)} ...')
        sql = open(path).read().replace('public_transport', db)
        r = mysql([db], stdin=sql)
        if r.returncode != 0:
            sys.exit(f'Migration {version} failed:\n{r.stderr}\nRestore the backup with scripts/restore.sh if the database is now inconsistent.')
    retire_old_app_user(db)
    print(f'Done. {db} is now at version {current_version(db)}.')


def retire_old_app_user(db):
    """Version 3 renamed the application's database user. Drop the old account, but only when no other existing
    database still relies on it (MySQL accounts are server-wide)."""
    old = 'transitiq_app'
    if scalar(db, f"SELECT COUNT(*) FROM mysql.user WHERE user = '{old}'") != '1':
        return
    others = scalar(db, f"SELECT COUNT(*) FROM (SELECT db FROM mysql.db WHERE user = '{old}' UNION SELECT db FROM mysql.tables_priv "
                        f"WHERE user = '{old}' UNION SELECT db FROM mysql.procs_priv WHERE user = '{old}') g "
                        f"JOIN information_schema.schemata s ON s.schema_name = g.db WHERE g.db <> '{db}'")
    if others == '0':
        mysql([db, '-e', f"DROP USER IF EXISTS '{old}'@'localhost'"])
        print(f'  Removed the old database account {old} (the application now uses routewise_app).')
    else:
        print(f'  Kept the old database account {old}: {others} other database(s) still use it.')


if __name__ == '__main__':
    main()
