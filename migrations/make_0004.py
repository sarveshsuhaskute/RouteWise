"""Developer tool: writes migrations/0004_seat_holds_partitioned_audit.sql.

The structural changes (partitioning the audit table) are written by hand below. The new table, the partition layout,
the programmable objects (functions, views, procedures, triggers, events) and the grants are copied verbatim from
schema.sql, so the upgrade path and a fresh install cannot drift apart. A test (tests/test_app.py, MigrationTests)
proves it by comparing an upgraded database with a fresh one.
Released migrations never change: 0002 and 0003 were generated the same way from the version-2 and version-3 schemas.

    python migrations/make_0004.py
"""
import os
import sys

sys.exit("0004 is released and frozen: it was generated from the version-4 schema. New changes go into a new migration.")
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
schema = open(os.path.join(HERE, '..', 'schema.sql')).read()


def between(start, end, inclusive_end=True):
    i = schema.index(start)
    j = schema.index(end, i) + (len(end) if inclusive_end else 0)
    return schema[i:j]


tables = between('-- [v4 tables]', '-- [end v4 tables]')
partitions = between('PARTITION BY RANGE (UNIX_TIMESTAMP(ActionTime)) (', 'PARTITION pmax    VALUES LESS THAN MAXVALUE\n);')
indexes = between('DROP PROCEDURE IF EXISTS _add_index;', 'DROP PROCEDURE _add_index;')
objects = between('DROP FUNCTION IF EXISTS SeatsLeft;', '-- ---------------------------------------------------------------------\n--  8.', inclusive_end=False)
grants = between("CREATE USER IF NOT EXISTS 'routewise_app'", "GRANT EXECUTE ON public_transport.* TO 'routewise_app'@'localhost';")
version = between('INSERT IGNORE INTO SchemaVersion', ';')

sql = f"""-- =====================================================================
--  Migration 0004: seat holds that expire, a partitioned audit trail with automatic partition maintenance,
--  the departure board, and invoker-rights maintenance routines
--  Upgrades a version-3 database in place; no existing row changes.
--  Run it with:  python scripts/migrate.py      (it takes a backup first)
--  The structural part is hand-written; everything else below is generated from schema.sql by make_0004.py.
-- =====================================================================

SET NAMES utf8mb4;

{tables}

-- the audit trail becomes a partitioned table: the partitioning column must belong to the primary key
UPDATE TicketAudit SET ActionTime = CURRENT_TIMESTAMP WHERE ActionTime IS NULL;
ALTER TABLE TicketAudit MODIFY ActionTime TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        DROP PRIMARY KEY, ADD PRIMARY KEY (AuditID, ActionTime);
ALTER TABLE TicketAudit
{partitions}

-- indexes (unchanged; re-run safe)
{indexes}

-- the programmable objects, copied from schema.sql
{objects.rstrip()}

-- database user privileges
{grants}

-- record the new version
{version}
"""
out = os.path.join(HERE, '0004_seat_holds_partitioned_audit.sql')
open(out, 'w').write(sql)
print('wrote', out, f'({len(sql.splitlines())} lines)')
