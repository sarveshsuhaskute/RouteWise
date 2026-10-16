-- RouteWise: the analytical queries shown on the Reports page
USE public_transport;

-- 1. Revenue by route
-- Aggregation over a view: tickets sold and revenue per route (GROUP BY + SUM).
SELECT RouteName, Type, TicketsSold, Revenue
FROM RouteRevenue
ORDER BY Revenue DESC
LIMIT 10;

-- 2. Top passengers by spend
-- GROUP BY with HAVING: passengers with at least 2 trips, ranked by total fare.
SELECT P.Name, P.CardType, COUNT(*) AS Trips, SUM(T.Fare) AS TotalSpent
FROM Passenger P JOIN Ticket T ON T.PassengerID = P.PassengerID
GROUP BY P.PassengerID, P.Name, P.CardType
HAVING COUNT(*) >= 2
ORDER BY TotalSpent DESC
LIMIT 10;

-- 3. Busier than average trips
-- Subquery: trips (a schedule on a travel date) whose booked seats exceed the average over all trips.
SELECT ScheduleID, RouteName, VehicleNo, TravelDate, Booked, OccupancyPct
FROM TripOccupancy
WHERE Booked > (SELECT AVG(Booked) FROM TripOccupancy)
ORDER BY Booked DESC
LIMIT 10;

-- 4. Average rating per route
-- View over a LEFT JOIN of routes and feedback: average rating and number of reviews.
SELECT RouteName, Reviews, AvgRating
FROM RouteRatings
ORDER BY AvgRating DESC
LIMIT 10;

-- 5. Passengers who never booked
-- LEFT JOIN ... IS NULL (anti-join): registered passengers without any ticket.
SELECT P.PassengerID, P.Name, P.Email, P.CardType
FROM Passenger P LEFT JOIN Ticket T ON T.PassengerID = P.PassengerID
WHERE T.TicketID IS NULL
LIMIT 10;

-- 6. Driver workload
-- Three-table LEFT JOIN: vehicle and number of scheduled runs per driver.
SELECT D.Name AS Driver, D.ShiftHours, V.VehicleNo, COUNT(S.ScheduleID) AS ScheduledRuns
FROM Driver D
LEFT JOIN Vehicle  V ON V.DriverID  = D.DriverID
LEFT JOIN Schedule S ON S.VehicleID = V.VehicleID
GROUP BY D.DriverID, D.Name, D.ShiftHours, V.VehicleNo
ORDER BY ScheduledRuns DESC
LIMIT 10;

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
-- CTE plus RANK() OVER (PARTITION BY card type ORDER BY spend): the top three spenders of each card type.
WITH spend AS (
    SELECT P.PassengerID, P.Name, P.CardType, COALESCE(SUM(T.Fare), 0) AS TotalSpent
    FROM Passenger P LEFT JOIN Ticket T ON T.PassengerID = P.PassengerID
    GROUP BY P.PassengerID, P.Name, P.CardType),
ranked AS (
    SELECT CardType, Name, TotalSpent,
           RANK() OVER (PARTITION BY CardType ORDER BY TotalSpent DESC) AS RankInCard
    FROM spend)
SELECT CardType, Name, TotalSpent, RankInCard
FROM ranked
WHERE RankInCard <= 3
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
SELECT A.AuditID, A.Action, A.TicketID, P.Name AS Passenger, A.SeatNo, A.Fare, A.Actor, A.ActionTime
FROM TicketAudit A JOIN Passenger P ON P.PassengerID = A.PassengerID
ORDER BY A.AuditID DESC
LIMIT 10;

-- 13. Window, aisle or middle seat?
-- Join on a composite key (vehicle + seat label) into the seat map: how full each seat position is across all trips.
SELECT ST.Position, COUNT(*) AS Tickets,
       ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS SharePct
FROM Ticket T
JOIN Schedule S ON S.ScheduleID = T.ScheduleID
LEFT JOIN VehicleSwap VS ON VS.ScheduleID = T.ScheduleID AND VS.TravelDate = T.TravelDate
JOIN Seat ST ON ST.VehicleID = COALESCE(VS.VehicleID, S.VehicleID) AND ST.SeatNo = T.SeatNo
GROUP BY ST.Position
ORDER BY Tickets DESC;

-- 14. Takings by payment method (cash-up)
-- GROUP BY ... WITH ROLLUP over the payment ledger of the last 30 days: subtotals per method and a grand total; GROUPING() labels the summary rows.
SELECT IF(GROUPING(Method), 'All methods', Method) AS Method,
       IF(GROUPING(Kind), 'Net', Kind) AS Kind,
       COUNT(*) AS Entries, SUM(Amount) AS Amount
FROM Payment
WHERE PaidAt >= CURDATE() - INTERVAL 30 DAY
GROUP BY Method, Kind WITH ROLLUP;
