"""Developer tool: writes migrations/0005_days_swaps_payments.sql.

The structural changes (service days into rows, a sale for every existing ticket, re-partitioning the audit table on a
DATETIME) are written by hand below. The new tables, the partition layout,
the programmable objects (functions, views, procedures, triggers, events) and the grants are copied verbatim from
schema.sql, so the upgrade path and a fresh install cannot drift apart. A test (tests/test_app.py, MigrationTests)
proves it by comparing an upgraded database with a fresh one.
Released migrations never change: 0002 to 0004 were generated the same way from the version-2 to version-4 schemas.

    python migrations/make_0005.py
"""
import os
import sys

sys.exit("0005 is released and frozen: it was generated from the version-5 schema. New changes go into a new migration.")

HERE = os.path.dirname(os.path.abspath(__file__))
schema = open(os.path.join(HERE, '..', 'schema.sql')).read()


def between(start, end, inclusive_end=True):
    i = schema.index(start)
    j = schema.index(end, i) + (len(end) if inclusive_end else 0)
    return schema[i:j]


tables = between('-- [v5 tables]', '-- [end v5 tables]')
partitions = between('PARTITION BY RANGE COLUMNS (ActionTime) (', 'PARTITION pmax    VALUES LESS THAN (MAXVALUE)\n);')
indexes = between('DROP PROCEDURE IF EXISTS _add_index;', 'DROP PROCEDURE _add_index;')
SECTION_8 = '-- ---------------------------------------------------------------------\n--  8.'
objects = between('DROP FUNCTION IF EXISTS TripVehicle;', SECTION_8, inclusive_end=False)
grants = between("CREATE USER IF NOT EXISTS 'routewise_app'", "GRANT EXECUTE ON public_transport.* TO 'routewise_app'@'localhost';")
version = between('INSERT IGNORE INTO SchemaVersion', ';')
days = "'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'"

sql = f"""-- =====================================================================
--  Migration 0005: service days as rows (ScheduleDay), one-day vehicle swaps, a payment ledger, and the audit trail
--  re-partitioned on a DATETIME column (RANGE COLUMNS: no 2038 limit, no time-zone dependence)
--  Upgrades a version-4 database in place. Every existing ticket gets one cash sale in the ledger, so the ledger
--  balances against the fares from the start.
--  Run it with:  python scripts/migrate.py      (it takes a backup first)
--  The structural part is hand-written; everything else below is generated from schema.sql by make_0005.py.
-- =====================================================================

SET NAMES utf8mb4;

{tables}

-- service days: the SET column becomes one row per day
INSERT IGNORE INTO ScheduleDay (ScheduleID, Weekday)
SELECT S.ScheduleID, d.n
FROM Schedule S JOIN (SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
                      UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7) AS d
  ON FIND_IN_SET(ELT(d.n, {days}), S.ServiceDays) > 0;
ALTER TABLE Schedule DROP CHECK chk_service_days;
ALTER TABLE Schedule DROP COLUMN ServiceDays;

-- the ledger starts with one sale per existing ticket
INSERT INTO Payment (TicketID, PassengerID, Kind, Method, Reference, Amount, PaidAt, Actor)
SELECT TicketID, PassengerID, 'Sale', 'Cash', 'recorded at upgrade', Fare, BookingDate, 'migration' FROM Ticket;

-- the audit trail: DATETIME instead of TIMESTAMP, partitioned by RANGE COLUMNS, two more indexes
ALTER TABLE TicketAudit REMOVE PARTITIONING;
ALTER TABLE TicketAudit MODIFY ActionTime DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        ADD KEY idx_audit_ticket (TicketID),
                        ADD KEY idx_audit_trip (ScheduleID, TravelDate);
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
out = os.path.join(HERE, '0005_days_swaps_payments.sql')
open(out, 'w').write(sql)
print('wrote', out, f'({len(sql.splitlines())} lines)')
