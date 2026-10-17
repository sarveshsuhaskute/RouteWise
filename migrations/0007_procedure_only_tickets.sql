-- =====================================================================
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

-- 6r-6z. Changing a row is checked as strictly as creating it. Without these, an UPDATE could slip past every rule
-- above: a fare edited after the sale (the ledger would no longer match), a ticket moved to a seat that does not exist,
-- a vehicle given two overlapping runs. Records of money and history cannot be changed at all.

-- A sold ticket is a fact: it is cancelled (refund + audit) or moved with TransferTicket (cancel + rebook in one
-- transaction), never edited in place.
DROP TRIGGER IF EXISTS ProtectTicket;
DELIMITER $$
CREATE TRIGGER ProtectTicket BEFORE UPDATE ON Ticket FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: A sold ticket cannot be edited; cancel it, or move it with TransferTicket.';
END$$
DELIMITER ;

-- A departure that changes vehicle, times or route passes the same checks as a new one, and passengers already
-- booked on it keep a valid seat.
DROP TRIGGER IF EXISTS CheckScheduleChange;
DELIMITER $$
CREATE TRIGGER CheckScheduleChange BEFORE UPDATE ON Schedule FOR EACH ROW
BEGIN
    DECLARE v_vehicle_type VARCHAR(20);
    DECLARE v_route_type VARCHAR(20);
    DECLARE v_moved BOOLEAN DEFAULT NEW.VehicleID <> OLD.VehicleID OR NEW.DepartureTime <> OLD.DepartureTime
                                    OR NEW.ArrivalTime <> OLD.ArrivalTime;
    SELECT Type INTO v_vehicle_type FROM Vehicle WHERE VehicleID = NEW.VehicleID;
    SELECT Type INTO v_route_type   FROM Route   WHERE RouteID   = NEW.RouteID;
    IF v_moved AND EXISTS (SELECT 1 FROM Schedule WHERE VehicleID = NEW.VehicleID AND ScheduleID <> OLD.ScheduleID
                           AND NEW.DepartureTime < ArrivalTime AND NEW.ArrivalTime > DepartureTime) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Vehicle already has a run at that time.';
    ELSEIF v_vehicle_type <> v_route_type THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Vehicle type does not match the route type.';
    ELSEIF NEW.RouteID <> OLD.RouteID
           AND EXISTS (SELECT 1 FROM Ticket WHERE ScheduleID = OLD.ScheduleID AND TravelDate >= CURDATE()) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Tickets are sold on this departure; it cannot move to another route.';
    ELSEIF (NEW.DepartureTime <> OLD.DepartureTime OR NEW.ArrivalTime <> OLD.ArrivalTime)
           AND EXISTS (SELECT 1 FROM VehicleSwap WHERE ScheduleID = OLD.ScheduleID AND TravelDate >= CURDATE()) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Remove the vehicle swaps of this departure before changing its times.';
    ELSEIF NEW.VehicleID <> OLD.VehicleID
           AND EXISTS (SELECT 1 FROM Ticket T
                       WHERE T.ScheduleID = OLD.ScheduleID AND T.TravelDate >= CURDATE()
                         AND NOT EXISTS (SELECT 1 FROM VehicleSwap VS WHERE VS.ScheduleID = T.ScheduleID AND VS.TravelDate = T.TravelDate)
                         AND NOT EXISTS (SELECT 1 FROM Seat ST WHERE ST.VehicleID = NEW.VehicleID AND ST.SeatNo = T.SeatNo)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Seats sold on upcoming trips do not exist on the new vehicle.';
    ELSEIF OLD.RunsOnHolidays AND NOT NEW.RunsOnHolidays
           AND EXISTS (SELECT 1 FROM Ticket T JOIN Holiday H ON H.HolidayDate = T.TravelDate
                       WHERE T.ScheduleID = OLD.ScheduleID AND T.TravelDate >= CURDATE()) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Tickets are sold for this departure on an upcoming public holiday.';
    END IF;
END$$
DELIMITER ;

-- A vehicle that runs departures (or covers a swap) keeps its type: no bus turns into a metro train on a bus route.
-- It leaves service only when no passenger is booked on a trip it would still run.
DROP TRIGGER IF EXISTS CheckVehicleChange;
DELIMITER $$
CREATE TRIGGER CheckVehicleChange BEFORE UPDATE ON Vehicle FOR EACH ROW
BEGIN
    IF NEW.Type <> OLD.Type
       AND (EXISTS (SELECT 1 FROM Schedule WHERE VehicleID = OLD.VehicleID)
            OR EXISTS (SELECT 1 FROM VehicleSwap WHERE VehicleID = OLD.VehicleID AND TravelDate >= CURDATE())) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: This vehicle runs departures of its type; it cannot change type.';
    -- taken out of service: nobody may be left holding a ticket for a trip it would run (swap those trips first)
    ELSEIF OLD.Status = 'Active' AND NEW.Status <> 'Active'
       AND EXISTS (SELECT 1 FROM Ticket T JOIN Schedule S ON S.ScheduleID = T.ScheduleID
                   WHERE T.TravelDate >= CURDATE() AND TripVehicle(T.ScheduleID, T.TravelDate) = OLD.VehicleID
                     AND (T.TravelDate > CURDATE() OR S.DepartureTime > CURTIME())) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Passengers are booked on upcoming trips of this vehicle; swap in another vehicle for those trips first.';
    END IF;
END$$
DELIMITER ;

-- Seat labels come from the vehicle's capacity and layout (GenerateSeats); relabelling one by hand could strand a ticket.
DROP TRIGGER IF EXISTS ProtectSeat;
DELIMITER $$
CREATE TRIGGER ProtectSeat BEFORE UPDATE ON Seat FOR EACH ROW
BEGIN
    IF NEW.VehicleID <> OLD.VehicleID OR NEW.SeatNo <> OLD.SeatNo THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Seat labels follow the vehicle''s capacity and layout; change the vehicle instead.';
    END IF;
END$$
DELIMITER ;

-- A route that has departures keeps its type: its vehicles were checked against it (CheckVehicleRouteType).
DROP TRIGGER IF EXISTS CheckRouteChange;
DELIMITER $$
CREATE TRIGGER CheckRouteChange BEFORE UPDATE ON Route FOR EACH ROW
BEGIN
    IF NEW.Type <> OLD.Type AND EXISTS (SELECT 1 FROM Schedule WHERE RouteID = OLD.RouteID) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: This route has departures; it cannot change type.';
    END IF;
END$$
DELIMITER ;

-- A public holiday cannot be declared on a day for which tickets are sold on departures that do not run on holidays:
-- those passengers would hold tickets for trips that no longer run.
DROP TRIGGER IF EXISTS CheckHoliday;
DELIMITER $$
CREATE TRIGGER CheckHoliday BEFORE INSERT ON Holiday FOR EACH ROW
BEGIN
    IF EXISTS (SELECT 1 FROM Ticket T JOIN Schedule S ON S.ScheduleID = T.ScheduleID
               WHERE T.TravelDate = NEW.HolidayDate AND NOT S.RunsOnHolidays) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Tickets are sold that day on departures that do not run on holidays.';
    END IF;
END$$
DELIMITER ;

DROP TRIGGER IF EXISTS CheckHolidayChange;
DELIMITER $$
CREATE TRIGGER CheckHolidayChange BEFORE UPDATE ON Holiday FOR EACH ROW
BEGIN
    IF NEW.HolidayDate <> OLD.HolidayDate
       AND EXISTS (SELECT 1 FROM Ticket T JOIN Schedule S ON S.ScheduleID = T.ScheduleID
                   WHERE T.TravelDate = NEW.HolidayDate AND NOT S.RunsOnHolidays) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Tickets are sold that day on departures that do not run on holidays.';
    END IF;
END$$
DELIMITER ;

-- A service day is added or removed (GuardServiceDay checks a removal), never turned into another day.
DROP TRIGGER IF EXISTS ProtectServiceDay;
DELIMITER $$
CREATE TRIGGER ProtectServiceDay BEFORE UPDATE ON ScheduleDay FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Add or remove a service day instead of changing it.';
END$$
DELIMITER ;

-- Only the reason of a swap may be corrected; another vehicle or date is a new swap (CheckVehicleSwap checks it).
DROP TRIGGER IF EXISTS ProtectSwap;
DELIMITER $$
CREATE TRIGGER ProtectSwap BEFORE UPDATE ON VehicleSwap FOR EACH ROW
BEGIN
    IF NEW.ScheduleID <> OLD.ScheduleID OR NEW.TravelDate <> OLD.TravelDate OR NEW.VehicleID <> OLD.VehicleID THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: Remove the swap and add a new one to change its vehicle or date.';
    END IF;
END$$
DELIMITER ;

-- The payment ledger and the audit trail are append-only, for every account including root: a mistake is corrected
-- by a new entry (a refund), never by changing history. Old audit quarters can still be archived with
-- ALTER TABLE ... DROP PARTITION, which removes whole partitions without firing row triggers.
DROP TRIGGER IF EXISTS ProtectPaymentUpdate;
DELIMITER $$
CREATE TRIGGER ProtectPaymentUpdate BEFORE UPDATE ON Payment FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: The payment ledger is append-only.';
END$$
DELIMITER ;

DROP TRIGGER IF EXISTS ProtectPaymentDelete;
DELIMITER $$
CREATE TRIGGER ProtectPaymentDelete BEFORE DELETE ON Payment FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: The payment ledger is append-only.';
END$$
DELIMITER ;

DROP TRIGGER IF EXISTS ProtectAuditUpdate;
DELIMITER $$
CREATE TRIGGER ProtectAuditUpdate BEFORE UPDATE ON TicketAudit FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: The audit trail is append-only.';
END$$
DELIMITER ;

DROP TRIGGER IF EXISTS ProtectAuditDelete;
DELIMITER $$
CREATE TRIGGER ProtectAuditDelete BEFORE DELETE ON TicketAudit FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Error: The audit trail is append-only.';
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
-- rights earlier versions gave and this one does not (no-ops on a fresh install)
REVOKE IF EXISTS INSERT, UPDATE, DELETE ON public_transport.Ticket FROM 'routewise_app'@'localhost' IGNORE UNKNOWN USER;
REVOKE IF EXISTS UPDATE ON public_transport.Schedule FROM 'routewise_app'@'localhost' IGNORE UNKNOWN USER;
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Passenger TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Driver    TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Route     TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON public_transport.Vehicle   TO 'routewise_app'@'localhost';
GRANT SELECT, INSERT, DELETE ON public_transport.Schedule          TO 'routewise_app'@'localhost';
GRANT SELECT ON public_transport.Ticket                            TO 'routewise_app'@'localhost';   -- written only by the procedures
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

-- record the new version
INSERT IGNORE INTO SchemaVersion (Version, Name) VALUES
(1, 'baseline: bookings, audit trail, roles, least-privilege user'),
(2, 'travel dates, fare rules, type check, audit actor, schema versioning'),
(3, 'seat map, service days and holidays, full-text search, stored sign-in limit'),
(4, 'seat holds, partitioned audit trail, departure board, invoker-rights maintenance routines'),
(5, 'DATETIME partitions, ScheduleDay table, one-day vehicle swaps, payment ledger'),
(6, 'update rules: immutable tickets, checked schedule and vehicle changes, append-only ledger and audit'),
(7, 'tickets only through the procedures, route type and holiday checks, no vehicle retired under booked passengers');
