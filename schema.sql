-- =====================================================================
--  RouteWise - Public Transport Management System  -  MySQL 8+ schema (version 5)
--  Import in MySQL Workbench, or:  mysql -u root -p < schema.sql
--  The script is re-runnable: tables use IF NOT EXISTS, programmable
--  objects are dropped and recreated, seed data is only added once.
--  Upgrading an existing version-1 to version-4 database without losing data: scripts/migrate.py
--
--  Functional dependencies (every relation is in BCNF: each determinant is a candidate key)
--    FareRule   : RouteType -> all     CardDiscount: CardType -> all
--    Passenger  : PassengerID -> all;  Email -> all
--    Driver     : DriverID -> all;     LicenseNo -> all
--    Route      : RouteID -> all
--    Vehicle    : VehicleID -> all;    VehicleNo -> all
--    Seat       : (VehicleID, SeatNo) -> Position      (the position depends on the vehicle's layout)
--    Schedule   : ScheduleID -> all;   (VehicleID, DepartureTime) -> all
--    ScheduleDay: (ScheduleID, Weekday)   all-key relation, no non-trivial dependency
--    VehicleSwap: (ScheduleID, TravelDate) -> VehicleID, Reason
--    Payment    : PaymentID -> all
--    Holiday    : HolidayDate -> Name
--    Ticket     : TicketID -> all;     (ScheduleID, TravelDate, SeatNo) -> all
--    SeatHold   : (ScheduleID, TravelDate, SeatNo) -> HeldBy, ExpiresAt
--    Feedback   : FeedbackID -> all
--    TicketAudit: (AuditID, ActionTime) -> all        AppUser: UserID -> all;  Username -> all
--    LoginAttempt: (Username, Address) -> all            SchemaVersion: Version -> all
-- =====================================================================
-- every script declares its character set, so the result does not depend on the client that runs it
SET NAMES utf8mb4;
CREATE DATABASE IF NOT EXISTS public_transport;
USE public_transport;

-- ---------------------------------------------------------------------
--  1. TABLES
-- ---------------------------------------------------------------------
-- Reference data: fares and discounts live in tables, so a price change is a data change, not a code change.
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

CREATE TABLE IF NOT EXISTS Passenger (
    PassengerID INT AUTO_INCREMENT PRIMARY KEY,
    Name        VARCHAR(100) NOT NULL,
    Email       VARCHAR(100) NOT NULL UNIQUE,
    Phone       VARCHAR(15),
    DOB         DATE,
    CardType    VARCHAR(20) NOT NULL DEFAULT 'None',
    CONSTRAINT fk_passenger_card FOREIGN KEY (CardType) REFERENCES CardDiscount(CardType)
);

CREATE TABLE IF NOT EXISTS Driver (
    DriverID   INT AUTO_INCREMENT PRIMARY KEY,
    Name       VARCHAR(100) NOT NULL,
    LicenseNo  VARCHAR(50)  NOT NULL UNIQUE,
    ShiftHours INT NOT NULL DEFAULT 8,
    Phone      VARCHAR(15),
    CONSTRAINT chk_shift CHECK (ShiftHours BETWEEN 1 AND 12)
);

CREATE TABLE IF NOT EXISTS Route (
    RouteID     INT AUTO_INCREMENT PRIMARY KEY,
    RouteName   VARCHAR(100) NOT NULL,
    Origin      VARCHAR(100) NOT NULL,
    Destination VARCHAR(100) NOT NULL,
    Distance    DECIMAL(5,2),
    Type        VARCHAR(20) NOT NULL DEFAULT 'Bus',
    CONSTRAINT chk_distance CHECK (Distance IS NULL OR Distance > 0),
    CONSTRAINT fk_route_type FOREIGN KEY (Type) REFERENCES FareRule(RouteType)
);

CREATE TABLE IF NOT EXISTS Vehicle (
    VehicleID   INT AUTO_INCREMENT PRIMARY KEY,
    VehicleNo   VARCHAR(20) NOT NULL UNIQUE,
    Type        VARCHAR(20) NOT NULL,
    Capacity    INT NOT NULL,
    SeatsPerRow INT NOT NULL DEFAULT 4,
    Model       VARCHAR(50),
    Status      VARCHAR(20) NOT NULL DEFAULT 'Active',
    DriverID    INT NULL,
    CONSTRAINT chk_capacity CHECK (Capacity BETWEEN 1 AND 600),
    CONSTRAINT chk_seats_per_row CHECK (SeatsPerRow BETWEEN 2 AND 6),
    CONSTRAINT chk_vstatus  CHECK (Status IN ('Active', 'Maintenance', 'Retired')),
    FOREIGN KEY (DriverID) REFERENCES Driver(DriverID) ON DELETE SET NULL,
    CONSTRAINT fk_vehicle_type FOREIGN KEY (Type) REFERENCES FareRule(RouteType)
);

CREATE TABLE IF NOT EXISTS Schedule (
    ScheduleID     INT AUTO_INCREMENT PRIMARY KEY,
    RouteID        INT  NOT NULL,
    VehicleID      INT  NOT NULL,
    DepartureTime  TIME NOT NULL,
    ArrivalTime    TIME NOT NULL,
    Frequency      INT,
    RunsOnHolidays BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT chk_times CHECK (ArrivalTime > DepartureTime),
    FOREIGN KEY (RouteID)   REFERENCES Route(RouteID),
    FOREIGN KEY (VehicleID) REFERENCES Vehicle(VehicleID)
);

-- [v3 tables] (migrations/make_0003.py copies the definitions between these markers into the upgrade)
-- The seat map: one row per physical seat, generated from the vehicle's capacity and seats per row by the
-- CreateSeats / ResizeSeats triggers. Seats are labelled row + letter (1A, 1B, ... 12D); a ticket must name one of them.
CREATE TABLE IF NOT EXISTS Seat (
    VehicleID INT         NOT NULL,
    SeatNo    VARCHAR(10) NOT NULL,
    Position  VARCHAR(6)  NOT NULL,
    PRIMARY KEY (VehicleID, SeatNo),
    CONSTRAINT chk_position CHECK (Position IN ('Window', 'Middle', 'Aisle')),
    CONSTRAINT fk_seat_vehicle FOREIGN KEY (VehicleID) REFERENCES Vehicle(VehicleID) ON DELETE CASCADE
);

-- Public holidays: departures with RunsOnHolidays = FALSE do not run (and cannot be booked) on these dates.
CREATE TABLE IF NOT EXISTS Holiday (
    HolidayDate DATE PRIMARY KEY,
    Name        VARCHAR(100) NOT NULL
);

-- Failed sign-ins per username and address: the lock-out survives restarts and is shared by every server process.
CREATE TABLE IF NOT EXISTS LoginAttempt (
    Username     VARCHAR(50) NOT NULL,
    Address      VARCHAR(45) NOT NULL,
    Failures     INT         NOT NULL,
    FirstFailure DATETIME    NOT NULL,
    PRIMARY KEY (Username, Address),
    CONSTRAINT chk_failures CHECK (Failures > 0)
);
-- [end v3 tables]

-- A seat belongs to a TRIP: a schedule on a travel date. The same bus, time and seat can be sold again on another day.
CREATE TABLE IF NOT EXISTS Ticket (
    TicketID    INT AUTO_INCREMENT PRIMARY KEY,
    PassengerID INT NOT NULL,
    ScheduleID  INT NOT NULL,
    TravelDate  DATE NOT NULL,
    SeatNo      VARCHAR(10) NOT NULL,
    Fare        DECIMAL(6,2) NOT NULL,
    BookingDate TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_fare   CHECK (Fare > 0),
    CONSTRAINT chk_seat   CHECK (SeatNo REGEXP '^[1-9][0-9]{0,2}[A-F]$'),
    CONSTRAINT chk_travel CHECK (TravelDate >= DATE(BookingDate) AND TravelDate <= DATE(BookingDate) + INTERVAL 30 DAY),
    UNIQUE KEY uq_trip_seat (ScheduleID, TravelDate, SeatNo),   -- race-proof seat uniqueness per trip
    FOREIGN KEY (PassengerID) REFERENCES Passenger(PassengerID),
    FOREIGN KEY (ScheduleID)  REFERENCES Schedule(ScheduleID)
);

-- [v4 tables] (migrations/make_0004.py copies the definitions between these markers into the upgrade)
-- A seat a clerk has picked on the seat map but not booked yet. Other clerks cannot book it until the hold
-- expires (5 minutes); the PurgeSeatHolds event deletes expired holds every minute. Written only by HoldSeat and triggers.
CREATE TABLE IF NOT EXISTS SeatHold (
    ScheduleID INT         NOT NULL,
    TravelDate DATE        NOT NULL,
    SeatNo     VARCHAR(10) NOT NULL,
    HeldBy     VARCHAR(50) NOT NULL,
    ExpiresAt  DATETIME    NOT NULL,
    PRIMARY KEY (ScheduleID, TravelDate, SeatNo),
    KEY idx_hold_user (HeldBy),
    KEY idx_hold_expiry (ExpiresAt),
    CONSTRAINT fk_hold_schedule FOREIGN KEY (ScheduleID) REFERENCES Schedule(ScheduleID) ON DELETE CASCADE
);
-- [end v4 tables]

-- [v5 tables] (migrations/make_0005.py copies the definitions between these markers into the upgrade)
-- The days of the week a departure runs (1 = Sunday ... 7 = Saturday, as DAYOFWEEK numbers them). A new departure runs
-- every day (DefaultServiceDays trigger); AddSchedule narrows it. GuardServiceDay keeps at least one day and refuses to
-- remove a day for which tickets are sold.
CREATE TABLE IF NOT EXISTS ScheduleDay (
    ScheduleID INT     NOT NULL,
    Weekday    TINYINT NOT NULL,
    PRIMARY KEY (ScheduleID, Weekday),
    CONSTRAINT chk_weekday CHECK (Weekday BETWEEN 1 AND 7),
    CONSTRAINT fk_day_schedule FOREIGN KEY (ScheduleID) REFERENCES Schedule(ScheduleID) ON DELETE CASCADE
);

-- A different vehicle for one trip (a departure on one date), e.g. while the usual vehicle is in the workshop.
-- TripVehicle() returns the vehicle a trip actually uses; every seat, capacity and occupancy rule asks it.
CREATE TABLE IF NOT EXISTS VehicleSwap (
    ScheduleID INT          NOT NULL,
    TravelDate DATE         NOT NULL,
    VehicleID  INT          NOT NULL,
    Reason     VARCHAR(100) NOT NULL,
    PRIMARY KEY (ScheduleID, TravelDate),
    KEY idx_swap_vehicle (VehicleID, TravelDate),
    CONSTRAINT fk_swap_schedule FOREIGN KEY (ScheduleID) REFERENCES Schedule(ScheduleID) ON DELETE CASCADE,
    CONSTRAINT fk_swap_vehicle  FOREIGN KEY (VehicleID)  REFERENCES Vehicle(VehicleID)
);

-- The money ledger, written only by triggers: a Sale when a ticket is booked, a Refund when it is cancelled
-- (a transfer is a refund of the old fare plus a sale of the new one). So SUM(Amount) always equals the fares of the
-- tickets that exist. Like the audit trail it keeps no foreign key, because tickets are deleted when cancelled.
CREATE TABLE IF NOT EXISTS Payment (
    PaymentID   INT AUTO_INCREMENT PRIMARY KEY,
    TicketID    INT          NOT NULL,
    PassengerID INT          NOT NULL,
    Kind        VARCHAR(10)  NOT NULL,
    Method      VARCHAR(10)  NOT NULL,
    Reference   VARCHAR(40)  NULL,
    Amount      DECIMAL(7,2) NOT NULL,
    PaidAt      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    Actor       VARCHAR(50)  NULL,
    KEY idx_payment_ticket (TicketID),
    KEY idx_payment_time (PaidAt),
    CONSTRAINT chk_pay_kind   CHECK (Kind IN ('Sale', 'Refund')),
    CONSTRAINT chk_pay_method CHECK (Method IN ('Cash', 'UPI', 'Card')),
    CONSTRAINT chk_pay_sign   CHECK ((Kind = 'Sale' AND Amount > 0) OR (Kind = 'Refund' AND Amount < 0))
);
-- [end v5 tables]

CREATE TABLE IF NOT EXISTS Feedback (
    FeedbackID   INT AUTO_INCREMENT PRIMARY KEY,
    PassengerID  INT NOT NULL,
    RouteID      INT NULL,
    Rating       INT NOT NULL,
    Comments     TEXT,
    FeedbackDate TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_rating CHECK (Rating BETWEEN 1 AND 5),
    FOREIGN KEY (PassengerID) REFERENCES Passenger(PassengerID),
    FOREIGN KEY (RouteID)     REFERENCES Route(RouteID) ON DELETE SET NULL
);

-- Application logins (passwords are stored as salted hashes, never in plain text)
CREATE TABLE IF NOT EXISTS AppUser (
    UserID       INT AUTO_INCREMENT PRIMARY KEY,
    Username     VARCHAR(50)  NOT NULL UNIQUE,
    FullName     VARCHAR(100) NOT NULL,
    PasswordHash VARCHAR(255) NOT NULL,
    Role         VARCHAR(10)  NOT NULL DEFAULT 'viewer',
    CreatedAt    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_role CHECK (Role IN ('admin', 'staff', 'viewer'))
);

-- Written to by triggers only: a permanent history of bookings/cancellations and WHO did them
-- (Actor is the application user set by the application for the session, otherwise the database user).
-- The history only grows, so it is PARTITIONED by quarter: queries on a period read only its partitions
-- (partition pruning), and old quarters can be archived or dropped in one step. It has no foreign keys,
-- which is what makes partitioning possible in InnoDB. ExtendAuditPartitions adds future quarters.
CREATE TABLE IF NOT EXISTS TicketAudit (
    AuditID     INT AUTO_INCREMENT,
    TicketID    INT NOT NULL,
    Action      VARCHAR(20) NOT NULL,
    PassengerID INT NOT NULL,
    ScheduleID  INT NOT NULL,
    TravelDate  DATE NULL,
    SeatNo      VARCHAR(10) NOT NULL,
    Fare        DECIMAL(6,2) NOT NULL,
    ActionTime  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    Actor       VARCHAR(50) NULL,
    PRIMARY KEY (AuditID, ActionTime),           -- the partitioning column must be part of every unique key
    KEY idx_audit_ticket (TicketID),             -- the ticket check looks up one ticket's history
    KEY idx_audit_trip (ScheduleID, TravelDate)  -- questions about a trip do not depend on the partitioning
)
-- RANGE COLUMNS on a DATETIME: no conversion function, no dependence on the session time zone, and no 2038 limit
-- (a TIMESTAMP, and UNIX_TIMESTAMP(), end in January 2038).
PARTITION BY RANGE COLUMNS (ActionTime) (
    PARTITION p_before_2026 VALUES LESS THAN ('2026-01-01 00:00:00'),
    PARTITION p2026q1 VALUES LESS THAN ('2026-04-01 00:00:00'),
    PARTITION p2026q2 VALUES LESS THAN ('2026-07-01 00:00:00'),
    PARTITION p2026q3 VALUES LESS THAN ('2026-10-01 00:00:00'),
    PARTITION p2026q4 VALUES LESS THAN ('2027-01-01 00:00:00'),
    PARTITION p2027q1 VALUES LESS THAN ('2027-04-01 00:00:00'),
    PARTITION p2027q2 VALUES LESS THAN ('2027-07-01 00:00:00'),
    PARTITION p2027q3 VALUES LESS THAN ('2027-10-01 00:00:00'),
    PARTITION p2027q4 VALUES LESS THAN ('2028-01-01 00:00:00'),
    PARTITION pmax    VALUES LESS THAN (MAXVALUE)
);

-- Which version of this schema a database is at (managed by schema.sql and scripts/migrate.py).
CREATE TABLE IF NOT EXISTS SchemaVersion (
    Version   INT PRIMARY KEY,
    Name      VARCHAR(120) NOT NULL,
    AppliedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------
--  2. INDEXES (speed up the joins / lookups / searches used by the app)
-- ---------------------------------------------------------------------
-- (re-run safe: an index is only created when it does not exist yet; names starting ft_ are FULLTEXT indexes)
DROP PROCEDURE IF EXISTS _add_index;
DELIMITER $$
CREATE PROCEDURE _add_index(IN p_table VARCHAR(64), IN p_index VARCHAR(64), IN p_cols VARCHAR(128))
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.statistics
                   WHERE table_schema = DATABASE() AND table_name = p_table AND index_name = p_index) THEN
        SET @s = CONCAT('CREATE ', IF(LEFT(p_index, 3) = 'ft_', 'FULLTEXT ', ''), 'INDEX ', p_index, ' ON ', p_table, ' (', p_cols, ')');
        PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;
    END IF;
END$$
DELIMITER ;
CALL _add_index('Ticket',    'idx_ticket_passenger', 'PassengerID');
CALL _add_index('Ticket',    'idx_ticket_date',      'TravelDate');
CALL _add_index('Schedule',  'idx_schedule_route',   'RouteID');
CALL _add_index('Schedule',  'idx_schedule_vehicle', 'VehicleID');
CALL _add_index('Feedback',  'idx_feedback_route',   'RouteID');
CALL _add_index('Passenger', 'idx_passenger_phone',  'Phone');
-- word search: LIKE '%x%' cannot use an index and reads every row; a FULLTEXT index finds the words directly
CALL _add_index('Passenger', 'ft_passenger', 'Name, Email');
CALL _add_index('Route',     'ft_route',     'RouteName, Origin, Destination');
DROP PROCEDURE _add_index;

-- ---------------------------------------------------------------------
--  3. FUNCTIONS (defined before the views, which use RunsOn)
-- ---------------------------------------------------------------------
-- The vehicle a trip (departure + date) really uses: a one-day swap if there is one, otherwise the scheduled vehicle.
DROP FUNCTION IF EXISTS TripVehicle;
DELIMITER $$
CREATE FUNCTION TripVehicle(p_schedule INT, p_date DATE) RETURNS INT
READS SQL DATA
BEGIN
    RETURN COALESCE((SELECT VehicleID FROM VehicleSwap WHERE ScheduleID = p_schedule AND TravelDate = p_date),
                    (SELECT VehicleID FROM Schedule WHERE ScheduleID = p_schedule));
END$$
DELIMITER ;

DROP FUNCTION IF EXISTS SeatsLeft;
DELIMITER $$
CREATE FUNCTION SeatsLeft(p_schedule INT, p_date DATE) RETURNS INT
READS SQL DATA
BEGIN
    RETURN (SELECT Capacity FROM Vehicle WHERE VehicleID = TripVehicle(p_schedule, p_date))
         - (SELECT COUNT(*) FROM Ticket WHERE ScheduleID = p_schedule AND TravelDate = p_date);
END$$
DELIMITER ;

-- Fare = max(MinFare, distance x RatePerKm) minus the travel-card discount; the rates come from FareRule and CardDiscount.
DROP FUNCTION IF EXISTS CalcFare;
DELIMITER $$
CREATE FUNCTION CalcFare(p_passenger INT, p_schedule INT) RETURNS DECIMAL(6,2)
READS SQL DATA
BEGIN
    DECLARE v_dist DECIMAL(5,2);
    DECLARE v_type VARCHAR(20);
    DECLARE v_card VARCHAR(20);
    DECLARE v_rate DECIMAL(5,2);
    DECLARE v_min  DECIMAL(6,2);
    DECLARE v_pct  INT;
    DECLARE v_base DECIMAL(8,2);
    SELECT R.Distance, R.Type INTO v_dist, v_type
    FROM Schedule S JOIN Route R ON S.RouteID = R.RouteID WHERE S.ScheduleID = p_schedule;
    SELECT CardType INTO v_card FROM Passenger WHERE PassengerID = p_passenger;
    IF v_type IS NULL OR v_card IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT RatePerKm, MinFare INTO v_rate, v_min FROM FareRule WHERE RouteType = v_type;
    SELECT DiscountPct INTO v_pct FROM CardDiscount WHERE CardType = v_card;
    IF v_rate IS NULL OR v_pct IS NULL THEN
        RETURN NULL;
    END IF;
    SET v_base = GREATEST(v_min, ROUND(COALESCE(v_dist, 10) * v_rate, 2));
    RETURN ROUND(v_base * (100 - v_pct) / 100, 2);
END$$
DELIMITER ;

-- Does a departure run on a date? Its service days (ScheduleDay) must include the weekday, and on a public holiday
-- it runs only when RunsOnHolidays is set. NULL for an unknown schedule.
DROP FUNCTION IF EXISTS RunsOn;
DELIMITER $$
CREATE FUNCTION RunsOn(p_schedule INT, p_date DATE) RETURNS BOOLEAN
READS SQL DATA
BEGIN
    RETURN (SELECT EXISTS (SELECT 1 FROM ScheduleDay D WHERE D.ScheduleID = p_schedule AND D.Weekday = DAYOFWEEK(p_date))
                   AND (RunsOnHolidays OR NOT EXISTS (SELECT 1 FROM Holiday WHERE HolidayDate = p_date))
            FROM Schedule WHERE ScheduleID = p_schedule);
END$$
DELIMITER ;

-- ---------------------------------------------------------------------
--  4. VIEWS
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW BookingSummary AS
SELECT T.TicketID, P.Name AS PassengerName, R.RouteName, V.VehicleNo,
       S.DepartureTime, T.TravelDate, T.SeatNo, T.Fare, T.BookingDate
FROM Ticket T
JOIN Passenger P ON T.PassengerID = P.PassengerID
JOIN Schedule  S ON T.ScheduleID  = S.ScheduleID
JOIN Route     R ON S.RouteID     = R.RouteID
LEFT JOIN VehicleSwap VS ON VS.ScheduleID = T.ScheduleID AND VS.TravelDate = T.TravelDate
JOIN Vehicle   V ON V.VehicleID   = COALESCE(VS.VehicleID, S.VehicleID);       -- the vehicle the trip really uses

-- Occupancy of every departure TODAY (what a clerk needs when selling a ticket), and whether it runs today.
CREATE OR REPLACE VIEW ScheduleOccupancy AS
SELECT S.ScheduleID, R.RouteName, V.VehicleNo, V.Status AS VehicleStatus,
       S.DepartureTime,
       (SELECT GROUP_CONCAT(ELT(D.Weekday, 'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat') ORDER BY (D.Weekday + 5) MOD 7 SEPARATOR ',')
        FROM ScheduleDay D WHERE D.ScheduleID = S.ScheduleID)  AS ServiceDays,
       S.RunsOnHolidays, RunsOn(S.ScheduleID, CURDATE()) AS RunsToday,
       CURDATE() AS TravelDate, V.Capacity,
       COUNT(T.TicketID)                                   AS Booked,
       V.Capacity - COUNT(T.TicketID)                      AS SeatsLeft,
       ROUND(100 * COUNT(T.TicketID) / V.Capacity, 1)      AS OccupancyPct
FROM Schedule S
JOIN Route   R ON S.RouteID   = R.RouteID
LEFT JOIN VehicleSwap VS ON VS.ScheduleID = S.ScheduleID AND VS.TravelDate = CURDATE()
JOIN Vehicle V ON V.VehicleID = COALESCE(VS.VehicleID, S.VehicleID)
LEFT JOIN Ticket T ON T.ScheduleID = S.ScheduleID AND T.TravelDate = CURDATE()
GROUP BY S.ScheduleID, R.RouteName, V.VehicleNo, V.Status, S.DepartureTime, S.RunsOnHolidays, V.Capacity;

-- Occupancy of every TRIP (schedule on a date) that has at least one ticket, for any date.
CREATE OR REPLACE VIEW TripOccupancy AS
SELECT S.ScheduleID, R.RouteName, V.VehicleNo, S.DepartureTime, T.TravelDate, V.Capacity,
       COUNT(*)                                            AS Booked,
       V.Capacity - COUNT(*)                               AS SeatsLeft,
       ROUND(100 * COUNT(*) / V.Capacity, 1)               AS OccupancyPct
FROM Ticket T
JOIN Schedule S ON T.ScheduleID = S.ScheduleID
JOIN Route    R ON S.RouteID    = R.RouteID
LEFT JOIN VehicleSwap VS ON VS.ScheduleID = T.ScheduleID AND VS.TravelDate = T.TravelDate
JOIN Vehicle  V ON V.VehicleID  = COALESCE(VS.VehicleID, S.VehicleID)
GROUP BY S.ScheduleID, R.RouteName, V.VehicleNo, S.DepartureTime, T.TravelDate, V.Capacity;

CREATE OR REPLACE VIEW RouteRevenue AS
SELECT R.RouteID, R.RouteName, R.Type,
       COUNT(T.TicketID)         AS TicketsSold,
       COALESCE(SUM(T.Fare), 0)  AS Revenue
FROM Route R
LEFT JOIN Schedule S ON S.RouteID = R.RouteID
LEFT JOIN Ticket   T ON T.ScheduleID = S.ScheduleID
GROUP BY R.RouteID, R.RouteName, R.Type;

CREATE OR REPLACE VIEW RouteRatings AS
SELECT R.RouteID, R.RouteName,
       COUNT(F.FeedbackID)            AS Reviews,
       ROUND(AVG(F.Rating), 2)        AS AvgRating
FROM Route R
LEFT JOIN Feedback F ON F.RouteID = R.RouteID
GROUP BY R.RouteID, R.RouteName;

-- Updatable view: only active vehicles; WITH CHECK OPTION stops an update through the view
-- from moving a row out of the view (e.g. setting Status to 'Retired').
CREATE OR REPLACE VIEW ActiveVehicle AS
SELECT VehicleID, VehicleNo, Type, Capacity, Model, Status, DriverID
FROM Vehicle
WHERE Status = 'Active'
WITH CHECK OPTION;

-- Today's departure board: every departure that runs today, with a status worked out from the timetable and the clock.
CREATE OR REPLACE VIEW DepartureBoard AS
SELECT S.ScheduleID, R.RouteName, R.Origin, R.Destination, V.VehicleNo, S.DepartureTime, S.ArrivalTime,
       CASE WHEN V.Status <> 'Active'                              THEN 'Cancelled'
            WHEN CURTIME() < SUBTIME(S.DepartureTime, '00:10:00')  THEN 'Scheduled'
            WHEN CURTIME() < S.DepartureTime                       THEN 'Boarding'
            WHEN CURTIME() < S.ArrivalTime                         THEN 'En route'
            ELSE 'Arrived' END                                     AS Status,
       V.Capacity - COALESCE(TC.Booked, 0)                         AS SeatsLeft,
       VS.Reason                                                   AS SwapReason
FROM Schedule S
JOIN Route   R ON R.RouteID   = S.RouteID
LEFT JOIN VehicleSwap VS ON VS.ScheduleID = S.ScheduleID AND VS.TravelDate = CURDATE()
JOIN Vehicle V ON V.VehicleID = COALESCE(VS.VehicleID, S.VehicleID)
LEFT JOIN (SELECT ScheduleID, COUNT(*) AS Booked FROM Ticket WHERE TravelDate = CURDATE() GROUP BY ScheduleID) TC
       ON TC.ScheduleID = S.ScheduleID
-- the RunsOn() rule written out as joins, so the optimizer handles all departures at once instead of one call per row
WHERE EXISTS (SELECT 1 FROM ScheduleDay D WHERE D.ScheduleID = S.ScheduleID AND D.Weekday = DAYOFWEEK(CURDATE()))
  AND (S.RunsOnHolidays OR NOT EXISTS (SELECT 1 FROM Holiday H WHERE H.HolidayDate = CURDATE()));

-- ---------------------------------------------------------------------
--  5. PROCEDURES
-- ---------------------------------------------------------------------
-- Builds the seat map of a vehicle: p_capacity seats, p_per_row to a row, labelled 1A, 1B, ... (called by triggers)
-- The outer columns are window seats, the two middle columns aisle seats, the rest middle seats.
DROP PROCEDURE IF EXISTS GenerateSeats;
DELIMITER $$
CREATE PROCEDURE GenerateSeats(IN p_vehicle INT, IN p_capacity INT, IN p_per_row INT)
SQL SECURITY INVOKER         -- runs with the caller's rights: the CreateSeats/ResizeSeats triggers may, the app user may not
BEGIN
    INSERT INTO Seat (VehicleID, SeatNo, Position)
    WITH RECURSIVE seq (i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM seq WHERE i + 1 < p_capacity)
    SELECT p_vehicle,
           CONCAT(i DIV p_per_row + 1, SUBSTRING('ABCDEF', i MOD p_per_row + 1, 1)),
           CASE WHEN i MOD p_per_row + 1 IN (1, p_per_row) THEN 'Window'
                WHEN i MOD p_per_row + 1 IN (CEIL(p_per_row / 2), CEIL(p_per_row / 2) + 1) THEN 'Aisle'
                ELSE 'Middle' END
    FROM seq;
END$$
DELIMITER ;

DROP PROCEDURE IF EXISTS BookTicket;
DELIMITER $$
CREATE PROCEDURE BookTicket(IN p_p_id INT, IN p_s_id INT, IN p_seat VARCHAR(10), IN p_fare DECIMAL(6,2), IN p_travel DATE)
BEGIN
    DECLARE v_date DATE;
    SET v_date = COALESCE(p_travel, CURDATE());                      -- NULL travel date = today
    IF v_date < CURDATE() THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Travel date is in the past.';
    ELSEIF v_date > CURDATE() + INTERVAL 30 DAY THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Tickets can be booked at most 30 days ahead.';
    END IF;
    -- NULL fare = calculate it automatically
    INSERT INTO Ticket (PassengerID, ScheduleID, TravelDate, SeatNo, Fare)
    VALUES (p_p_id, p_s_id, v_date, p_seat, COALESCE(p_fare, CalcFare(p_p_id, p_s_id)));
END$$
DELIMITER ;

DROP PROCEDURE IF EXISTS CancelTicket;
DELIMITER $$
CREATE PROCEDURE CancelTicket(IN p_ticket INT)
BEGIN
    DELETE FROM Ticket WHERE TicketID = p_ticket;
    IF ROW_COUNT() = 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Ticket not found.';
    END IF;
END$$
DELIMITER ;

-- Moves a passenger to another departure/seat/date as ONE transaction (all-or-nothing).
-- The old ticket is deleted and the new one inserted; if any step fails (seat taken, vehicle full,
-- vehicle out of service, past date, bad schedule...) the handler rolls everything back, so the passenger
-- never ends up with no ticket or with two. A NULL new date keeps the ticket's travel date.
DROP PROCEDURE IF EXISTS TransferTicket;
DELIMITER $$
CREATE PROCEDURE TransferTicket(IN p_ticket INT, IN p_new_schedule INT, IN p_new_seat VARCHAR(10), IN p_new_date DATE)
BEGIN
    DECLARE v_passenger INT;
    DECLARE v_old_date DATE;
    DECLARE v_date DATE;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        ROLLBACK;
        RESIGNAL;
    END;

    START TRANSACTION;
    SELECT PassengerID, TravelDate INTO v_passenger, v_old_date FROM Ticket WHERE TicketID = p_ticket FOR UPDATE;
    IF v_passenger IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Ticket not found.';
    END IF;
    SET v_date = COALESCE(p_new_date, v_old_date);
    -- the new sale is paid the same way as the original one (the Payment triggers read @pay_method)
    SELECT Method, Reference INTO @pay_method, @pay_ref FROM Payment WHERE TicketID = p_ticket AND Kind = 'Sale' LIMIT 1;
    IF v_date < CURDATE() THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Travel date is in the past.';
    ELSEIF v_date > CURDATE() + INTERVAL 30 DAY THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Tickets can be booked at most 30 days ahead.';
    END IF;
    DELETE FROM Ticket WHERE TicketID = p_ticket;                                  -- frees the old seat first
    INSERT INTO Ticket (PassengerID, ScheduleID, TravelDate, SeatNo, Fare)         -- triggers validate the new seat
    VALUES (v_passenger, p_new_schedule, v_date, p_new_seat, CalcFare(v_passenger, p_new_schedule));
    COMMIT;
END$$
DELIMITER ;

-- Adds a departure and the days it runs (e.g. 'Mon,Tue,Wed,Thu,Fri') as ONE transaction. The DefaultServiceDays trigger
-- first gives the new departure every day; the days not listed are then removed. An empty list rolls everything back.
DROP PROCEDURE IF EXISTS AddSchedule;
DELIMITER $$
CREATE PROCEDURE AddSchedule(IN p_route INT, IN p_vehicle INT, IN p_departure TIME, IN p_arrival TIME, IN p_frequency INT,
                             IN p_days VARCHAR(30), IN p_holidays BOOLEAN)
BEGIN
    DECLARE v_id INT;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        ROLLBACK;
        RESIGNAL;
    END;
    START TRANSACTION;
    INSERT INTO Schedule (RouteID, VehicleID, DepartureTime, ArrivalTime, Frequency, RunsOnHolidays)
    VALUES (p_route, p_vehicle, p_departure, p_arrival, p_frequency, p_holidays);
    SET v_id = LAST_INSERT_ID();
    DELETE FROM ScheduleDay WHERE ScheduleID = v_id
        AND FIND_IN_SET(ELT(Weekday, 'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'), REPLACE(p_days, ' ', '')) = 0;
    COMMIT;
    SELECT v_id AS ScheduleID;
END$$
DELIMITER ;

-- Holds a seat for a clerk for p_minutes while the booking is completed. One hold per clerk: picking another
-- seat releases the previous one. Runs with its definer's rights, because the application may not write SeatHold itself.
DROP PROCEDURE IF EXISTS HoldSeat;
DELIMITER $$
CREATE PROCEDURE HoldSeat(IN p_schedule INT, IN p_date DATE, IN p_seat VARCHAR(10), IN p_user VARCHAR(50), IN p_minutes INT)
BEGIN
    DECLARE v_lock INT;
    -- the same lock as a booking takes, so a hold and a booking for one departure never interleave
    SELECT ScheduleID INTO v_lock FROM Schedule WHERE ScheduleID = p_schedule FOR UPDATE;
    IF v_lock IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Unknown departure.';
    ELSEIF p_minutes NOT BETWEEN 1 AND 30 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: A hold lasts 1 to 30 minutes.';
    ELSEIF RunsOn(p_schedule, p_date) = FALSE THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: This departure does not run on that date.';
    ELSEIF NOT EXISTS (SELECT 1 FROM Seat WHERE VehicleID = TripVehicle(p_schedule, p_date) AND SeatNo = p_seat) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: No such seat on this vehicle.';
    ELSEIF EXISTS (SELECT 1 FROM Ticket WHERE ScheduleID = p_schedule AND TravelDate = p_date AND SeatNo = p_seat) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Seat already booked.';
    END IF;
    DELETE FROM SeatHold WHERE ScheduleID = p_schedule AND TravelDate = p_date AND SeatNo = p_seat AND ExpiresAt <= NOW();
    IF EXISTS (SELECT 1 FROM SeatHold WHERE ScheduleID = p_schedule AND TravelDate = p_date AND SeatNo = p_seat
               AND HeldBy <> p_user) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Seat is held by another clerk.';
    END IF;
    DELETE FROM SeatHold WHERE HeldBy = p_user
        AND NOT (ScheduleID = p_schedule AND TravelDate = p_date AND SeatNo = p_seat);
    INSERT INTO SeatHold (ScheduleID, TravelDate, SeatNo, HeldBy, ExpiresAt)
    VALUES (p_schedule, p_date, p_seat, p_user, NOW() + INTERVAL p_minutes MINUTE)
    ON DUPLICATE KEY UPDATE ExpiresAt = VALUES(ExpiresAt);
END$$
DELIMITER ;

-- Partition maintenance: adds quarterly partitions to TicketAudit (by splitting pmax) until the table has named
-- partitions for at least p_months_ahead months. Called every month by the MaintainAuditPartitions event.
-- Runs with the CALLER's rights: the event may change the table, the application user may not.
DROP PROCEDURE IF EXISTS ExtendAuditPartitions;
DELIMITER $$
CREATE PROCEDURE ExtendAuditPartitions(IN p_months_ahead INT)
SQL SECURITY INVOKER
BEGIN
    DECLARE v_start DATETIME;
    extend: LOOP
        -- the upper bound of the last named partition, e.g. '2028-01-01 00:00:00' (RANGE COLUMNS bounds are quoted)
        SELECT MAX(CAST(TRIM(BOTH '''' FROM PARTITION_DESCRIPTION) AS DATETIME)) INTO v_start FROM information_schema.partitions
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'TicketAudit' AND PARTITION_DESCRIPTION <> 'MAXVALUE';
        IF v_start >= NOW() + INTERVAL p_months_ahead MONTH THEN
            LEAVE extend;
        END IF;
        SET @ddl = CONCAT('ALTER TABLE TicketAudit REORGANIZE PARTITION pmax INTO (PARTITION p', YEAR(v_start), 'q', QUARTER(v_start),
                          ' VALUES LESS THAN (''', v_start + INTERVAL 3 MONTH, '''), PARTITION pmax VALUES LESS THAN (MAXVALUE))');
        PREPARE st FROM @ddl; EXECUTE st; DEALLOCATE PREPARE st;
    END LOOP;
END$$
DELIMITER ;

-- Cursor example: row-by-row scoring of every route with a NOT FOUND handler, returned as a result set.
-- Score = revenue / 10 + average rating x 10;  grade A >= 60, B >= 50, otherwise C.
DROP PROCEDURE IF EXISTS RouteScorecard;
DELIMITER $$
CREATE PROCEDURE RouteScorecard()
BEGIN
    DECLARE v_done INT DEFAULT 0;
    DECLARE v_name VARCHAR(100);
    DECLARE v_tickets INT;
    DECLARE v_revenue DECIMAL(10,2);
    DECLARE v_rating DECIMAL(4,2);
    DECLARE v_score DECIMAL(6,1);
    DECLARE cur CURSOR FOR
        SELECT RR.RouteName, RR.TicketsSold, RR.Revenue, RT.AvgRating
        FROM RouteRevenue RR JOIN RouteRatings RT ON RT.RouteID = RR.RouteID;
    DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_done = 1;

    DROP TEMPORARY TABLE IF EXISTS tmp_scorecard;
    CREATE TEMPORARY TABLE tmp_scorecard (
        RouteName VARCHAR(100), Tickets INT, Revenue DECIMAL(10,2),
        AvgRating DECIMAL(4,2), Score DECIMAL(6,1), Grade CHAR(1));

    OPEN cur;
    scoring: LOOP
        FETCH cur INTO v_name, v_tickets, v_revenue, v_rating;
        IF v_done = 1 THEN LEAVE scoring; END IF;
        SET v_score = ROUND(v_revenue / 10 + COALESCE(v_rating, 0) * 10, 1);
        INSERT INTO tmp_scorecard
        VALUES (v_name, v_tickets, v_revenue, v_rating, v_score,
                CASE WHEN v_score >= 60 THEN 'A' WHEN v_score >= 50 THEN 'B' ELSE 'C' END);
    END LOOP;
    CLOSE cur;

    SELECT RouteName, Tickets, Revenue, AvgRating, Score, Grade FROM tmp_scorecard ORDER BY Score DESC;
    DROP TEMPORARY TABLE tmp_scorecard;
END$$
DELIMITER ;

-- ---------------------------------------------------------------------
--  6. TRIGGERS
-- ---------------------------------------------------------------------
-- Ticket checks run in this order: 6a -> 6b -> 6c -> 6d (FOLLOWS), then the CHECK constraints and keys.
-- 6a. Reject a seat that is already booked on the same trip (schedule + travel date) or held by another clerk
DROP TRIGGER IF EXISTS PreventDoubleSeat;
DELIMITER $$
CREATE TRIGGER PreventDoubleSeat BEFORE INSERT ON Ticket FOR EACH ROW
BEGIN
    DECLARE v_lock INT;
    -- Lock the departure first: simultaneous bookings for one schedule queue up here, one at a time,
    -- instead of deadlocking or overselling. The other Ticket triggers run after this one (FOLLOWS).
    SELECT ScheduleID INTO v_lock FROM Schedule WHERE ScheduleID = NEW.ScheduleID FOR UPDATE;
    IF EXISTS (SELECT 1 FROM Ticket WHERE ScheduleID = NEW.ScheduleID AND TravelDate = NEW.TravelDate AND SeatNo = NEW.SeatNo) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Seat already booked.';
    END IF;
    -- a seat another clerk is holding on the seat map is not for sale until the hold is used or expires
    IF EXISTS (SELECT 1 FROM SeatHold WHERE ScheduleID = NEW.ScheduleID AND TravelDate = NEW.TravelDate AND SeatNo = NEW.SeatNo
               AND ExpiresAt > NOW() AND HeldBy <> COALESCE(@actor, SUBSTRING_INDEX(USER(), '@', 1))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Seat is held by another clerk for a few minutes.';
    END IF;
END$$
DELIMITER ;

-- 6b. Reject a travel date on which the departure does not run (service days, public holidays)
DROP TRIGGER IF EXISTS CheckServiceDay;
DELIMITER $$
CREATE TRIGGER CheckServiceDay BEFORE INSERT ON Ticket FOR EACH ROW FOLLOWS PreventDoubleSeat
BEGIN
    IF RunsOn(NEW.ScheduleID, NEW.TravelDate) = FALSE THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: This departure does not run on that date.';
    END IF;
END$$
DELIMITER ;

-- 6c. Reject bookings on a full trip or on a vehicle that is not in service
DROP TRIGGER IF EXISTS CheckCapacity;
DELIMITER $$
CREATE TRIGGER CheckCapacity BEFORE INSERT ON Ticket FOR EACH ROW FOLLOWS CheckServiceDay
BEGIN
    DECLARE v_cap INT;
    DECLARE v_status VARCHAR(20);
    DECLARE v_sold INT;
    SELECT Capacity, Status INTO v_cap, v_status FROM Vehicle WHERE VehicleID = TripVehicle(NEW.ScheduleID, NEW.TravelDate);
    SELECT COUNT(*) INTO v_sold FROM Ticket WHERE ScheduleID = NEW.ScheduleID AND TravelDate = NEW.TravelDate;
    IF v_status <> 'Active' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Vehicle is not in service.';
    ELSEIF v_sold >= v_cap THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Vehicle is full.';
    END IF;
END$$
DELIMITER ;

-- 6d. The seat must exist in the seat map of the vehicle the trip uses (TripVehicle: a one-day swap counts)
DROP TRIGGER IF EXISTS ValidateSeat;
DELIMITER $$
CREATE TRIGGER ValidateSeat BEFORE INSERT ON Ticket FOR EACH ROW FOLLOWS CheckCapacity
BEGIN
    IF NOT EXISTS (SELECT 1 FROM Seat WHERE VehicleID = TripVehicle(NEW.ScheduleID, NEW.TravelDate) AND SeatNo = NEW.SeatNo) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: No such seat on this vehicle.';
    END IF;
END$$
DELIMITER ;

-- 6e. A vehicle cannot be scheduled for two overlapping runs
DROP TRIGGER IF EXISTS PreventVehicleOverlap;
DELIMITER $$
CREATE TRIGGER PreventVehicleOverlap BEFORE INSERT ON Schedule FOR EACH ROW
BEGIN
    IF EXISTS (SELECT 1 FROM Schedule WHERE VehicleID = NEW.VehicleID
               AND NEW.DepartureTime < ArrivalTime AND NEW.ArrivalTime > DepartureTime) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Vehicle already has a run at that time.';
    END IF;
END$$
DELIMITER ;

-- 6f. A vehicle can only run routes of its own type (no metro train on a bus route)
DROP TRIGGER IF EXISTS CheckVehicleRouteType;
DELIMITER $$
CREATE TRIGGER CheckVehicleRouteType BEFORE INSERT ON Schedule FOR EACH ROW
BEGIN
    DECLARE v_vehicle_type VARCHAR(20);
    DECLARE v_route_type VARCHAR(20);
    SELECT Type INTO v_vehicle_type FROM Vehicle WHERE VehicleID = NEW.VehicleID;
    SELECT Type INTO v_route_type   FROM Route   WHERE RouteID   = NEW.RouteID;
    IF v_vehicle_type IS NOT NULL AND v_route_type IS NOT NULL AND v_vehicle_type <> v_route_type THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Vehicle type does not match the route type.';
    END IF;
END$$
DELIMITER ;

-- 6g/6h. Audit trail for every booking and cancellation, including who did it
DROP TRIGGER IF EXISTS AuditBooking;
DELIMITER $$
CREATE TRIGGER AuditBooking AFTER INSERT ON Ticket FOR EACH ROW
BEGIN
    INSERT INTO TicketAudit (TicketID, Action, PassengerID, ScheduleID, TravelDate, SeatNo, Fare, Actor)
    VALUES (NEW.TicketID, 'BOOKED', NEW.PassengerID, NEW.ScheduleID, NEW.TravelDate, NEW.SeatNo, NEW.Fare,
            COALESCE(@actor, SUBSTRING_INDEX(USER(), '@', 1)));
END$$
DELIMITER ;

DROP TRIGGER IF EXISTS AuditCancellation;
DELIMITER $$
CREATE TRIGGER AuditCancellation AFTER DELETE ON Ticket FOR EACH ROW
BEGIN
    INSERT INTO TicketAudit (TicketID, Action, PassengerID, ScheduleID, TravelDate, SeatNo, Fare, Actor)
    VALUES (OLD.TicketID, 'CANCELLED', OLD.PassengerID, OLD.ScheduleID, OLD.TravelDate, OLD.SeatNo, OLD.Fare,
            COALESCE(@actor, SUBSTRING_INDEX(USER(), '@', 1)));
END$$
DELIMITER ;

-- 6k. A booked seat no longer needs its hold
DROP TRIGGER IF EXISTS ReleaseHold;
DELIMITER $$
CREATE TRIGGER ReleaseHold AFTER INSERT ON Ticket FOR EACH ROW FOLLOWS AuditBooking
BEGIN
    DELETE FROM SeatHold WHERE ScheduleID = NEW.ScheduleID AND TravelDate = NEW.TravelDate AND SeatNo = NEW.SeatNo;
END$$
DELIMITER ;

-- 6i/6j. The seat map follows the vehicle: created with it, rebuilt when its capacity or layout changes.
-- A change that would remove a seat booked for today or later is refused (the whole UPDATE is undone).
DROP TRIGGER IF EXISTS CreateSeats;
DELIMITER $$
CREATE TRIGGER CreateSeats AFTER INSERT ON Vehicle FOR EACH ROW
BEGIN
    CALL GenerateSeats(NEW.VehicleID, NEW.Capacity, NEW.SeatsPerRow);
END$$
DELIMITER ;

DROP TRIGGER IF EXISTS ResizeSeats;
DELIMITER $$
CREATE TRIGGER ResizeSeats AFTER UPDATE ON Vehicle FOR EACH ROW
BEGIN
    IF NEW.Capacity <> OLD.Capacity OR NEW.SeatsPerRow <> OLD.SeatsPerRow THEN
        DELETE FROM Seat WHERE VehicleID = NEW.VehicleID;
        CALL GenerateSeats(NEW.VehicleID, NEW.Capacity, NEW.SeatsPerRow);
        IF EXISTS (SELECT 1 FROM Ticket T
                   WHERE T.TravelDate >= CURDATE() AND TripVehicle(T.ScheduleID, T.TravelDate) = NEW.VehicleID
                     AND NOT EXISTS (SELECT 1 FROM Seat ST WHERE ST.VehicleID = NEW.VehicleID AND ST.SeatNo = T.SeatNo)) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Seats booked for upcoming trips would disappear with this capacity or layout.';
        END IF;
    END IF;
END$$
DELIMITER ;

-- 6l/6m. Money: every booking records a sale, every cancellation a refund of the sale (same method).
-- The application passes the payment method and reference in @pay_method / @pay_ref, as it passes @actor.
DROP TRIGGER IF EXISTS RecordSale;
DELIMITER $$
CREATE TRIGGER RecordSale AFTER INSERT ON Ticket FOR EACH ROW FOLLOWS ReleaseHold
BEGIN
    INSERT INTO Payment (TicketID, PassengerID, Kind, Method, Reference, Amount, Actor)
    VALUES (NEW.TicketID, NEW.PassengerID, 'Sale', COALESCE(@pay_method, 'Cash'), @pay_ref, NEW.Fare,
            COALESCE(@actor, SUBSTRING_INDEX(USER(), '@', 1)));
END$$
DELIMITER ;

DROP TRIGGER IF EXISTS RecordRefund;
DELIMITER $$
CREATE TRIGGER RecordRefund AFTER DELETE ON Ticket FOR EACH ROW FOLLOWS AuditCancellation
BEGIN
    DECLARE v_method VARCHAR(10);
    SELECT Method INTO v_method FROM Payment WHERE TicketID = OLD.TicketID AND Kind = 'Sale' LIMIT 1;   -- refund the way it was paid
    INSERT INTO Payment (TicketID, PassengerID, Kind, Method, Reference, Amount, Actor)
    VALUES (OLD.TicketID, OLD.PassengerID, 'Refund', COALESCE(v_method, 'Cash'),
            CONCAT('refund of ticket #', OLD.TicketID), -OLD.Fare, COALESCE(@actor, SUBSTRING_INDEX(USER(), '@', 1)));
END$$
DELIMITER ;

-- 6n/6o. Service days: a new departure runs every day until told otherwise; a departure keeps at least one day,
-- and a day cannot be removed while tickets are sold for it.
DROP TRIGGER IF EXISTS DefaultServiceDays;
DELIMITER $$
CREATE TRIGGER DefaultServiceDays AFTER INSERT ON Schedule FOR EACH ROW
BEGIN
    INSERT INTO ScheduleDay (ScheduleID, Weekday)
    SELECT NEW.ScheduleID, d.n FROM (SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
                                     UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7) AS d;
END$$
DELIMITER ;

DROP TRIGGER IF EXISTS GuardServiceDay;
DELIMITER $$
CREATE TRIGGER GuardServiceDay BEFORE DELETE ON ScheduleDay FOR EACH ROW
BEGIN
    IF (SELECT COUNT(*) FROM ScheduleDay WHERE ScheduleID = OLD.ScheduleID) <= 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: A departure must run on at least one day of the week.';
    ELSEIF EXISTS (SELECT 1 FROM Ticket WHERE ScheduleID = OLD.ScheduleID AND TravelDate >= CURDATE()
                   AND DAYOFWEEK(TravelDate) = OLD.Weekday) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Tickets are sold for that day of the week.';
    END IF;
END$$
DELIMITER ;

-- 6p/6q. One-day vehicle swaps: the replacement must be able to run that trip, and every seat already sold on it
-- must exist on the vehicle that will run it. Both rules hold when a swap is added and when it is removed.
DROP TRIGGER IF EXISTS CheckVehicleSwap;
DELIMITER $$
CREATE TRIGGER CheckVehicleSwap BEFORE INSERT ON VehicleSwap FOR EACH ROW
BEGIN
    DECLARE v_lock INT;
    DECLARE v_dep TIME;
    DECLARE v_arr TIME;
    DECLARE v_scheduled INT;
    DECLARE v_route_type VARCHAR(20);
    -- the same lock as a booking, so no seat is sold on the trip while its vehicle changes
    SELECT S.ScheduleID, S.DepartureTime, S.ArrivalTime, S.VehicleID, R.Type INTO v_lock, v_dep, v_arr, v_scheduled, v_route_type
    FROM Schedule S JOIN Route R ON R.RouteID = S.RouteID WHERE S.ScheduleID = NEW.ScheduleID FOR UPDATE;
    IF NEW.TravelDate < CURDATE() THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Travel date is in the past.';
    ELSEIF RunsOn(NEW.ScheduleID, NEW.TravelDate) = FALSE THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: This departure does not run on that date.';
    ELSEIF NEW.VehicleID = v_scheduled THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: That is already the scheduled vehicle.';
    ELSEIF NOT EXISTS (SELECT 1 FROM Vehicle WHERE VehicleID = NEW.VehicleID AND Status = 'Active' AND Type = v_route_type) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: The replacement must be an active vehicle of the route type.';
    ELSEIF EXISTS (SELECT 1 FROM Schedule S2
                   WHERE S2.ScheduleID <> NEW.ScheduleID AND RunsOn(S2.ScheduleID, NEW.TravelDate)
                     AND TripVehicle(S2.ScheduleID, NEW.TravelDate) = NEW.VehicleID
                     AND v_dep < S2.ArrivalTime AND v_arr > S2.DepartureTime) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: The replacement vehicle already has a run at that time.';
    ELSEIF EXISTS (SELECT 1 FROM Ticket T WHERE T.ScheduleID = NEW.ScheduleID AND T.TravelDate = NEW.TravelDate
                   AND NOT EXISTS (SELECT 1 FROM Seat ST WHERE ST.VehicleID = NEW.VehicleID AND ST.SeatNo = T.SeatNo)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Seats already sold on this trip do not exist on the replacement vehicle.';
    END IF;
END$$
DELIMITER ;

DROP TRIGGER IF EXISTS CheckSwapRemoval;
DELIMITER $$
CREATE TRIGGER CheckSwapRemoval BEFORE DELETE ON VehicleSwap FOR EACH ROW
BEGIN
    DECLARE v_lock INT;
    SELECT ScheduleID INTO v_lock FROM Schedule WHERE ScheduleID = OLD.ScheduleID FOR UPDATE;
    IF EXISTS (SELECT 1 FROM Ticket T JOIN Schedule S ON S.ScheduleID = T.ScheduleID
               WHERE T.ScheduleID = OLD.ScheduleID AND T.TravelDate = OLD.TravelDate
                 AND NOT EXISTS (SELECT 1 FROM Seat ST WHERE ST.VehicleID = S.VehicleID AND ST.SeatNo = T.SeatNo)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Seats sold on this trip do not exist on the scheduled vehicle.';
    END IF;
END$$
DELIMITER ;

-- ---------------------------------------------------------------------
--  7. SCHEDULED EVENTS (run inside MySQL; event_scheduler is ON by default in MySQL 8)
-- ---------------------------------------------------------------------
-- forget failed sign-ins after a day
DROP EVENT IF EXISTS PurgeLoginAttempts;
CREATE EVENT PurgeLoginAttempts ON SCHEDULE EVERY 1 HOUR
DO DELETE FROM LoginAttempt WHERE FirstFailure < NOW() - INTERVAL 1 DAY;

-- release seats whose hold has expired
DROP EVENT IF EXISTS PurgeSeatHolds;
CREATE EVENT PurgeSeatHolds ON SCHEDULE EVERY 1 MINUTE
DO DELETE FROM SeatHold WHERE ExpiresAt <= NOW();

-- keep at least six months of named partitions ahead in the audit table
DROP EVENT IF EXISTS MaintainAuditPartitions;
CREATE EVENT MaintainAuditPartitions ON SCHEDULE EVERY 1 MONTH
DO CALL ExtendAuditPartitions(6);

-- ---------------------------------------------------------------------
--  8. DATABASE USER FOR THE APPLICATION (least privilege)
--  The web application connects as 'routewise_app', never as root. It may read and change business data,
--  call the stored routines and read the views, but it cannot create or drop objects, it can only READ
--  the audit table, the payment ledger, the seat map and the seat holds (triggers and HoldSeat, which run with their definer's
--  rights, write those). GenerateSeats and ExtendAuditPartitions run with the caller's rights, so the app cannot use them.
--  Demo password: change it with scripts/set_app_password.sh before any real use.
-- ---------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'routewise_app'@'localhost' IDENTIFIED BY 'routewise_app_pw';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Passenger TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Driver    TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Route     TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Vehicle   TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Schedule  TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Ticket    TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Feedback  TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.AppUser   TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.LoginAttempt TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, DELETE ON public_transport.Holiday       TO 'routewise_app'@'localhost';
GRANT SELECT, UPDATE ON public_transport.FareRule      TO 'routewise_app'@'localhost';
GRANT SELECT, UPDATE ON public_transport.CardDiscount  TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.Seat              TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.SeatHold          TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.Payment           TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, DELETE ON public_transport.ScheduleDay TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, DELETE ON public_transport.VehicleSwap TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.SchemaVersion     TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.TicketAudit       TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.BookingSummary    TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.ScheduleOccupancy TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.TripOccupancy     TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.RouteRevenue      TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.RouteRatings      TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.ActiveVehicle     TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.DepartureBoard    TO 'routewise_app'@'localhost';
GRANT EXECUTE ON public_transport.* TO 'routewise_app'@'localhost';

-- ---------------------------------------------------------------------
--  9. SEED DATA
-- ---------------------------------------------------------------------
INSERT IGNORE INTO FareRule (RouteType, RatePerKm, MinFare) VALUES ('Bus', 1.50, 10.00), ('Metro', 1.00, 10.00);
INSERT IGNORE INTO CardDiscount (CardType, DiscountPct) VALUES ('None', 0), ('Daily', 10), ('Weekly', 20), ('Monthly', 30);

INSERT IGNORE INTO Route (RouteID, RouteName, Origin, Destination, Distance, Type) VALUES
(1, 'Route 201',   'Majestic',     'Electronic City',      25.50, 'Bus'),
(2, 'Route 500',   'Silk Board',   'Whitefield',           18.20, 'Bus'),
(3, 'Purple Line', 'Whitefield',   'Challaghatta',         43.50, 'Metro'),
(4, 'Green Line',  'Nagasandra',   'Silk Institute',       30.00, 'Metro'),
(5, 'Route 335E',  'Shivajinagar', 'Kempegowda Airport',   35.00, 'Bus');

INSERT IGNORE INTO Driver (DriverID, Name, LicenseNo, ShiftHours, Phone) VALUES
(1, 'Ravi Kumar',   'KA-01-202401', 8, '9876543210'),
(2, 'Suresh Gowda', 'KA-01-202402', 8, '9876543211'),
(3, 'Anita Rao',    'KA-01-202403', 9, '9876543212'),
(4, 'Mohan Das',    'KA-51-202404', 8, '9876543213'),
(5, 'Lakshmi Iyer', 'KA-51-202405', 8, '9876543214'),
(6, 'Imran Khan',   'KA-01-202406', 7, '9876543215');

-- (the CreateSeats trigger builds each vehicle's seat map as the vehicle is inserted)
INSERT IGNORE INTO Vehicle (VehicleID, VehicleNo, Type, Capacity, SeatsPerRow, Model, Status, DriverID) VALUES
(1, 'KA-01-F-1234', 'Bus',   40, 4, 'Tata Starbus',         'Active',      1),
(2, 'KA-01-F-5678', 'Bus',   45, 4, 'Ashok Leyland Viking', 'Active',      2),
(3, 'KA-01-F-9012', 'Bus',   30, 3, 'Volvo 8400',           'Active',      3),
(4, 'KA-51-M-1001', 'Metro', 60, 6, 'BEML Metro',           'Active',      4),
(5, 'KA-51-M-1002', 'Metro', 60, 6, 'BEML Metro',           'Active',      5),
(6, 'KA-01-F-3456', 'Bus',   40, 4, 'Eicher Skyline',       'Maintenance', NULL);

-- Departures 1-10 run every day; 11 is a weekday office service (not on public holidays), 12 a weekend extra.
-- (the DefaultServiceDays trigger gives every new departure all seven days; the two exceptions are narrowed after)
SET @seed_schedules = NOT EXISTS (SELECT 1 FROM Schedule);
INSERT INTO Schedule (ScheduleID, RouteID, VehicleID, DepartureTime, ArrivalTime, Frequency, RunsOnHolidays)
SELECT * FROM (
    SELECT 1 AS ScheduleID, 1 AS RouteID, 1 AS VehicleID, '06:00:00' AS DepartureTime, '07:30:00' AS ArrivalTime, 30 AS Frequency, TRUE AS RunsOnHolidays
    UNION ALL SELECT 2,  1, 2, '08:00:00', '09:30:00', 30, TRUE
    UNION ALL SELECT 3,  2, 2, '10:00:00', '11:15:00', 20, TRUE
    UNION ALL SELECT 4,  2, 3, '17:00:00', '18:30:00', 20, TRUE
    UNION ALL SELECT 5,  3, 4, '07:00:00', '08:10:00', 10, TRUE
    UNION ALL SELECT 6,  3, 4, '18:00:00', '19:10:00', 10, TRUE
    UNION ALL SELECT 7,  4, 5, '09:00:00', '09:55:00', 10, TRUE
    UNION ALL SELECT 8,  5, 3, '05:30:00', '06:45:00', 60, TRUE
    UNION ALL SELECT 9,  5, 1, '14:00:00', '15:15:00', 60, TRUE
    UNION ALL SELECT 10, 4, 5, '20:00:00', '20:55:00', 15, TRUE
    UNION ALL SELECT 11, 1, 1, '10:00:00', '11:30:00', 30, FALSE
    UNION ALL SELECT 12, 3, 4, '12:00:00', '13:10:00', 20, TRUE
) AS sch
WHERE @seed_schedules;
DELETE FROM ScheduleDay WHERE @seed_schedules
    AND ((ScheduleID = 11 AND Weekday IN (1, 7)) OR (ScheduleID = 12 AND Weekday BETWEEN 2 AND 6));

INSERT IGNORE INTO Passenger (Name, Email, Phone, DOB, CardType) VALUES
('B M Thanu Sri',  'thanu@gmail.com',    '9000000001', '2004-05-10', 'Monthly'),
('Sanvi S',        'sanvi@gmail.com',    '9000000002', '2004-08-22', 'Weekly'),
('Akash V',        'akash@gmail.com',    '9000000005', '2003-07-20', 'Monthly'),
('Priya Mani',     'priya@gmail.com',    '9000000006', '2005-02-14', 'Weekly'),
('Rahul Hegde',    'rahul@gmail.com',    '9000000007', '2004-11-30', 'None'),
('Kavya Shree',    'kavya@gmail.com',    '9000000008', '2002-05-05', 'Daily'),
('Santhosh Kumar', 'santhosh@gmail.com', '9000000009', '2001-09-12', 'Monthly'),
('Meera Nair',     'meera@gmail.com',    '9000000010', '2003-01-18', 'Weekly'),
('Arjun Reddy',    'arjun@gmail.com',    '9000000011', '2002-12-02', 'None'),
('Divya Patel',    'divya@gmail.com',    '9000000012', '2004-03-27', 'Monthly'),
('Vikram Singh',   'vikram@gmail.com',   '9000000013', '2001-06-09', 'Daily'),
('Ananya Joshi',   'ananya@gmail.com',   '9000000014', '2005-09-30', 'Weekly'),
('Neha Kapoor',    'neha@gmail.com',     '9000000015', '2003-10-11', 'None');

-- Public holidays (fixed-date national and Karnataka state holidays)
INSERT IGNORE INTO Holiday (HolidayDate, Name) VALUES
('2026-08-15', 'Independence Day'), ('2026-10-02', 'Gandhi Jayanti'), ('2026-11-01', 'Karnataka Rajyotsava'),
('2026-12-25', 'Christmas'), ('2027-01-26', 'Republic Day'), ('2027-05-01', 'May Day'), ('2027-08-15', 'Independence Day');

-- Sample tickets / feedback: inserted only into an empty table so re-imports stay clean (tickets are for today,
-- on departures that run every day)
INSERT INTO Ticket (PassengerID, ScheduleID, TravelDate, SeatNo, Fare)
SELECT t.PassengerID, t.ScheduleID, CURDATE(), t.SeatNo, t.Fare FROM (
    SELECT 1 AS PassengerID, 1 AS ScheduleID, '1A' AS SeatNo, 45.00 AS Fare
    UNION ALL SELECT 2, 1, '1B', 45.00   UNION ALL SELECT 3, 1, '2A', 50.00
    UNION ALL SELECT 9, 1, '3A', 45.00   UNION ALL SELECT 4, 2, '1A', 45.00
    UNION ALL SELECT 5, 2, '1B', 45.00   UNION ALL SELECT 8, 2, '2A', 45.00
    UNION ALL SELECT 6, 3, '3A', 30.00   UNION ALL SELECT 7, 3, '3B', 30.00
    UNION ALL SELECT 1, 3, '3C', 30.00   UNION ALL SELECT 8, 4, '4A', 35.00
    UNION ALL SELECT 10, 4, '4B', 35.00  UNION ALL SELECT 9, 5, '1A', 25.00
    UNION ALL SELECT 10, 5, '1B', 25.00  UNION ALL SELECT 11, 5, '1C', 25.00
    UNION ALL SELECT 12, 6, '1A', 25.00  UNION ALL SELECT 1, 6, '1B', 25.00
    UNION ALL SELECT 2, 7, '2A', 30.00   UNION ALL SELECT 3, 7, '2B', 30.00
    UNION ALL SELECT 7, 10, '2A', 30.00  UNION ALL SELECT 4, 8, '5A', 60.00
    UNION ALL SELECT 5, 8, '5B', 60.00   UNION ALL SELECT 6, 9, '5A', 60.00
    UNION ALL SELECT 11, 9, '5B', 60.00
) AS t
WHERE NOT EXISTS (SELECT 1 FROM Ticket);

INSERT INTO Feedback (PassengerID, RouteID, Rating, Comments)
SELECT * FROM (
    SELECT 1 AS PassengerID, 1 AS RouteID, 5 AS Rating, 'Bus was on time and clean.' AS Comments
    UNION ALL SELECT 2, 1, 4, 'Good service, a bit crowded in the morning.'
    UNION ALL SELECT 3, 1, 3, 'Seats could be more comfortable.'
    UNION ALL SELECT 4, 2, 4, 'Reliable evening service.'
    UNION ALL SELECT 5, 2, 2, 'Bus arrived 20 minutes late.'
    UNION ALL SELECT 6, 3, 5, 'Metro is fast and comfortable.'
    UNION ALL SELECT 7, 3, 5, 'Best way to beat the traffic.'
    UNION ALL SELECT 8, 4, 4, 'Clean stations, frequent trains.'
    UNION ALL SELECT 9, 5, 3, 'Airport bus is a little pricey.'
    UNION ALL SELECT 10, 5, 4, 'Plenty of luggage space.'
) AS f
WHERE NOT EXISTS (SELECT 1 FROM Feedback);

-- A fresh install already contains everything up to version 5.
INSERT IGNORE INTO SchemaVersion (Version, Name) VALUES
(1, 'baseline: bookings, audit trail, roles, least-privilege user'),
(2, 'travel dates, fare rules, type check, audit actor, schema versioning'),
(3, 'seat map, service days and holidays, full-text search, stored sign-in limit'),
(4, 'seat holds, partitioned audit trail, departure board, invoker-rights maintenance routines'),
(5, 'DATETIME partitions, ScheduleDay table, one-day vehicle swaps, payment ledger');
