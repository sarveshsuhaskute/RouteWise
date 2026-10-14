-- =====================================================================
--  Public Transportation Optimization System  -  MySQL 8+ schema (version 2)
--  Import in MySQL Workbench, or:  mysql -u root -p < schema.sql
--  The script is re-runnable: tables use IF NOT EXISTS, programmable
--  objects are dropped and recreated, seed data is only added once.
--  Upgrading an existing version-1 database without losing data: scripts/migrate.py
--
--  Functional dependencies (every relation is in BCNF: each determinant is a candidate key)
--    FareRule   : RouteType -> all     CardDiscount: CardType -> all
--    Passenger  : PassengerID -> all;  Email -> all
--    Driver     : DriverID -> all;     LicenseNo -> all
--    Route      : RouteID -> all
--    Vehicle    : VehicleID -> all;    VehicleNo -> all
--    Schedule   : ScheduleID -> all;   (VehicleID, DepartureTime) -> all
--    Ticket     : TicketID -> all;     (ScheduleID, TravelDate, SeatNo) -> all
--    Feedback   : FeedbackID -> all
--    TicketAudit: AuditID -> all       AppUser: UserID -> all;  Username -> all
--    SchemaVersion: Version -> all
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
    VehicleID INT AUTO_INCREMENT PRIMARY KEY,
    VehicleNo VARCHAR(20) NOT NULL UNIQUE,
    Type      VARCHAR(20) NOT NULL,
    Capacity  INT NOT NULL,
    Model     VARCHAR(50),
    Status    VARCHAR(20) NOT NULL DEFAULT 'Active',
    DriverID  INT NULL,
    CONSTRAINT chk_capacity CHECK (Capacity > 0),
    CONSTRAINT chk_vstatus  CHECK (Status IN ('Active', 'Maintenance', 'Retired')),
    FOREIGN KEY (DriverID) REFERENCES Driver(DriverID) ON DELETE SET NULL,
    CONSTRAINT fk_vehicle_type FOREIGN KEY (Type) REFERENCES FareRule(RouteType)
);

CREATE TABLE IF NOT EXISTS Schedule (
    ScheduleID    INT AUTO_INCREMENT PRIMARY KEY,
    RouteID       INT  NOT NULL,
    VehicleID     INT  NOT NULL,
    DepartureTime TIME NOT NULL,
    ArrivalTime   TIME NOT NULL,
    Frequency     INT,
    CONSTRAINT chk_times CHECK (ArrivalTime > DepartureTime),
    FOREIGN KEY (RouteID)   REFERENCES Route(RouteID),
    FOREIGN KEY (VehicleID) REFERENCES Vehicle(VehicleID)
);

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
    CONSTRAINT chk_seat   CHECK (SeatNo REGEXP '^[A-Za-z0-9]{1,6}$'),
    CONSTRAINT chk_travel CHECK (TravelDate >= DATE(BookingDate) AND TravelDate <= DATE(BookingDate) + INTERVAL 30 DAY),
    UNIQUE KEY uq_trip_seat (ScheduleID, TravelDate, SeatNo),   -- race-proof seat uniqueness per trip
    FOREIGN KEY (PassengerID) REFERENCES Passenger(PassengerID),
    FOREIGN KEY (ScheduleID)  REFERENCES Schedule(ScheduleID)
);

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
CREATE TABLE IF NOT EXISTS TicketAudit (
    AuditID     INT AUTO_INCREMENT PRIMARY KEY,
    TicketID    INT NOT NULL,
    Action      VARCHAR(20) NOT NULL,
    PassengerID INT NOT NULL,
    ScheduleID  INT NOT NULL,
    TravelDate  DATE NULL,
    SeatNo      VARCHAR(10) NOT NULL,
    Fare        DECIMAL(6,2) NOT NULL,
    ActionTime  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    Actor       VARCHAR(50) NULL
);

