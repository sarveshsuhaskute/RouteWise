-- Sample analytical queries (the same ones shown on the app's Reports page)
USE public_transport;

-- 1. Revenue by route
-- Aggregation over a view: tickets sold and revenue per route (GROUP BY + SUM).
SELECT RouteName, Type, TicketsSold, Revenue
FROM RouteRevenue
ORDER BY Revenue DESC;

-- 2. Top passengers by spend
-- GROUP BY with HAVING: passengers with at least 2 trips, ranked by total fare.
SELECT P.Name, P.CardType, COUNT(*) AS Trips, SUM(T.Fare) AS TotalSpent
FROM Passenger P JOIN Ticket T ON T.PassengerID = P.PassengerID
GROUP BY P.PassengerID, P.Name, P.CardType
HAVING COUNT(*) >= 2
ORDER BY TotalSpent DESC;

-- 3. Busier than average schedules
-- Subquery: schedules whose booked seats exceed the average across all schedules.
SELECT ScheduleID, RouteName, VehicleNo, Booked, OccupancyPct
FROM ScheduleOccupancy
WHERE Booked > (SELECT AVG(Booked) FROM ScheduleOccupancy)
ORDER BY Booked DESC;

-- 4. Average rating per route
-- View over a LEFT JOIN of routes and feedback: average rating and number of reviews.
SELECT RouteName, Reviews, AvgRating
FROM RouteRatings
ORDER BY AvgRating DESC;

-- 5. Passengers who never booked
-- LEFT JOIN ... IS NULL (anti-join): registered passengers without any ticket.
SELECT P.PassengerID, P.Name, P.Email, P.CardType
FROM Passenger P LEFT JOIN Ticket T ON T.PassengerID = P.PassengerID
WHERE T.TicketID IS NULL;

-- 6. Driver workload
-- Three-table LEFT JOIN: vehicle and number of scheduled runs per driver.
SELECT D.Name AS Driver, D.ShiftHours, V.VehicleNo, COUNT(S.ScheduleID) AS ScheduledRuns
FROM Driver D
LEFT JOIN Vehicle  V ON V.DriverID  = D.DriverID
LEFT JOIN Schedule S ON S.VehicleID = V.VehicleID
GROUP BY D.DriverID, D.Name, D.ShiftHours, V.VehicleNo
ORDER BY ScheduledRuns DESC;

-- 7. Tickets by travel-card type
-- Which pass types generate the most tickets and revenue.
SELECT P.CardType, COUNT(T.TicketID) AS Tickets, COALESCE(SUM(T.Fare), 0) AS Revenue
FROM Passenger P LEFT JOIN Ticket T ON T.PassengerID = P.PassengerID
GROUP BY P.CardType
ORDER BY Tickets DESC;

-- 8. Running revenue per route
-- Window function: SUM() OVER (PARTITION BY route ORDER BY ticket) gives a running total inside each route.
SELECT R.RouteName, T.TicketID, T.Fare,
       SUM(T.Fare) OVER (PARTITION BY R.RouteID ORDER BY T.TicketID) AS RunningRevenue
FROM Ticket T
JOIN Schedule S ON S.ScheduleID = T.ScheduleID
JOIN Route R    ON R.RouteID    = S.RouteID
ORDER BY R.RouteName, T.TicketID
LIMIT 12;

-- 9. Passenger rank within each travel-card type
-- CTE plus RANK() OVER (PARTITION BY card type ORDER BY spend): the biggest spenders of each card type.
WITH spend AS (
    SELECT P.PassengerID, P.Name, P.CardType, COALESCE(SUM(T.Fare), 0) AS TotalSpent
    FROM Passenger P LEFT JOIN Ticket T ON T.PassengerID = P.PassengerID
    GROUP BY P.PassengerID, P.Name, P.CardType)
SELECT CardType, Name, TotalSpent,
       RANK() OVER (PARTITION BY CardType ORDER BY TotalSpent DESC) AS RankInCard
FROM spend
ORDER BY CardType, RankInCard, Name;

-- 10. Departures per hour of the day
-- Recursive CTE generates the hours 05 to 20, and a LEFT JOIN counts departures, so empty hours appear as 0.
WITH RECURSIVE hours (h) AS (
    SELECT 5 UNION ALL SELECT h + 1 FROM hours WHERE h < 20)
SELECT CONCAT(LPAD(h, 2, '0'), ':00') AS HourOfDay, COUNT(S.ScheduleID) AS Departures
FROM hours LEFT JOIN Schedule S ON HOUR(S.DepartureTime) = h
GROUP BY h
ORDER BY h;

-- 11. Route scorecard (stored procedure with a cursor)
-- RouteScorecard() walks the routes one by one with a CURSOR, scores each and returns a graded table.
CALL RouteScorecard();

-- 12. Audit trail (latest 10 events)
-- Rows written automatically by the AuditBooking / AuditCancellation triggers.
SELECT A.AuditID, A.Action, A.TicketID, P.Name AS Passenger, A.SeatNo, A.Fare, A.ActionTime
FROM TicketAudit A JOIN Passenger P ON P.PassengerID = A.PassengerID
ORDER BY A.AuditID DESC
LIMIT 10;

-- 13. Stored routines, transactions and the updatable view
SELECT ScheduleID, RouteName, SeatsLeft(ScheduleID) AS SeatsLeft FROM Schedule JOIN Route USING (RouteID);
SELECT CalcFare(1, 1) AS FareForPassenger1OnSchedule1;
-- CALL BookTicket(8, 1, 'F1', NULL);        -- books a seat; NULL fare = CalcFare; triggers validate it
-- CALL TransferTicket(1, 2, 'Z9');          -- moves a ticket as ONE transaction (rolls back on any error)
-- CALL CancelTicket(1);                     -- cancels a ticket (audit trigger logs it)
SELECT VehicleNo, Status FROM ActiveVehicle; -- view WITH CHECK OPTION: an UPDATE that sets Status to 'Retired' is rejected
