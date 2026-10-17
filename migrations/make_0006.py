"""Developer tool: writes migrations/0006_update_rules.sql.

Version 6 changes no table. It adds the rules for UPDATE (and append-only ledgers), so it consists of the programmable
objects and the grants, copied verbatim from schema.sql, so the upgrade path and a fresh install cannot drift apart.
A test (tests/test_app.py, MigrationTests) proves it by comparing an upgraded database with a fresh one, privileges included.
Released migrations never change: 0002 to 0005 were generated the same way from the version-2 to version-5 schemas.

    python migrations/make_0006.py
"""
import os
import sys

sys.exit("0006 is released and frozen: it was generated from the version-6 schema. New changes go into a new migration.")

HERE = os.path.dirname(os.path.abspath(__file__))
schema = open(os.path.join(HERE, '..', 'schema.sql')).read()


def between(start, end, inclusive_end=True):
    i = schema.index(start)
    j = schema.index(end, i) + (len(end) if inclusive_end else 0)
    return schema[i:j]


SECTION_8 = '-- ---------------------------------------------------------------------\n--  8.'
objects = between('DROP FUNCTION IF EXISTS TripVehicle;', SECTION_8, inclusive_end=False)
grants = between("CREATE USER IF NOT EXISTS 'routewise_app'", "GRANT EXECUTE ON public_transport.* TO 'routewise_app'@'localhost';")
version = between('INSERT IGNORE INTO SchemaVersion', ';')

sql = f"""-- =====================================================================
--  Migration 0006: rules for changing rows. A sold ticket can no longer be edited in place (cancel or transfer it);
--  a departure or vehicle that changes is checked like a new one; seat labels, service days and swaps cannot be
--  rewritten; the payment ledger and the audit trail become append-only for every account. The application user
--  loses the UPDATE and DELETE rights on Ticket and UPDATE on Schedule, which it never needed.
--  Upgrades a version-5 database in place; no table and no data change.
--  Run it with:  python scripts/migrate.py      (it takes a backup first)
--  Generated from schema.sql by make_0006.py.
-- =====================================================================

SET NAMES utf8mb4;

-- the programmable objects, copied from schema.sql
{objects.rstrip()}

-- database user privileges
{grants}

-- record the new version
{version}
"""
out = os.path.join(HERE, '0006_update_rules.sql')
open(out, 'w').write(sql)
print('wrote', out, f'({len(sql.splitlines())} lines)')
