"""Routes, schedules (with service days), public holidays and fare rules."""
import math

from flask import flash, redirect, render_template, request, url_for

from .. import app
from ..db import execute, query, query_proc
from ..search import VEHICLE, boolean_query, like, prefix
from ..web import WEEK, date_arg, opt, page_info, run, val


def schedule_filter(q):
    if not q:
        return 'ScheduleOccupancy O', '', []
    if q.isdigit():
        return 'ScheduleOccupancy O', 'WHERE O.ScheduleID = %s', [int(q)]
    if VEHICLE.fullmatch(q):
        return 'ScheduleOccupancy O', 'WHERE O.VehicleNo LIKE %s', [prefix(q.upper())]
    terms = boolean_query(q)
    if not terms:
        return 'ScheduleOccupancy O', 'WHERE O.RouteName LIKE %s', [like(q)]
    hits = """(SELECT S.ScheduleID FROM Schedule S JOIN Route R ON R.RouteID = S.RouteID
               WHERE MATCH(R.RouteName, R.Origin, R.Destination) AGAINST (%s IN BOOLEAN MODE)) AS hit"""
    return f'ScheduleOccupancy O JOIN {hits} ON hit.ScheduleID = O.ScheduleID', '', [terms]


@app.route('/routes')
def routes():
    q = request.args.get('q', '').strip()
    source, where, args = schedule_filter(q)
    total = query(f'SELECT COUNT(*) AS n FROM {source} {where}', args)[0]['n']
    pg = page_info(total, 40)
    return render_template(
        'routes.html', pg=pg, q=q, week=WEEK,
        routes=query("SELECT * FROM RouteRevenue R JOIN Route USING (RouteID) ORDER BY RouteID"),
        schedules=query(f'SELECT O.* FROM {source} {where} ORDER BY O.ScheduleID LIMIT %s OFFSET %s', args + [pg['per_page'], pg['offset']]),
        vehicles=query("SELECT VehicleID, VehicleNo, Type FROM ActiveVehicle ORDER BY VehicleNo"),
        types=query("SELECT RouteType FROM FareRule ORDER BY RouteType"),
        fare_rules=query("SELECT * FROM FareRule ORDER BY RouteType"),
        discounts=query("SELECT * FROM CardDiscount ORDER BY DiscountPct"),
        holidays=query("SELECT * FROM Holiday WHERE HolidayDate >= CURDATE() - INTERVAL 30 DAY ORDER BY HolidayDate"),
        swaps=query("""SELECT VS.ScheduleID, VS.TravelDate, VS.Reason, R.RouteName, S.DepartureTime,
                              V0.VehicleNo AS Scheduled, V1.VehicleNo AS Replacement
                       FROM VehicleSwap VS JOIN Schedule S ON S.ScheduleID = VS.ScheduleID JOIN Route R ON R.RouteID = S.RouteID
                       JOIN Vehicle V0 ON V0.VehicleID = S.VehicleID JOIN Vehicle V1 ON V1.VehicleID = VS.VehicleID
                       WHERE VS.TravelDate >= CURDATE() ORDER BY VS.TravelDate, S.DepartureTime"""),
        all_schedules=query("""SELECT S.ScheduleID, R.RouteName, S.DepartureTime, V.VehicleNo FROM Schedule S
                               JOIN Route R ON R.RouteID = S.RouteID JOIN Vehicle V ON V.VehicleID = S.VehicleID ORDER BY S.ScheduleID"""),
        route_list=query("SELECT RouteID, RouteName FROM Route ORDER BY RouteName"))


@app.route('/routes/add', methods=['POST'])
def add_route():
    return run(lambda: execute(
        "INSERT INTO Route (RouteName, Origin, Destination, Distance, Type) VALUES (%s,%s,%s,%s,%s)",
        [val('route_name'), val('origin'), val('destination'), opt('distance'), val('type') or 'Bus']),
        'Route added.', 'routes')


@app.route('/routes/<int:rid>/delete', methods=['POST'])
def delete_route(rid):
    return run(lambda: execute("DELETE FROM Route WHERE RouteID=%s", (rid,)), 'Route deleted.', 'routes')