-- Which version of this schema a database is at (managed by schema.sql and scripts/migrate.py).
CREATE TABLE IF NOT EXISTS SchemaVersion (
    Version   INT PRIMARY KEY,
    Name      VARCHAR(120) NOT NULL,
    AppliedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------
--  2. INDEXES (speed up the joins / lookups used by the app)
-- ---------------------------------------------------------------------
-- (re-run safe: an index is only created when it does not exist yet)
DROP PROCEDURE IF EXISTS _add_index;
DELIMITER $$
CREATE PROCEDURE _add_index(IN p_table VARCHAR(64), IN p_index VARCHAR(64), IN p_cols VARCHAR(128))
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.statistics
                   WHERE table_schema = DATABASE() AND table_name = p_table AND index_name = p_index) THEN
        SET @s = CONCAT('CREATE INDEX ', p_index, ' ON ', p_table, ' (', p_cols, ')');
        PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;
    END IF;
END$$
DELIMITER ;
CALL _add_index('Ticket',   'idx_ticket_passenger', 'PassengerID');
CALL _add_index('Ticket',   'idx_ticket_date',      'TravelDate');
CALL _add_index('Schedule', 'idx_schedule_route',   'RouteID');
CALL _add_index('Schedule', 'idx_schedule_vehicle', 'VehicleID');
CALL _add_index('Feedback', 'idx_feedback_route',   'RouteID');
DROP PROCEDURE _add_index;

-- ---------------------------------------------------------------------
--  3. VIEWS
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW BookingSummary AS
SELECT T.TicketID, P.Name AS PassengerName, R.RouteName, V.VehicleNo,
       S.DepartureTime, T.TravelDate, T.SeatNo, T.Fare, T.BookingDate
FROM Ticket T
JOIN Passenger P ON T.PassengerID = P.PassengerID
JOIN Schedule  S ON T.ScheduleID  = S.ScheduleID
JOIN Route     R ON S.RouteID     = R.RouteID
JOIN Vehicle   V ON S.VehicleID   = V.VehicleID;

-- Occupancy of every departure TODAY (what a clerk needs when selling a ticket).
CREATE OR REPLACE VIEW ScheduleOccupancy AS
SELECT S.ScheduleID, R.RouteName, V.VehicleNo, V.Status AS VehicleStatus,
       S.DepartureTime, CURDATE() AS TravelDate, V.Capacity,
       COUNT(T.TicketID)                                   AS Booked,
       V.Capacity - COUNT(T.TicketID)                      AS SeatsLeft,
       ROUND(100 * COUNT(T.TicketID) / V.Capacity, 1)      AS OccupancyPct
FROM Schedule S
JOIN Route   R ON S.RouteID   = R.RouteID
JOIN Vehicle V ON S.VehicleID = V.VehicleID
LEFT JOIN Ticket T ON T.ScheduleID = S.ScheduleID AND T.TravelDate = CURDATE()
GROUP BY S.ScheduleID, R.RouteName, V.VehicleNo, V.Status, S.DepartureTime, V.Capacity;

-- Occupancy of every TRIP (schedule on a date) that has at least one ticket, for any date.
CREATE OR REPLACE VIEW TripOccupancy AS
SELECT S.ScheduleID, R.RouteName, V.VehicleNo, S.DepartureTime, T.TravelDate, V.Capacity,
       COUNT(*)                                            AS Booked,
       V.Capacity - COUNT(*)                               AS SeatsLeft,
       ROUND(100 * COUNT(*) / V.Capacity, 1)               AS OccupancyPct
FROM Ticket T
JOIN Schedule S ON T.ScheduleID = S.ScheduleID
JOIN Route    R ON S.RouteID    = R.RouteID
JOIN Vehicle  V ON S.VehicleID  = V.VehicleID
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

-- ---------------------------------------------------------------------
--  4. FUNCTIONS, PROCEDURES
-- ---------------------------------------------------------------------
DROP FUNCTION IF EXISTS SeatsLeft;
DELIMITER $$
CREATE FUNCTION SeatsLeft(p_schedule INT, p_date DATE) RETURNS INT
READS SQL DATA
BEGIN
    RETURN (SELECT V.Capacity FROM Schedule S JOIN Vehicle V ON S.VehicleID = V.VehicleID
            WHERE S.ScheduleID = p_schedule)
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
--  5. TRIGGERS
-- ---------------------------------------------------------------------
-- 5a. Reject a seat that is already booked on the same trip (schedule + travel date)
DROP TRIGGER IF EXISTS PreventDoubleSeat;
DELIMITER $$
CREATE TRIGGER PreventDoubleSeat BEFORE INSERT ON Ticket FOR EACH ROW
BEGIN
    DECLARE v_lock INT;
    -- Lock the departure first: simultaneous bookings for one schedule queue up here, one at a time,
    -- instead of deadlocking or overselling. This trigger runs before CheckCapacity (see FOLLOWS below).
    SELECT ScheduleID INTO v_lock FROM Schedule WHERE ScheduleID = NEW.ScheduleID FOR UPDATE;
    IF EXISTS (SELECT 1 FROM Ticket WHERE ScheduleID = NEW.ScheduleID AND TravelDate = NEW.TravelDate AND SeatNo = NEW.SeatNo) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Seat already booked.';
    END IF;
