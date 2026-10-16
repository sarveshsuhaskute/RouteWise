-- =====================================================================
--  Migration 0004: seat holds that expire, a partitioned audit trail with automatic partition maintenance,
--  the departure board, and invoker-rights maintenance routines
--  Upgrades a version-3 database in place; no existing row changes.
--  Run it with:  python scripts/migrate.py      (it takes a backup first)
--  The structural part is hand-written; everything else below is generated from schema.sql by make_0004.py.
-- =====================================================================

SET NAMES utf8mb4;

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

-- the audit trail becomes a partitioned table: the partitioning column must belong to the primary key
UPDATE TicketAudit SET ActionTime = CURRENT_TIMESTAMP WHERE ActionTime IS NULL;
ALTER TABLE TicketAudit MODIFY ActionTime TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        DROP PRIMARY KEY, ADD PRIMARY KEY (AuditID, ActionTime);
ALTER TABLE TicketAudit
PARTITION BY RANGE (UNIX_TIMESTAMP(ActionTime)) (
    PARTITION p_before_2026 VALUES LESS THAN (UNIX_TIMESTAMP('2026-01-01 00:00:00')),
    PARTITION p2026q1 VALUES LESS THAN (UNIX_TIMESTAMP('2026-04-01 00:00:00')),
    PARTITION p2026q2 VALUES LESS THAN (UNIX_TIMESTAMP('2026-07-01 00:00:00')),
    PARTITION p2026q3 VALUES LESS THAN (UNIX_TIMESTAMP('2026-10-01 00:00:00')),
    PARTITION p2026q4 VALUES LESS THAN (UNIX_TIMESTAMP('2027-01-01 00:00:00')),
    PARTITION p2027q1 VALUES LESS THAN (UNIX_TIMESTAMP('2027-04-01 00:00:00')),
    PARTITION p2027q2 VALUES LESS THAN (UNIX_TIMESTAMP('2027-07-01 00:00:00')),
    PARTITION p2027q3 VALUES LESS THAN (UNIX_TIMESTAMP('2027-10-01 00:00:00')),
    PARTITION p2027q4 VALUES LESS THAN (UNIX_TIMESTAMP('2028-01-01 00:00:00')),
    PARTITION pmax    VALUES LESS THAN MAXVALUE
);

-- indexes (unchanged; re-run safe)
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

-- the programmable objects, copied from schema.sql
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

