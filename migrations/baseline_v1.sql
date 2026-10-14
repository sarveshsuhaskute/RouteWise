-- =====================================================================
--  BASELINE (version 1) of the RouteWise schema, kept ONLY so the migration test can prove that
--  upgrading a version-1 database with scripts/migrate.py gives exactly the same result as a fresh install.
--  Do not import this file for normal use: run schema.sql instead.
--
--  Functional dependencies (every relation is in BCNF: each determinant is a candidate key)
--    Passenger  : PassengerID -> all;  Email -> all
--    Driver     : DriverID -> all;     LicenseNo -> all
--    Route      : RouteID -> all
--    Vehicle    : VehicleID -> all;    VehicleNo -> all
--    Schedule   : ScheduleID -> all;   (VehicleID, DepartureTime) -> all
--    Ticket     : TicketID -> all;     (ScheduleID, SeatNo) -> all
--    Feedback   : FeedbackID -> all
--    TicketAudit: AuditID -> all       AppUser: UserID -> all;  Username -> all
-- =====================================================================
-- every script declares its character set, so the result does not depend on the client that runs it
SET NAMES utf8mb4;
CREATE DATABASE IF NOT EXISTS public_transport;
USE public_transport;

-- ---------------------------------------------------------------------
--  1. TABLES
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS Passenger (
    PassengerID INT AUTO_INCREMENT PRIMARY KEY,
    Name        VARCHAR(100) NOT NULL,
    Email       VARCHAR(100) NOT NULL UNIQUE,
    Phone       VARCHAR(15),
    DOB         DATE,
    CardType    VARCHAR(20) NOT NULL DEFAULT 'None',
    CONSTRAINT chk_card CHECK (CardType IN ('None', 'Daily', 'Weekly', 'Monthly'))
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
    CONSTRAINT chk_distance CHECK (Distance IS NULL OR Distance > 0)
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
    FOREIGN KEY (DriverID) REFERENCES Driver(DriverID) ON DELETE SET NULL
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

CREATE TABLE IF NOT EXISTS Ticket (
    TicketID    INT AUTO_INCREMENT PRIMARY KEY,
    PassengerID INT NOT NULL,
    ScheduleID  INT NOT NULL,
    SeatNo      VARCHAR(10) NOT NULL,
    Fare        DECIMAL(6,2) NOT NULL,
    BookingDate TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_fare CHECK (Fare > 0),
    UNIQUE KEY uq_schedule_seat (ScheduleID, SeatNo),   -- race-proof seat uniqueness
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

-- Written to by triggers only: a permanent history of bookings/cancellations
CREATE TABLE IF NOT EXISTS TicketAudit (
    AuditID     INT AUTO_INCREMENT PRIMARY KEY,
    TicketID    INT NOT NULL,
    Action      VARCHAR(20) NOT NULL,
    PassengerID INT NOT NULL,
    ScheduleID  INT NOT NULL,
    SeatNo      VARCHAR(10) NOT NULL,
    Fare        DECIMAL(6,2) NOT NULL,
    ActionTime  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
CALL _add_index('Schedule', 'idx_schedule_route',   'RouteID');
CALL _add_index('Schedule', 'idx_schedule_vehicle', 'VehicleID');
CALL _add_index('Feedback', 'idx_feedback_route',   'RouteID');
DROP PROCEDURE _add_index;

-- ---------------------------------------------------------------------
--  3. VIEWS
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW BookingSummary AS
SELECT T.TicketID, P.Name AS PassengerName, R.RouteName, V.VehicleNo,
       S.DepartureTime, T.SeatNo, T.Fare, T.BookingDate
FROM Ticket T
JOIN Passenger P ON T.PassengerID = P.PassengerID
JOIN Schedule  S ON T.ScheduleID  = S.ScheduleID
JOIN Route     R ON S.RouteID     = R.RouteID
JOIN Vehicle   V ON S.VehicleID   = V.VehicleID;

CREATE OR REPLACE VIEW ScheduleOccupancy AS
SELECT S.ScheduleID, R.RouteName, V.VehicleNo, V.Status AS VehicleStatus,
       S.DepartureTime, V.Capacity,
       COUNT(T.TicketID)                                   AS Booked,
       V.Capacity - COUNT(T.TicketID)                      AS SeatsLeft,
       ROUND(100 * COUNT(T.TicketID) / V.Capacity, 1)      AS OccupancyPct
FROM Schedule S
JOIN Route   R ON S.RouteID   = R.RouteID
JOIN Vehicle V ON S.VehicleID = V.VehicleID
LEFT JOIN Ticket T ON T.ScheduleID = S.ScheduleID
GROUP BY S.ScheduleID, R.RouteName, V.VehicleNo, V.Status, S.DepartureTime, V.Capacity;

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
--  4. FUNCTION, PROCEDURES
-- ---------------------------------------------------------------------
DROP FUNCTION IF EXISTS SeatsLeft;
DELIMITER $$
CREATE FUNCTION SeatsLeft(p_schedule INT) RETURNS INT
READS SQL DATA
BEGIN
    RETURN (SELECT V.Capacity FROM Schedule S JOIN Vehicle V ON S.VehicleID = V.VehicleID
            WHERE S.ScheduleID = p_schedule)
         - (SELECT COUNT(*) FROM Ticket WHERE ScheduleID = p_schedule);
END$$
DELIMITER ;

-- Fare = max(10, distance x rate) minus a travel-card discount (Daily 10%, Weekly 20%, Monthly 30%)
DROP FUNCTION IF EXISTS CalcFare;
DELIMITER $$
CREATE FUNCTION CalcFare(p_passenger INT, p_schedule INT) RETURNS DECIMAL(6,2)
READS SQL DATA
BEGIN
    DECLARE v_dist DECIMAL(5,2);
    DECLARE v_type VARCHAR(20);
    DECLARE v_card VARCHAR(20);
    DECLARE v_base DECIMAL(8,2);
    DECLARE v_pct  INT;
    SELECT R.Distance, R.Type INTO v_dist, v_type
    FROM Schedule S JOIN Route R ON S.RouteID = R.RouteID WHERE S.ScheduleID = p_schedule;
    SELECT CardType INTO v_card FROM Passenger WHERE PassengerID = p_passenger;
    IF v_type IS NULL OR v_card IS NULL THEN
        RETURN NULL;
    END IF;
    SET v_base = GREATEST(10, ROUND(COALESCE(v_dist, 10) * IF(v_type = 'Metro', 1.0, 1.5), 2));
    SET v_pct = CASE v_card WHEN 'Monthly' THEN 30 WHEN 'Weekly' THEN 20 WHEN 'Daily' THEN 10 ELSE 0 END;
    RETURN ROUND(v_base * (100 - v_pct) / 100, 2);
END$$
DELIMITER ;

DROP PROCEDURE IF EXISTS BookTicket;
DELIMITER $$
CREATE PROCEDURE BookTicket(IN p_p_id INT, IN p_s_id INT, IN p_seat VARCHAR(10), IN p_fare DECIMAL(6,2))
BEGIN
    -- NULL fare = calculate it automatically
    INSERT INTO Ticket (PassengerID, ScheduleID, SeatNo, Fare)
    VALUES (p_p_id, p_s_id, p_seat, COALESCE(p_fare, CalcFare(p_p_id, p_s_id)));
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

-- Moves a passenger to another departure/seat as ONE transaction (all-or-nothing).
-- The old ticket is deleted and the new one inserted; if any step fails (seat taken, vehicle full,
-- vehicle out of service, bad schedule...) the handler rolls everything back, so the passenger
-- never ends up with no ticket or with two.
DROP PROCEDURE IF EXISTS TransferTicket;
DELIMITER $$
CREATE PROCEDURE TransferTicket(IN p_ticket INT, IN p_new_schedule INT, IN p_new_seat VARCHAR(10))
BEGIN
    DECLARE v_passenger INT;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        ROLLBACK;
        RESIGNAL;
    END;

    START TRANSACTION;
    SELECT PassengerID INTO v_passenger FROM Ticket WHERE TicketID = p_ticket FOR UPDATE;
    IF v_passenger IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Ticket not found.';
    END IF;
    DELETE FROM Ticket WHERE TicketID = p_ticket;                     -- frees the old seat first
    INSERT INTO Ticket (PassengerID, ScheduleID, SeatNo, Fare)        -- triggers validate the new seat
    VALUES (v_passenger, p_new_schedule, p_new_seat, CalcFare(v_passenger, p_new_schedule));
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
-- 5a. Reject a seat that is already booked on the same schedule
DROP TRIGGER IF EXISTS PreventDoubleSeat;
DELIMITER $$
CREATE TRIGGER PreventDoubleSeat BEFORE INSERT ON Ticket FOR EACH ROW
BEGIN
    DECLARE v_lock INT;
    -- Lock the departure first: simultaneous bookings for one schedule queue up here, one at a time,
    -- instead of deadlocking or overselling. This trigger runs before CheckCapacity (see FOLLOWS below).
    SELECT ScheduleID INTO v_lock FROM Schedule WHERE ScheduleID = NEW.ScheduleID FOR UPDATE;
    IF EXISTS (SELECT 1 FROM Ticket WHERE ScheduleID = NEW.ScheduleID AND SeatNo = NEW.SeatNo) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Seat already booked.';
    END IF;
END$$
DELIMITER ;

-- 5b. Reject bookings on a full vehicle or one that is not in service
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
    SELECT COUNT(*) INTO v_sold FROM Ticket WHERE ScheduleID = NEW.ScheduleID;
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

-- 5d/5e. Audit trail for every booking and cancellation
DROP TRIGGER IF EXISTS AuditBooking;
DELIMITER $$
CREATE TRIGGER AuditBooking AFTER INSERT ON Ticket FOR EACH ROW
BEGIN
    INSERT INTO TicketAudit (TicketID, Action, PassengerID, ScheduleID, SeatNo, Fare)
    VALUES (NEW.TicketID, 'BOOKED', NEW.PassengerID, NEW.ScheduleID, NEW.SeatNo, NEW.Fare);
END$$
DELIMITER ;

DROP TRIGGER IF EXISTS AuditCancellation;
DELIMITER $$
CREATE TRIGGER AuditCancellation AFTER DELETE ON Ticket FOR EACH ROW
BEGIN
    INSERT INTO TicketAudit (TicketID, Action, PassengerID, ScheduleID, SeatNo, Fare)
    VALUES (OLD.TicketID, 'CANCELLED', OLD.PassengerID, OLD.ScheduleID, OLD.SeatNo, OLD.Fare);
END$$
DELIMITER ;

-- ---------------------------------------------------------------------
--  5b. DATABASE USER FOR THE APPLICATION (least privilege)
--  The web application connects as 'routewise_app', never as root. It may read and change business data,
--  call the stored routines and read the views, but it cannot create or drop objects, and it can only READ
--  the audit table (the triggers, which run with their definer's rights, are the only writers).
--  Demo password: change it in production and set DB_USER / DB_PASSWORD for the application.
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
GRANT SELECT ON public_transport.TicketAudit       TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.BookingSummary    TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.ScheduleOccupancy TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.RouteRevenue      TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.RouteRatings      TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.ActiveVehicle     TO 'routewise_app'@'localhost';
GRANT EXECUTE ON public_transport.* TO 'routewise_app'@'localhost';

-- ---------------------------------------------------------------------
--  6. SEED DATA
-- ---------------------------------------------------------------------
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

-- Sample tickets / feedback: inserted only into an empty table so re-imports stay clean
INSERT INTO Ticket (PassengerID, ScheduleID, SeatNo, Fare)
SELECT * FROM (
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