END$$
DELIMITER ;

-- 5b. Reject bookings on a full trip or on a vehicle that is not in service
DROP TRIGGER IF EXISTS CheckCapacity;
DELIMITER $$
CREATE TRIGGER CheckCapacity BEFORE INSERT ON Ticket FOR EACH ROW FOLLOWS PreventDoubleSeat
BEGIN
    DECLARE v_cap INT;
    DECLARE v_status VARCHAR(20);
    DECLARE v_sold INT;
    SELECT V.Capacity, V.Status INTO v_cap, v_status
    FROM Schedule S JOIN Vehicle V ON S.VehicleID = V.VehicleID
    WHERE S.ScheduleID = NEW.ScheduleID;
    SELECT COUNT(*) INTO v_sold FROM Ticket WHERE ScheduleID = NEW.ScheduleID AND TravelDate = NEW.TravelDate;
    IF v_status <> 'Active' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Vehicle is not in service.';
    ELSEIF v_sold >= v_cap THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Vehicle is full.';
    END IF;
END$$
DELIMITER ;

-- 5c. A vehicle cannot be scheduled for two overlapping runs
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

-- 5d. A vehicle can only run routes of its own type (no metro train on a bus route)
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

-- 5e/5f. Audit trail for every booking and cancellation, including who did it
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