@app.route('/schedules/add', methods=['POST'])
def add_schedule():
    chosen = [d for d in request.form.getlist('days') if d in WEEK]
    days = ','.join(chosen) if chosen else ','.join(WEEK)             # nothing ticked = every day
    # the form ticks "runs on public holidays" by default; a request without any day choices means a normal daily service
    holidays = 'runs_on_holidays' in request.form or not request.form.getlist('days')
    # AddSchedule inserts the departure and its ScheduleDay rows in one transaction
    return run(lambda: query_proc('AddSchedule', [val('route_id'), val('vehicle_id'), val('departure'), val('arrival'),
                                                  opt('frequency'), days, holidays]),
               'Schedule added.', 'routes')


@app.route('/swaps/add', methods=['POST'])
def add_swap():
    """Admin: another vehicle runs one trip (a departure on one date); the CheckVehicleSwap trigger decides if it can."""
    day = date_arg(val('travel_date'))
    if not (val('s_id').isdigit() and val('vehicle_id').isdigit() and day and val('reason')):
        flash('Choose a departure, a date, a replacement vehicle and give a reason.', 'danger')
        return redirect(url_for('routes'))
    return run(lambda: execute("INSERT INTO VehicleSwap (ScheduleID, TravelDate, VehicleID, Reason) VALUES (%s, %s, %s, %s)",
                               (val('s_id'), day, val('vehicle_id'), val('reason'))),
               f'Departure #{val("s_id")} uses another vehicle on {day:%d %b %Y}.', 'routes')


@app.route('/swaps/<int:sid>/<day>/delete', methods=['POST'])
def delete_swap(sid, day):
    parsed = date_arg(day)
    if not parsed:
        flash('Unknown vehicle swap.', 'danger')
        return redirect(url_for('routes'))
    return run(lambda: execute("DELETE FROM VehicleSwap WHERE ScheduleID = %s AND TravelDate = %s", (sid, parsed)),
               'Vehicle swap removed; the scheduled vehicle runs that trip again.', 'routes')


@app.route('/schedules/<int:sid>/delete', methods=['POST'])
def delete_schedule(sid):
    return run(lambda: execute("DELETE FROM Schedule WHERE ScheduleID=%s", (sid,)), 'Schedule deleted.', 'routes')


@app.route('/holidays/add', methods=['POST'])
def add_holiday():
    day = date_arg(val('holiday_date'))
    if not day or not val('name'):
        flash('A valid date and a name are required.', 'danger')
        return redirect(url_for('routes'))
    return run(lambda: execute("INSERT INTO Holiday (HolidayDate, Name) VALUES (%s, %s)", (day, val('name'))),
               f'{val("name")} ({day:%d %b %Y}) added as a public holiday.', 'routes')


@app.route('/holidays/<day>/delete', methods=['POST'])
def delete_holiday(day):
    parsed = date_arg(day)
    if not parsed:
        flash('Unknown holiday.', 'danger')
        return redirect(url_for('routes'))
    return run(lambda: execute("DELETE FROM Holiday WHERE HolidayDate = %s", (parsed,)), 'Holiday removed.', 'routes')


@app.route('/fares/update', methods=['POST'])
def update_fare_rule():
    """Admin: change a fare rate / minimum fare / card discount. CalcFare reads these tables, so prices change at once."""
    kind, key = val('kind'), val('key')
    try:
        if kind == 'route':
            rate, minimum = float(val('rate')), float(val('min_fare'))
            if not (math.isfinite(rate) and math.isfinite(minimum)) or rate <= 0 or minimum <= 0 or rate > 999 or minimum > 999:
                raise ValueError
            return run(lambda: execute('UPDATE FareRule SET RatePerKm = %s, MinFare = %s WHERE RouteType = %s', (rate, minimum, key)),
                       f'Fare rule for {key} updated.', 'routes')
        if kind == 'card':
            pct = int(val('pct'))
            if not 0 <= pct <= 90:
                raise ValueError
            return run(lambda: execute('UPDATE CardDiscount SET DiscountPct = %s WHERE CardType = %s', (pct, key)),
                       f'Discount for {key} cards updated.', 'routes')
    except ValueError:
        pass
    flash('Enter valid numbers (rates above 0, discounts from 0 to 90 %).', 'danger')
    return redirect(url_for('routes'))
