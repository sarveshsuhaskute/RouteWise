"""Developer tool: writes migrations/0007_procedure_only_tickets.sql.

Version 7 changes no table. It adds the route-type, holiday and vehicle-retirement rules and takes away the
application's last direct write on tickets, so it consists of the programmable objects and the grants, copied verbatim
from schema.sql, so the upgrade path and a fresh install cannot drift apart.
A test (tests/test_app.py, MigrationTests) proves it by comparing an upgraded database with a fresh one, privileges included.
Released migrations never change: 0002 to 0006 were generated the same way from the version-2 to version-6 schemas.

    python migrations/make_0007.py
"""
import os

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
--  Migration 0007: tickets are written only by the stored procedures (the application user keeps SELECT on Ticket,
--  so every ticket gets a travel date from today to 30 days ahead and a booking time set by MySQL); a route with
--  departures keeps its type; a public holiday cannot strand sold tickets, nor can switching off RunsOnHolidays; a
--  vehicle leaves service only when no passenger is booked on a trip it would still run.
--  Upgrades a version-6 database in place; no table and no data change.
--  Run it with:  python scripts/migrate.py      (it takes a backup first)
--  Generated from schema.sql by make_0007.py.
-- =====================================================================

SET NAMES utf8mb4;

-- the programmable objects, copied from schema.sql
{objects.rstrip()}

-- database user privileges
{grants}

-- record the new version
{version}
"""
out = os.path.join(HERE, '0007_procedure_only_tickets.sql')
open(out, 'w').write(sql)
print('wrote', out, f'({len(sql.splitlines())} lines)')