-- ---------------------------------------------------------------------
--  5g. DATABASE USER FOR THE APPLICATION (least privilege)
--  The web application connects as 'transitiq_app', never as root. It may read and change business data,
--  call the stored routines and read the views, but it cannot create or drop objects, and it can only READ
--  the audit table (the triggers, which run with their definer's rights, are the only writers).
--  Demo password: change it with scripts/set_app_password.sh before any real use.
-- ---------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'transitiq_app'@'localhost' IDENTIFIED BY 'transitiq_app_pw';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Passenger TO 'transitiq_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Driver    TO 'transitiq_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Route     TO 'transitiq_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Vehicle   TO 'transitiq_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Schedule  TO 'transitiq_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Ticket    TO 'transitiq_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Feedback  TO 'transitiq_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.AppUser   TO 'transitiq_app'@'localhost';
GRANT SELECT, UPDATE ON public_transport.FareRule      TO 'transitiq_app'@'localhost';
GRANT SELECT, UPDATE ON public_transport.CardDiscount  TO 'transitiq_app'@'localhost';
GRANT SELECT ON public_transport.SchemaVersion     TO 'transitiq_app'@'localhost';
GRANT SELECT ON public_transport.TicketAudit       TO 'transitiq_app'@'localhost';
GRANT SELECT ON public_transport.BookingSummary    TO 'transitiq_app'@'localhost';
GRANT SELECT ON public_transport.ScheduleOccupancy TO 'transitiq_app'@'localhost';
GRANT SELECT ON public_transport.TripOccupancy     TO 'transitiq_app'@'localhost';
GRANT SELECT ON public_transport.RouteRevenue      TO 'transitiq_app'@'localhost';
GRANT SELECT ON public_transport.RouteRatings      TO 'transitiq_app'@'localhost';
GRANT SELECT ON public_transport.ActiveVehicle     TO 'transitiq_app'@'localhost';
GRANT EXECUTE ON public_transport.* TO 'transitiq_app'@'localhost';

-- ---------------------------------------------------------------------
--  6. SEED DATA
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

INSERT IGNORE INTO Vehicle (VehicleID, VehicleNo, Type, Capacity, Model, Status, DriverID) VALUES
(1, 'KA-01-F-1234', 'Bus',   40, 'Tata Starbus',         'Active',      1),
(2, 'KA-01-F-5678', 'Bus',   45, 'Ashok Leyland Viking', 'Active',      2),
(3, 'KA-01-F-9012', 'Bus',   30, 'Volvo 8400',           'Active',      3),
(4, 'KA-51-M-1001', 'Metro', 60, 'BEML Metro',           'Active',      4),
(5, 'KA-51-M-1002', 'Metro', 60, 'BEML Metro',           'Active',      5),
(6, 'KA-01-F-3456', 'Bus',   40, 'Eicher Skyline',       'Maintenance', NULL);

INSERT INTO Schedule (ScheduleID, RouteID, VehicleID, DepartureTime, ArrivalTime, Frequency)
SELECT * FROM (
    SELECT 1 AS ScheduleID, 1 AS RouteID, 1 AS VehicleID, '06:00:00' AS DepartureTime, '07:30:00' AS ArrivalTime, 30 AS Frequency
    UNION ALL SELECT 2,  1, 2, '08:00:00', '09:30:00', 30
    UNION ALL SELECT 3,  2, 2, '10:00:00', '11:15:00', 20
    UNION ALL SELECT 4,  2, 3, '17:00:00', '18:30:00', 20
    UNION ALL SELECT 5,  3, 4, '07:00:00', '08:10:00', 10
    UNION ALL SELECT 6,  3, 4, '18:00:00', '19:10:00', 10
    UNION ALL SELECT 7,  4, 5, '09:00:00', '09:55:00', 10
    UNION ALL SELECT 8,  5, 3, '05:30:00', '06:45:00', 60
    UNION ALL SELECT 9,  5, 1, '14:00:00', '15:15:00', 60
    UNION ALL SELECT 10, 4, 5, '20:00:00', '20:55:00', 15
) AS sch
WHERE NOT EXISTS (SELECT 1 FROM Schedule);

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

-- Sample tickets / feedback: inserted only into an empty table so re-imports stay clean (tickets are for today)
INSERT INTO Ticket (PassengerID, ScheduleID, TravelDate, SeatNo, Fare)
SELECT t.PassengerID, t.ScheduleID, CURDATE(), t.SeatNo, t.Fare FROM (
    SELECT 1 AS PassengerID, 1 AS ScheduleID, 'A1' AS SeatNo, 45.00 AS Fare
    UNION ALL SELECT 2, 1, 'A2', 45.00   UNION ALL SELECT 3, 1, 'B1', 50.00
    UNION ALL SELECT 9, 1, 'C1', 45.00   UNION ALL SELECT 4, 2, 'A1', 45.00
    UNION ALL SELECT 5, 2, 'A2', 45.00   UNION ALL SELECT 8, 2, 'B1', 45.00
    UNION ALL SELECT 6, 3, 'C1', 30.00   UNION ALL SELECT 7, 3, 'C2', 30.00
    UNION ALL SELECT 1, 3, 'C3', 30.00   UNION ALL SELECT 8, 4, 'D1', 35.00
    UNION ALL SELECT 10, 4, 'D2', 35.00  UNION ALL SELECT 9, 5, 'M1', 25.00
    UNION ALL SELECT 10, 5, 'M2', 25.00  UNION ALL SELECT 11, 5, 'M3', 25.00
    UNION ALL SELECT 12, 6, 'M1', 25.00  UNION ALL SELECT 1, 6, 'M2', 25.00
    UNION ALL SELECT 2, 7, 'N1', 30.00   UNION ALL SELECT 3, 7, 'N2', 30.00
    UNION ALL SELECT 7, 10, 'N1', 30.00  UNION ALL SELECT 4, 8, 'E1', 60.00
    UNION ALL SELECT 5, 8, 'E2', 60.00   UNION ALL SELECT 6, 9, 'E1', 60.00
    UNION ALL SELECT 11, 9, 'E2', 60.00
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

-- A fresh install already contains everything up to version 2.
INSERT IGNORE INTO SchemaVersion (Version, Name) VALUES
(1, 'baseline: bookings, audit trail, roles, least-privilege user'),
(2, 'travel dates, fare rules, type check, audit actor, schema versioning');
