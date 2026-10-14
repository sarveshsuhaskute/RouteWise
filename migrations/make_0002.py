"""Developer tool: writes migrations/0002_travel_dates_fares_audit.sql.

The structural changes (ALTER TABLE ...) are written by hand below. Everything programmable (indexes, views, functions,
procedures, triggers, grants) is copied verbatim from schema.sql, so the upgrade path and a fresh install cannot drift apart.
A test (tests/test_app.py, MigrationTests) proves it by comparing the two databases.

    python migrations/make_0002.py
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
schema = open(os.path.join(HERE, '..', 'schema.sql')).read()


def between(start, end, inclusive_end=True):
    i = schema.index(start)
    j = schema.index(end, i) + (len(end) if inclusive_end else 0)
    return schema[i:j]


indexes = between('DROP PROCEDURE IF EXISTS _add_index;', 'DROP PROCEDURE _add_index;')
objects = between('CREATE OR REPLACE VIEW BookingSummary AS', '-- ---------------------------------------------------------------------\n--  5g.', inclusive_end=False)
grants = between("CREATE USER IF NOT EXISTS 'transitiq_app'", "GRANT EXECUTE ON public_transport.* TO 'transitiq_app'@'localhost';")

head = """-- =====================================================================
--  Migration 0002: travel dates, data-driven fares, lookup keys, type check, audit actor, schema versioning
--  Upgrades a version-1 database in place. Existing tickets get TravelDate = the day they were booked.
--  Run it with:  python scripts/migrate.py      (it takes a backup first)
--  The structural part is hand-written; the programmable objects below are generated from schema.sql.
-- =====================================================================

SET NAMES utf8mb4;

-- reference data and schema versioning
CREATE TABLE IF NOT EXISTS FareRule (
    RouteType VARCHAR(20)   PRIMARY KEY,
    RatePerKm DECIMAL(5,2)  NOT NULL,
    MinFare   DECIMAL(6,2)  NOT NULL,
    CONSTRAINT chk_rate    CHECK (RatePerKm > 0),
    CONSTRAINT chk_minfare CHECK (MinFare > 0)
);
CREATE TABLE IF NOT EXISTS CardDiscount (
    CardType    VARCHAR(20) PRIMARY KEY,
    DiscountPct INT NOT NULL,
    CONSTRAINT chk_discount CHECK (DiscountPct BETWEEN 0 AND 90)
);
CREATE TABLE IF NOT EXISTS SchemaVersion (
    Version   INT PRIMARY KEY,
    Name      VARCHAR(120) NOT NULL,
    AppliedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
INSERT IGNORE INTO FareRule (RouteType, RatePerKm, MinFare) VALUES ('Bus', 1.50, 10.00), ('Metro', 1.00, 10.00);
INSERT IGNORE INTO CardDiscount (CardType, DiscountPct) VALUES ('None', 0), ('Daily', 10), ('Weekly', 20), ('Monthly', 30);

-- the old CHECK lists become foreign keys to the reference tables (one source of truth)
ALTER TABLE Passenger DROP CHECK chk_card;
ALTER TABLE Passenger ADD CONSTRAINT fk_passenger_card FOREIGN KEY (CardType) REFERENCES CardDiscount(CardType);
ALTER TABLE Route     ADD CONSTRAINT fk_route_type     FOREIGN KEY (Type)     REFERENCES FareRule(RouteType);
ALTER TABLE Vehicle   ADD CONSTRAINT fk_vehicle_type   FOREIGN KEY (Type)     REFERENCES FareRule(RouteType);

-- tickets belong to a trip: a schedule on a travel date
ALTER TABLE Ticket ADD COLUMN TravelDate DATE NULL AFTER ScheduleID;
UPDATE Ticket SET TravelDate = DATE(BookingDate);
ALTER TABLE Ticket MODIFY COLUMN TravelDate DATE NOT NULL;
ALTER TABLE Ticket ADD UNIQUE KEY uq_trip_seat (ScheduleID, TravelDate, SeatNo);
ALTER TABLE Ticket DROP INDEX uq_schedule_seat;
ALTER TABLE Ticket ADD CONSTRAINT chk_seat   CHECK (SeatNo REGEXP '^[A-Za-z0-9]{1,6}$');
ALTER TABLE Ticket ADD CONSTRAINT chk_travel CHECK (TravelDate >= DATE(BookingDate) AND TravelDate <= DATE(BookingDate) + INTERVAL 30 DAY);

-- the audit trail records the travel date and who acted
ALTER TABLE TicketAudit ADD COLUMN TravelDate DATE NULL AFTER ScheduleID, ADD COLUMN Actor VARCHAR(50) NULL;
UPDATE TicketAudit A JOIN Ticket T ON T.TicketID = A.TicketID SET A.TravelDate = T.TravelDate;

-- indexes
"""

tail = """
-- the programmable objects, copied from schema.sql
{objects}
-- database user privileges
{grants}

-- record the new version
INSERT IGNORE INTO SchemaVersion (Version, Name) VALUES
(1, 'baseline: bookings, audit trail, roles, least-privilege user'),
(2, 'travel dates, fare rules, type check, audit actor, schema versioning');
""".format(objects=objects.rstrip() + '\n', grants=grants)

out = os.path.join(HERE, '0002_travel_dates_fares_audit.sql')
open(out, 'w').write(head + indexes + '\n' + tail)
print('wrote', out, f'({len((head + indexes + tail).splitlines())} lines)')