-- Does a departure run on a date? Its service days must include the weekday, and on a public holiday
-- it runs only when RunsOnHolidays is set. NULL for an unknown schedule.
DROP FUNCTION IF EXISTS RunsOn;
DELIMITER $$
CREATE FUNCTION RunsOn(p_schedule INT, p_date DATE) RETURNS BOOLEAN
READS SQL DATA
BEGIN
    RETURN (SELECT FIND_IN_SET(ELT(DAYOFWEEK(p_date), 'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'), ServiceDays) > 0
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
JOIN Vehicle   V ON S.VehicleID   = V.VehicleID;

-- Occupancy of every departure TODAY (what a clerk needs when selling a ticket), and whether it runs today.
CREATE OR REPLACE VIEW ScheduleOccupancy AS
SELECT S.ScheduleID, R.RouteName, V.VehicleNo, V.Status AS VehicleStatus,
       S.DepartureTime, S.ServiceDays, S.RunsOnHolidays, RunsOn(S.ScheduleID, CURDATE()) AS RunsToday,
       CURDATE() AS TravelDate, V.Capacity,
       COUNT(T.TicketID)                                   AS Booked,
       V.Capacity - COUNT(T.TicketID)                      AS SeatsLeft,
       ROUND(100 * COUNT(T.TicketID) / V.Capacity, 1)      AS OccupancyPct
FROM Schedule S
JOIN Route   R ON S.RouteID   = R.RouteID
JOIN Vehicle V ON S.VehicleID = V.VehicleID
LEFT JOIN Ticket T ON T.ScheduleID = S.ScheduleID AND T.TravelDate = CURDATE()
GROUP BY S.ScheduleID, R.RouteName, V.VehicleNo, V.Status, S.DepartureTime, S.ServiceDays, S.RunsOnHolidays, V.Capacity;

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

-- Today's departure board: every departure that runs today, with a status worked out from the timetable and the clock.
CREATE OR REPLACE VIEW DepartureBoard AS
SELECT S.ScheduleID, R.RouteName, R.Origin, R.Destination, V.VehicleNo, S.DepartureTime, S.ArrivalTime,
       CASE WHEN V.Status <> 'Active'                              THEN 'Cancelled'
            WHEN CURTIME() < SUBTIME(S.DepartureTime, '00:10:00')  THEN 'Scheduled'
            WHEN CURTIME() < S.DepartureTime                       THEN 'Boarding'
            WHEN CURTIME() < S.ArrivalTime                         THEN 'En route'
            ELSE 'Arrived' END                                     AS Status,
       SeatsLeft(S.ScheduleID, CURDATE())                          AS SeatsLeft
FROM Schedule S
JOIN Route   R ON R.RouteID   = S.RouteID
JOIN Vehicle V ON V.VehicleID = S.VehicleID
WHERE RunsOn(S.ScheduleID, CURDATE());

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
    ELSEIF NOT EXISTS (SELECT 1 FROM Schedule S JOIN Seat ST ON ST.VehicleID = S.VehicleID
                       WHERE S.ScheduleID = p_schedule AND ST.SeatNo = p_seat) THEN
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
    DECLARE v_last BIGINT;
    DECLARE v_start DATETIME;
    extend: LOOP
        SELECT MAX(CAST(PARTITION_DESCRIPTION AS UNSIGNED)) INTO v_last FROM information_schema.partitions
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'TicketAudit' AND PARTITION_DESCRIPTION <> 'MAXVALUE';
        SET v_start = FROM_UNIXTIME(v_last);
        IF v_start >= NOW() + INTERVAL p_months_ahead MONTH THEN
            LEAVE extend;
        END IF;
        SET @ddl = CONCAT('ALTER TABLE TicketAudit REORGANIZE PARTITION pmax INTO (PARTITION p', YEAR(v_start), 'q', QUARTER(v_start),
                          ' VALUES LESS THAN (UNIX_TIMESTAMP(''', v_start + INTERVAL 3 MONTH, ''')), PARTITION pmax VALUES LESS THAN MAXVALUE)');
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

-- 6d. The seat must exist in the seat map of the departure's vehicle
DROP TRIGGER IF EXISTS ValidateSeat;
DELIMITER $$
CREATE TRIGGER ValidateSeat BEFORE INSERT ON Ticket FOR EACH ROW FOLLOWS CheckCapacity
BEGIN
    IF NOT EXISTS (SELECT 1 FROM Schedule S JOIN Seat ST ON ST.VehicleID = S.VehicleID
                   WHERE S.ScheduleID = NEW.ScheduleID AND ST.SeatNo = NEW.SeatNo) THEN
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
        IF EXISTS (SELECT 1 FROM Ticket T JOIN Schedule S ON S.ScheduleID = T.ScheduleID
                   WHERE S.VehicleID = NEW.VehicleID AND T.TravelDate >= CURDATE()
                     AND NOT EXISTS (SELECT 1 FROM Seat ST WHERE ST.VehicleID = NEW.VehicleID AND ST.SeatNo = T.SeatNo)) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Seats booked for upcoming trips would disappear with this capacity or layout.';
        END IF;
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

-- database user privileges
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

-- record the new version
INSERT IGNORE INTO SchemaVersion (Version, Name) VALUES
(1, 'baseline: bookings, audit trail, roles, least-privilege user'),
(2, 'travel dates, fare rules, type check, audit actor, schema versioning'),
(3, 'seat map, service days and holidays, full-text search, stored sign-in limit'),
(4, 'seat holds, partitioned audit trail, departure board, invoker-rights maintenance routines');
