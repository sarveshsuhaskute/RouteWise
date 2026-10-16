"""Reports: analytical SQL (joins, GROUP BY/HAVING, subqueries, anti-join, window functions, CTEs, a cursor procedure).
Each report shows its own SQL on the page; queries.sql holds the same queries for MySQL Workbench."""
import time
from datetime import datetime

from flask import render_template, request

from .. import app
from ..db import db_config, query, query_proc

REPORTS = [
    {'title': 'Revenue by route',
     'desc': 'Aggregation over a view: tickets sold and revenue per route (GROUP BY + SUM).',
     'sql': "SELECT RouteName, Type, TicketsSold, Revenue\nFROM RouteRevenue\nORDER BY Revenue DESC\nLIMIT 10;"},
    {'title': 'Top passengers by spend',
     'desc': 'GROUP BY with HAVING: passengers with at least 2 trips, ranked by total fare.',
     'sql': ("SELECT P.Name, P.CardType, COUNT(*) AS Trips, SUM(T.Fare) AS TotalSpent\n"
             "FROM Passenger P JOIN Ticket T ON T.PassengerID = P.PassengerID\n"
             "GROUP BY P.PassengerID, P.Name, P.CardType\n"
             "HAVING COUNT(*) >= 2\n"
             "ORDER BY TotalSpent DESC\n"
             "LIMIT 10;")},
    {'title': 'Busier than average trips',
     'desc': 'Subquery: trips (a schedule on a travel date) whose booked seats exceed the average over all trips.',
     'sql': ("SELECT ScheduleID, RouteName, VehicleNo, TravelDate, Booked, OccupancyPct\n"
             "FROM TripOccupancy\n"
             "WHERE Booked > (SELECT AVG(Booked) FROM TripOccupancy)\n"
             "ORDER BY Booked DESC\n"
             "LIMIT 10;")},
    {'title': 'Average rating per route',
     'desc': 'View over a LEFT JOIN of routes and feedback: average rating and number of reviews.',
     'sql': "SELECT RouteName, Reviews, AvgRating\nFROM RouteRatings\nORDER BY AvgRating DESC\nLIMIT 10;"},
    {'title': 'Passengers who never booked',
     'desc': 'LEFT JOIN ... IS NULL (anti-join): registered passengers without any ticket.',
     'sql': ("SELECT P.PassengerID, P.Name, P.Email, P.CardType\n"
             "FROM Passenger P LEFT JOIN Ticket T ON T.PassengerID = P.PassengerID\n"
             "WHERE T.TicketID IS NULL\n"
             "LIMIT 10;")},
    {'title': 'Driver workload',
     'desc': 'Three-table LEFT JOIN: vehicle and number of scheduled runs per driver.',
     'sql': ("SELECT D.Name AS Driver, D.ShiftHours, V.VehicleNo, COUNT(S.ScheduleID) AS ScheduledRuns\n"
             "FROM Driver D\n"
             "LEFT JOIN Vehicle  V ON V.DriverID  = D.DriverID\n"
             "LEFT JOIN Schedule S ON S.VehicleID = V.VehicleID\n"
             "GROUP BY D.DriverID, D.Name, D.ShiftHours, V.VehicleNo\n"
             "ORDER BY ScheduledRuns DESC\n"
             "LIMIT 10;")},
    {'title': 'Tickets by travel-card type',
     'desc': 'Which pass types generate the most tickets and revenue.',
     'sql': ("SELECT P.CardType, COUNT(T.TicketID) AS Tickets, COALESCE(SUM(T.Fare), 0) AS Revenue\n"
             "FROM Passenger P LEFT JOIN Ticket T ON T.PassengerID = P.PassengerID\n"
             "GROUP BY P.CardType\n"
             "ORDER BY Tickets DESC;")},
    {'title': 'Running revenue per route',
     'desc': 'Window function: SUM() OVER (PARTITION BY route ORDER BY ticket) gives a running total inside each route.',
     'sql': ("SELECT R.RouteName, T.TicketID, T.Fare,\n"
             "       SUM(T.Fare) OVER (PARTITION BY R.RouteID ORDER BY T.TicketID) AS RunningRevenue\n"
             "FROM Ticket T\n"
             "JOIN Schedule S ON S.ScheduleID = T.ScheduleID\n"
             "JOIN Route R    ON R.RouteID    = S.RouteID\n"
             "ORDER BY R.RouteName, T.TicketID\nLIMIT 12;")},
    {'title': 'Passenger rank within each travel-card type',
     'desc': 'CTE plus RANK() OVER (PARTITION BY card type ORDER BY spend): the top three spenders of each card type.',
     'sql': ("WITH spend AS (\n"
             "    SELECT P.PassengerID, P.Name, P.CardType, COALESCE(SUM(T.Fare), 0) AS TotalSpent\n"
             "    FROM Passenger P LEFT JOIN Ticket T ON T.PassengerID = P.PassengerID\n"
             "    GROUP BY P.PassengerID, P.Name, P.CardType),\n"
             "ranked AS (\n"
             "    SELECT CardType, Name, TotalSpent,\n"
             "           RANK() OVER (PARTITION BY CardType ORDER BY TotalSpent DESC) AS RankInCard\n"
             "    FROM spend)\n"
             "SELECT CardType, Name, TotalSpent, RankInCard\n"
             "FROM ranked\n"
             "WHERE RankInCard <= 3\n"
             "ORDER BY CardType, RankInCard, Name;")},
    {'title': 'Departures per hour of the day',
     'desc': 'Recursive CTE generates the hours 05 to 20, and a LEFT JOIN counts departures, so empty hours appear as 0.',
     'sql': ("WITH RECURSIVE hours (h) AS (\n"
             "    SELECT 5 UNION ALL SELECT h + 1 FROM hours WHERE h < 20)\n"
             "SELECT CONCAT(LPAD(h, 2, '0'), ':00') AS HourOfDay, COUNT(S.ScheduleID) AS Departures\n"
             "FROM hours LEFT JOIN Schedule S ON HOUR(S.DepartureTime) = h\n"
             "GROUP BY h\nORDER BY h;")},
    {'title': 'Route scorecard (stored procedure with a cursor)',
     'desc': 'RouteScorecard() walks the routes one by one with a CURSOR, scores each and returns a graded table.',
     'proc': 'RouteScorecard', 'sql': 'CALL RouteScorecard();'},
    {'title': 'Audit trail (latest 10 events)',
     'desc': 'Rows written automatically by the AuditBooking / AuditCancellation triggers.',
     'sql': ("SELECT A.AuditID, A.Action, A.TicketID, P.Name AS Passenger, A.SeatNo, A.Fare, A.Actor, A.ActionTime\n"
             "FROM TicketAudit A JOIN Passenger P ON P.PassengerID = A.PassengerID\n"
             "ORDER BY A.AuditID DESC\nLIMIT 10;")},
    {'title': 'Window, aisle or middle seat?',
     'desc': 'Join on a composite key (vehicle + seat label) into the seat map: how full each seat position is across all trips.',
     'sql': ("SELECT ST.Position, COUNT(*) AS Tickets,\n"
             "       ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS SharePct\n"
             "FROM Ticket T\n"
             "JOIN Schedule S ON S.ScheduleID = T.ScheduleID\n"
             "LEFT JOIN VehicleSwap VS ON VS.ScheduleID = T.ScheduleID AND VS.TravelDate = T.TravelDate\n"
             "JOIN Seat ST ON ST.VehicleID = COALESCE(VS.VehicleID, S.VehicleID) AND ST.SeatNo = T.SeatNo\n"
             "GROUP BY ST.Position\n"
             "ORDER BY Tickets DESC;")},
    {'title': 'Takings by payment method (cash-up)',
     'desc': 'GROUP BY ... WITH ROLLUP over the payment ledger of the last 30 days: subtotals per method and a grand total; GROUPING() labels the summary rows.',
     'sql': ("SELECT IF(GROUPING(Method), 'All methods', Method) AS Method,\n"
             "       IF(GROUPING(Kind), 'Net', Kind) AS Kind,\n"
             "       COUNT(*) AS Entries, SUM(Amount) AS Amount\n"
             "FROM Payment\n"
             "WHERE PaidAt >= CURDATE() - INTERVAL 30 DAY\n"
             "GROUP BY Method, Kind WITH ROLLUP;")},
]

_report_cache = {'at': 0.0, 'db': None, 'results': None}


def run_reports(force=False):
    """Run all reports, or reuse the results of the last run when they are younger than REPORT_CACHE_SECONDS."""
    now = time.time()
    fresh = (_report_cache['results'] is not None and _report_cache['db'] == db_config['database']
             and now - _report_cache['at'] < app.config['REPORT_CACHE_SECONDS'])
    if fresh and not force:
        return _report_cache['results'], _report_cache['at']
    results = []
    for r in REPORTS:
        # reports only read, so they can run on a read replica when one is configured (DB_REPLICA_HOST)
        rows = query_proc(r['proc'], replica=True)[:10] if r.get('proc') else query(r['sql'], replica=True)
        results.append({**r, 'rows': rows, 'cols': list(rows[0].keys()) if rows else []})
    _report_cache.update(at=now, db=db_config['database'], results=results)
    return results, now


@app.route('/reports')
def reports():
    seconds = app.config['REPORT_CACHE_SECONDS']
    results, generated = run_reports(force=request.args.get('refresh') == '1' or seconds <= 0)
    return render_template('reports.html', reports=results, generated=datetime.fromtimestamp(generated), cache_seconds=seconds)
