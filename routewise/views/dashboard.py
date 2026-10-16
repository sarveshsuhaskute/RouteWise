"""Dashboard: key figures, revenue chart, busiest trips, today's service and recent bookings."""
from flask import render_template

from .. import app
from ..db import query


@app.route('/')
def index():
    stats = query("""
        SELECT (SELECT COUNT(*) FROM Ticket)                              AS tickets,
               (SELECT COALESCE(SUM(Fare), 0) FROM Ticket)                AS revenue,
               (SELECT COUNT(*) FROM Passenger)                           AS passengers,
               (SELECT COUNT(*) FROM Vehicle WHERE Status = 'Active')     AS vehicles,
               (SELECT COUNT(*) FROM Vehicle)                             AS total_vehicles,
               (SELECT ROUND(AVG(Rating), 1) FROM Feedback)               AS rating,
               (SELECT COUNT(*) FROM Schedule WHERE RunsOn(ScheduleID, CURDATE())) AS running_today,
               (SELECT COUNT(*) FROM Schedule)                            AS departures,
               (SELECT Name FROM Holiday WHERE HolidayDate = CURDATE())   AS holiday_today
    """)[0]
    revenue = query("SELECT RouteName, Revenue, TicketsSold FROM RouteRevenue ORDER BY Revenue DESC LIMIT 8")
    occupancy = query("""SELECT ScheduleID, RouteName, DepartureTime, TravelDate, OccupancyPct, SeatsLeft
                         FROM TripOccupancy ORDER BY OccupancyPct DESC, TravelDate DESC LIMIT 6""")
    recent = query("SELECT * FROM BookingSummary ORDER BY TicketID DESC LIMIT 6")
    departures = query("""SELECT * FROM DepartureBoard WHERE Status IN ('Boarding', 'En route', 'Scheduled')
                          ORDER BY FIELD(Status, 'En route', 'Boarding', 'Scheduled'), DepartureTime LIMIT 6""")
    next_holiday = query("SELECT HolidayDate, Name FROM Holiday WHERE HolidayDate >= CURDATE() ORDER BY HolidayDate LIMIT 1")
    return render_template(
        'dashboard.html', stats=stats, recent=recent, occupancy=occupancy, departures=departures,
        next_holiday=next_holiday[0] if next_holiday else None,
        chart_revenue={'labels': [r['RouteName'] for r in revenue],
                       'values': [float(r['Revenue']) for r in revenue]})
