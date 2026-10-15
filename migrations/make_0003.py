"""Developer tool: writes migrations/0003_seat_map_service_days_search.sql.

The structural changes (ALTER TABLE ..., converting existing seat labels) are written by hand below. The new tables,
indexes, programmable objects (functions, views, procedures, triggers, the event) and the grants are copied verbatim
from schema.sql, so the upgrade path and a fresh install cannot drift apart. A test (tests/test_app.py, MigrationTests)
proves it by comparing an upgraded database with a fresh one.
Released migrations never change: 0002 was generated the same way from the version-2 schema.

    python migrations/make_0003.py
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
schema = open(os.path.join(HERE, '..', 'schema.sql')).read()


def between(start, end, inclusive_end=True):
    i = schema.index(start)
    j = schema.index(end, i) + (len(end) if inclusive_end else 0)
    return schema[i:j]


tables = between('-- [v3 tables]', '-- [end v3 tables]')
indexes = between('DROP PROCEDURE IF EXISTS _add_index;', 'DROP PROCEDURE _add_index;')
objects = between('DROP FUNCTION IF EXISTS SeatsLeft;', '-- ---------------------------------------------------------------------\n--  8.', inclusive_end=False)
grants = between("CREATE USER IF NOT EXISTS 'routewise_app'", "GRANT EXECUTE ON public_transport.* TO 'routewise_app'@'localhost';")
holidays = between('INSERT IGNORE INTO Holiday', ";")
version = between('INSERT IGNORE INTO SchemaVersion', ';')

sql = f"""-- =====================================================================
--  Migration 0003: seat map, service days and public holidays, FULLTEXT search, stored sign-in limit,
--  scheduled clean-up event, application user renamed to routewise_app
--  Upgrades a version-2 database in place. Every existing ticket keeps its passenger, trip and fare; its seat is
--  relabelled to a seat of the new seat map (the k-th ticket of a trip gets the k-th seat).
--  Run it with:  python scripts/migrate.py      (it takes a backup first)
--  The structural part is hand-written; everything else below is generated from schema.sql by make_0003.py.
-- =====================================================================

SET NAMES utf8mb4;

{tables}

-- vehicle layout: seats per row (metro cars are six across)
ALTER TABLE Vehicle ADD COLUMN SeatsPerRow INT NOT NULL DEFAULT 4 AFTER Capacity;
UPDATE Vehicle SET SeatsPerRow = 6 WHERE Type = 'Metro';
ALTER TABLE Vehicle DROP CHECK chk_capacity;
ALTER TABLE Vehicle ADD CONSTRAINT chk_capacity CHECK (Capacity BETWEEN 1 AND 600),
                    ADD CONSTRAINT chk_seats_per_row CHECK (SeatsPerRow BETWEEN 2 AND 6);

-- service calendar
ALTER TABLE Schedule ADD COLUMN ServiceDays SET('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun') NOT NULL DEFAULT 'Mon,Tue,Wed,Thu,Fri,Sat,Sun' AFTER Frequency,
                     ADD COLUMN RunsOnHolidays BOOLEAN NOT NULL DEFAULT TRUE AFTER ServiceDays,
                     ADD CONSTRAINT chk_service_days CHECK (ServiceDays <> '');
{holidays}

-- the seat map of every existing vehicle (the same rule as the GenerateSeats procedure)
INSERT INTO Seat (VehicleID, SeatNo, Position)
WITH RECURSIVE seq (i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM seq WHERE i + 1 < 600)
SELECT V.VehicleID,
       CONCAT(seq.i DIV V.SeatsPerRow + 1, SUBSTRING('ABCDEF', seq.i MOD V.SeatsPerRow + 1, 1)),
       CASE WHEN seq.i MOD V.SeatsPerRow + 1 IN (1, V.SeatsPerRow) THEN 'Window'
            WHEN seq.i MOD V.SeatsPerRow + 1 IN (CEIL(V.SeatsPerRow / 2), CEIL(V.SeatsPerRow / 2) + 1) THEN 'Aisle'
            ELSE 'Middle' END
FROM Vehicle V JOIN seq ON seq.i < V.Capacity;

-- existing tickets get seats of the seat map: first a unique temporary label, then the k-th seat of the trip
ALTER TABLE Ticket DROP CHECK chk_seat;
UPDATE Ticket SET SeatNo = CONCAT('~', TicketID);
UPDATE Ticket T
JOIN (SELECT TicketID, ROW_NUMBER() OVER (PARTITION BY ScheduleID, TravelDate ORDER BY TicketID) - 1 AS k FROM Ticket) R
  ON R.TicketID = T.TicketID
JOIN Schedule S ON S.ScheduleID = T.ScheduleID
JOIN Vehicle  V ON V.VehicleID  = S.VehicleID
SET T.SeatNo = CONCAT(R.k DIV V.SeatsPerRow + 1, SUBSTRING('ABCDEF', R.k MOD V.SeatsPerRow + 1, 1));
ALTER TABLE Ticket ADD CONSTRAINT chk_seat   CHECK (SeatNo REGEXP '^[1-9][0-9]{{0,2}}[A-F]$');

-- indexes (including the new FULLTEXT indexes)
{indexes}

-- the programmable objects, copied from schema.sql
{objects.rstrip()}

-- database user privileges: the application user is now routewise_app
-- (scripts/migrate.py removes the old routewise_app account once no other database uses it)
{grants}

-- record the new version
{version}
"""
out = os.path.join(HERE, '0003_seat_map_service_days_search.sql')
open(out, 'w').write(sql)
print('wrote', out, f'({len(sql.splitlines())} lines)')
