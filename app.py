import csv
import io
import math
import os
import re
import secrets
import time as _time
from datetime import date, datetime, timedelta, time
from decimal import Decimal

import mysql.connector
from flask import (Flask, render_template, request, redirect, url_for, flash,
                   session, abort, jsonify, Response)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
# Set SECRET_KEY to keep logins valid across restarts; otherwise a random key is generated per run.
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')

# --- 1. Database Configuration ---------------------------------------------
db_config = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    # The app connects as a least-privilege user created by schema.sql (never as root). Override for your setup.
    'user': os.environ.get('DB_USER', 'routewise_app'),
    'password': os.environ.get('DB_PASSWORD', 'routewise_app_pw'),
    'database': os.environ.get('DB_NAME', 'public_transport'),
}


# --- 2. Small database helpers ---------------------------------------------
def query(sql, args=()):
    """Run a SELECT and return all rows as dicts."""
    conn = mysql.connector.connect(**db_config)
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, args)
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()


def execute(sql, args=()):
    """Run an INSERT/UPDATE/DELETE and commit."""
    conn = mysql.connector.connect(**db_config)
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        conn.commit()
        cur.close()
    finally:
        conn.close()


def call(proc, args, retries=3):
    """Call a stored procedure and commit. Retries when MySQL reports a deadlock (1213) or lock timeout (1205)."""
    for attempt in range(retries):
        conn = mysql.connector.connect(**db_config)
        try:
            cur = conn.cursor()
            cur.callproc(proc, args)
            conn.commit()
            cur.close()
            return
        except mysql.connector.Error as err:
            if err.errno in (1213, 1205) and attempt < retries - 1:
                _time.sleep(0.05 * (attempt + 1))
                continue
            raise
        finally:
            conn.close()


def query_proc(proc, args=()):
    """Call a stored procedure that returns a result set (for example RouteScorecard) and return its rows."""
    conn = mysql.connector.connect(**db_config)
    try:
        cur = conn.cursor(dictionary=True)
        cur.callproc(proc, args)
        rows = []
        for result in cur.stored_results():
            rows = result.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()


def run(action, ok_message, endpoint):
    """Run a write action, flash the outcome (trigger/constraint errors included), redirect."""
    try:
        action()
        flash(ok_message, 'success')
    except mysql.connector.Error as err:
        flash(friendly_error(err), 'danger')
    return redirect(url_for(endpoint))


def friendly_error(err):
    msg = err.msg or str(err)
    if err.errno == 1451:
        return 'Cannot delete: other records still depend on this one.'
    if err.errno == 1062:
        return 'Duplicate value: that email / vehicle number / licence already exists.'
    if err.errno == 3819:
        return 'A value violates a database rule (check ratings, capacity, times, fare...).'
    if err.errno == 1406:
        return 'A value is too long for its field (seat numbers: at most 10 characters).'
    if err.errno in (1264, 1366, 1292):
        return 'A value is out of range or not valid for its field (for example a fare above 9999.99).'
    if err.errno in (1213, 1205):
        return 'The system is busy with another booking for this departure. Please try again.'
    if err.errno == 1048:
        return 'A required value is missing (for example a fare could not be calculated).'
    if err.errno == 1452:
        return 'Invalid reference: the selected passenger / schedule / route does not exist.'
    return msg


def val(name):
    return request.form.get(name, '').strip()


def like(q):
    return f'%{q}%'


def page_info(total, per_page=50):
    """Current page number, page count, row offset and a compact list of page numbers (None = gap) for the pager."""
    raw = request.args.get('page', '1')
    page = int(raw) if raw.isdigit() and int(raw) > 0 else 1
    pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, pages)
    numbers = sorted({1, pages, *range(max(page - 2, 1), min(page + 2, pages) + 1)})
    window, prev = [], 0
    for n in numbers:
        if n - prev > 1:
            window.append(None)
        window.append(n)
        prev = n
    return {'page': page, 'pages': pages, 'total': total, 'per_page': per_page, 'offset': (page - 1) * per_page, 'window': window}


def opt(name):
    """Optional form field: empty string -> NULL."""
    return val(name) or None



# --- 2b. Authentication, roles and CSRF protection -------------------------
# viewer: read-only | staff: bookings, passengers, feedback | admin: everything (fleet, routes, users)
ROLES = {'viewer': 0, 'staff': 1, 'admin': 2}
STAFF_ENDPOINTS = {'book_ticket', 'cancel_ticket', 'transfer_ticket', 'add_passenger', 'edit_passenger', 'add_feedback'}
ANY_USER_POSTS = {'logout'}      # POSTs every signed-in user may make, whatever the role
ADMIN_PAGES = {'users'}
PUBLIC_ENDPOINTS = {'login', 'static'}
DEFAULT_USERS = [  # demo accounts created on first start when the AppUser table is empty
    ('admin', 'Administrator', 'admin123', 'admin'),
    ('staff', 'Booking Clerk', 'staff123', 'staff'),
    ('viewer', 'Guest Viewer', 'viewer123', 'viewer'),
]


def ensure_default_users():
    if query("SELECT COUNT(*) AS n FROM AppUser")[0]['n'] == 0:
        for username, full, pwd, role in DEFAULT_USERS:
            execute("INSERT INTO AppUser (Username, FullName, PasswordHash, Role) VALUES (%s,%s,%s,%s)",
                    (username, full, generate_password_hash(pwd), role))


def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_hex(16)
    return session['csrf']


app.jinja_env.globals['csrf_token'] = csrf_token


@app.context_processor
def inject_user():
    user = session.get('user')
    return {'current_user': user, 'role': user['role'] if user else None}


def back():
    ref = request.referrer or ''
    return redirect(ref if ref.startswith(request.host_url) else url_for('index'))


@app.before_request
def guard():
    if request.endpoint is None:
        return
    if request.method == 'POST' and request.endpoint != 'static':
        if not secrets.compare_digest(request.form.get('csrf_token', ''), session.get('csrf', '')):
            abort(400, 'csrf')
    if request.endpoint in PUBLIC_ENDPOINTS:
        return
    user = session.get('user')
    if not user:
        return redirect(url_for('login', next=request.full_path.rstrip('?')))
    if request.endpoint in ANY_USER_POSTS:
        need = 'viewer'
    elif request.method == 'POST':
        need = 'staff' if request.endpoint in STAFF_ENDPOINTS else 'admin'
    else:
        need = 'admin' if request.endpoint in ADMIN_PAGES else 'viewer'
    if ROLES[user['role']] < ROLES[need]:
        flash(f'Your role ({user["role"]}) is not allowed to do that.', 'danger')
        return back()


@app.route('/login', methods=['GET', 'POST'])
def login():
    ensure_default_users()
    if request.method == 'GET' and session.get('user'):
        return redirect(url_for('index'))
    if request.method == 'POST':
        rows = query("SELECT * FROM AppUser WHERE Username = %s", (val('username').lower(),))
        if rows and check_password_hash(rows[0]['PasswordHash'], request.form.get('password', '')):
            token = csrf_token()
            session.clear()
            session['csrf'] = token
            session['user'] = {'id': rows[0]['UserID'], 'name': rows[0]['FullName'],
                               'username': rows[0]['Username'], 'role': rows[0]['Role']}
            nxt = request.args.get('next', '')
            return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else url_for('index'))
        flash('Invalid username or password.', 'danger')
    return render_template('login.html')


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/users')
def users():
    return render_template('users.html', users=query("SELECT * FROM AppUser ORDER BY UserID"))


@app.route('/users/add', methods=['POST'])
def add_user():
    if len(request.form.get('password', '')) < 6 or not val('username') or val('role') not in ROLES:
        flash('Username, a valid role and a password of at least 6 characters are required.', 'danger')
        return redirect(url_for('users'))
    return run(lambda: execute(
        "INSERT INTO AppUser (Username, FullName, PasswordHash, Role) VALUES (%s,%s,%s,%s)",
        [val('username').lower(), val('full_name') or val('username'),
         generate_password_hash(request.form['password']), val('role')]),
        f'User {val("username").lower()} created.', 'users')


@app.route('/users/<int:uid>/delete', methods=['POST'])
def delete_user(uid):
    if uid == session['user']['id']:
        flash('You cannot delete your own account.', 'danger')
        return redirect(url_for('users'))
    return run(lambda: execute("DELETE FROM AppUser WHERE UserID=%s", (uid,)), 'User deleted.', 'users')


# --- 3. Template filters ----------------------------------------------------
@app.template_filter('fmt')
def fmt(v):
    if v is None or v == '':
        return '—'
    if isinstance(v, timedelta):
        mins = int(v.total_seconds() // 60)
        return f'{mins // 60:02d}:{mins % 60:02d}'
    if isinstance(v, time):
        return v.strftime('%H:%M')
    if isinstance(v, datetime):
        return v.strftime('%d %b %Y, %H:%M')
    if isinstance(v, Decimal):
        return f'{v:,.2f}' if v.as_tuple().exponent < 0 else f'{v:,}'
    return v


@app.template_filter('money')
def money(v):
    return f'₹{float(v or 0):,.2f}'


@app.template_filter('stars')
def stars(n):
    n = int(n or 0)
    return '★' * n + '☆' * (5 - n)


@app.errorhandler(mysql.connector.Error)
def db_error(err):
    return render_template('error.html', title='Database error', icon='database-x', message=friendly_error(err),
                           hint='Is MySQL running, and has schema.sql been imported (it creates the database user)? Check DB_USER / DB_PASSWORD.'), 500


ERROR_PAGES = {
    400: ('Request not accepted', 'shield-exclamation', 'Your session may have expired (for example after the server restarted).'),
    403: ('Not allowed', 'lock', 'Your role does not allow this.'),
    404: ('Page not found', 'compass', 'That address does not exist.'),
    405: ('Wrong way to open this page', 'signpost', 'This address cannot be opened directly.'),
}


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(405)
def http_error(err):
    title, icon, message = ERROR_PAGES[err.code]
    return render_template('error.html', title=title, icon=icon, message=message, code=err.code,
                           hint='Go back to the dashboard or sign in again.'), err.code


@app.after_request
def no_store_for_private_pages(resp):
    # After signing out, the Back button must not show a cached private page.
    if session.get('user') or request.endpoint not in PUBLIC_ENDPOINTS:
        resp.headers['Cache-Control'] = 'no-store'
    return resp


# --- 4. Dashboard -----------------------------------------------------------
@app.route('/')
def index():
    stats = query("""
        SELECT (SELECT COUNT(*) FROM Ticket)                              AS tickets,
               (SELECT COALESCE(SUM(Fare), 0) FROM Ticket)                AS revenue,
               (SELECT COUNT(*) FROM Passenger)                           AS passengers,
               (SELECT COUNT(*) FROM Vehicle WHERE Status = 'Active')     AS vehicles,
               (SELECT COUNT(*) FROM Vehicle)                             AS total_vehicles,
               (SELECT ROUND(AVG(Rating), 1) FROM Feedback)               AS rating
    """)[0]
    revenue = query("SELECT RouteName, Revenue, TicketsSold FROM RouteRevenue ORDER BY Revenue DESC LIMIT 8")
    occupancy = query("""SELECT ScheduleID, RouteName, DepartureTime, OccupancyPct, SeatsLeft
                         FROM ScheduleOccupancy ORDER BY OccupancyPct DESC LIMIT 6""")
    recent = query("SELECT * FROM BookingSummary ORDER BY TicketID DESC LIMIT 6")
    return render_template(
        'dashboard.html', stats=stats, recent=recent, occupancy=occupancy,
        chart_revenue={'labels': [r['RouteName'] for r in revenue],
                       'values': [float(r['Revenue']) for r in revenue]},
)


# --- 5. Bookings (stored procedures + triggers) -----------------------------
@app.route('/bookings')
def bookings():
    q = request.args.get('q', '').strip()
    where, args = '', []
    if q:
        where = 'WHERE PassengerName LIKE %s OR RouteName LIKE %s OR VehicleNo LIKE %s OR SeatNo LIKE %s'
        args = [like(q)] * 4
        if q.isdigit():
            where += ' OR TicketID = %s'
            args.append(int(q))
    total = query(f'SELECT COUNT(*) AS n FROM BookingSummary {where}', args)[0]['n']
    pg = page_info(total)
    rows = query(f'SELECT * FROM BookingSummary {where} ORDER BY TicketID DESC LIMIT %s OFFSET %s', args + [pg['per_page'], pg['offset']])
    return render_template(
        'bookings.html', bookings=rows, pg=pg, q=q,
        passengers=query("SELECT PassengerID, Name, CardType FROM Passenger ORDER BY Name"),
        schedules=query("""SELECT *, SeatsLeft(ScheduleID) AS Left_ FROM ScheduleOccupancy
                           WHERE VehicleStatus = 'Active' ORDER BY ScheduleID"""))


@app.route('/book', methods=['POST'])
def book_ticket():
    p_id, s_id, seat, fare = val('p_id'), val('s_id'), val('seat'), val('fare')
    if not (p_id.isdigit() and s_id.isdigit() and seat):
        flash('Passenger, schedule and seat number are required.', 'danger')
        return redirect(url_for('bookings'))
    if fare:
        try:
            f = float(fare)
            if not math.isfinite(f) or f <= 0 or f > 9999.99:
                raise ValueError
        except ValueError:
            flash('Fare must be a number between 0.01 and 9999.99 (or leave it blank to calculate it automatically).', 'danger')
            return redirect(url_for('bookings'))
    # BookTicket inserts a Ticket (blank fare -> CalcFare); triggers reject taken seats / full or idle vehicles
    return run(lambda: call('BookTicket', [p_id, s_id, seat.upper(), fare or None]),
               f'Ticket booked: seat {seat.upper()} on schedule #{s_id}.', 'bookings')


@app.route('/transfer/<int:ticket_id>', methods=['POST'])
def transfer_ticket(ticket_id):
    s_id, seat = val('s_id'), val('seat')
    if not (s_id.isdigit() and seat):
        flash('Choose the new departure and enter the new seat number.', 'danger')
        return redirect(url_for('bookings'))
    # TransferTicket is one all-or-nothing transaction: on any failure the original ticket is untouched
    return run(lambda: call('TransferTicket', [ticket_id, s_id, seat.upper()]),
               f'Ticket #{ticket_id} moved to schedule #{s_id}, seat {seat.upper()}.', 'bookings')


@app.route('/api/fare')
def api_fare():
    p, s = request.args.get('p', ''), request.args.get('s', '')
    if not (p.isdigit() and s.isdigit()):
        return jsonify(fare=None)
    fare = query("SELECT CalcFare(%s, %s) AS fare", (p, s))[0]['fare']
    return jsonify(fare=float(fare) if fare is not None else None)


def csv_safe(v):
    """Stop Excel/Sheets from running a cell that starts with = + - @ as a formula (CSV injection)."""
    v = '' if v is None else str(v)
    return "'" + v if v[:1] in ('=', '+', '-', '@', '\t', '\r') else v


@app.route('/bookings/export.csv')
def export_bookings():
    rows = query("SELECT * FROM BookingSummary ORDER BY TicketID")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['TicketID', 'Passenger', 'Route', 'Vehicle', 'Departure', 'Seat', 'Fare', 'BookedOn'])
    for r in rows:
        writer.writerow([csv_safe(x) for x in (r['TicketID'], r['PassengerName'], r['RouteName'], r['VehicleNo'],
                                               fmt(r['DepartureTime']), r['SeatNo'], r['Fare'], r['BookingDate'])])
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=bookings.csv'})


@app.route('/cancel/<int:ticket_id>', methods=['POST'])
def cancel_ticket(ticket_id):
    return run(lambda: call('CancelTicket', [ticket_id]), f'Ticket #{ticket_id} cancelled.', 'bookings')


# --- 6. Passengers (full CRUD) ----------------------------------------------
@app.route('/passengers')
def passengers():
    q = request.args.get('q', '').strip()
    where, args = '', []
    if q:
        where = 'WHERE P.Name LIKE %s OR P.Email LIKE %s OR P.Phone LIKE %s'
        args = [like(q)] * 3
        if q.isdigit():
            where += ' OR P.PassengerID = %s'
            args.append(int(q))
    total = query(f'SELECT COUNT(*) AS n FROM Passenger P {where}', args)[0]['n']
    pg = page_info(total)
    rows = query(f"""SELECT P.*,
                            (SELECT COUNT(*) FROM Ticket T WHERE T.PassengerID = P.PassengerID) AS Trips,
                            (SELECT COALESCE(SUM(T.Fare), 0) FROM Ticket T WHERE T.PassengerID = P.PassengerID) AS Spent
                     FROM Passenger P {where} ORDER BY P.PassengerID LIMIT %s OFFSET %s""", args + [pg['per_page'], pg['offset']])
    return render_template('passengers.html', passengers=rows, pg=pg, q=q)


def passenger_problem():
    """Return a message when the passenger form holds an invalid value, otherwise None."""
    if not val('name') or '@' not in val('email'):
        return 'A name and a valid email are required.'
    if val('phone') and not re.fullmatch(r'[0-9+\-\s()]{3,15}', val('phone')):
        return 'Phone may contain only digits, spaces, + - ( ) and be 3 to 15 characters long.'
    if val('dob'):
        try:
            d = date.fromisoformat(val('dob'))
        except ValueError:
            return 'Date of birth is not a valid date.'
        if d > date.today() or d.year < 1900:
            return 'Date of birth must be between 1900 and today.'
    return None


def passenger_args():
    return [val('name'), val('email').lower(), opt('phone'), opt('dob'), val('card_type') or 'None']


@app.route('/passengers/add', methods=['POST'])
def add_passenger():
    problem = passenger_problem()
    if problem:
        flash(problem, 'danger')
        return redirect(url_for('passengers'))
    return run(lambda: execute(
        "INSERT INTO Passenger (Name, Email, Phone, DOB, CardType) VALUES (%s, %s, %s, %s, %s)",
        passenger_args()), f'Passenger {val("name")} added.', 'passengers')


@app.route('/passengers/<int:pid>/edit', methods=['POST'])
def edit_passenger(pid):
    problem = passenger_problem()
    if problem:
        flash(problem, 'danger')
        return redirect(url_for('passengers'))
    return run(lambda: execute(
        "UPDATE Passenger SET Name=%s, Email=%s, Phone=%s, DOB=%s, CardType=%s WHERE PassengerID=%s",
        passenger_args() + [pid]), 'Passenger updated.', 'passengers')


@app.route('/passengers/<int:pid>/delete', methods=['POST'])
def delete_passenger(pid):
    return run(lambda: execute("DELETE FROM Passenger WHERE PassengerID=%s", (pid,)),
               'Passenger deleted.', 'passengers')


# --- 7. Fleet & drivers (full CRUD) -----------------------------------------
@app.route('/fleet')
def fleet():
    vehicles = query("""SELECT V.*, D.Name AS DriverName FROM Vehicle V
                        LEFT JOIN Driver D ON V.DriverID = D.DriverID ORDER BY V.VehicleID""")
    drivers = query("""SELECT D.*, V.VehicleNo FROM Driver D
                       LEFT JOIN Vehicle V ON V.DriverID = D.DriverID ORDER BY D.DriverID""")
    return render_template('fleet.html', vehicles=vehicles, drivers=drivers)


def vehicle_args():
    return [val('vehicle_no').upper(), val('type'), val('capacity'), opt('model'),
            val('status') or 'Active', opt('driver_id')]


@app.route('/vehicles/add', methods=['POST'])
def add_vehicle():
    return run(lambda: execute(
        "INSERT INTO Vehicle (VehicleNo, Type, Capacity, Model, Status, DriverID) VALUES (%s,%s,%s,%s,%s,%s)",
        vehicle_args()), 'Vehicle added.', 'fleet')


@app.route('/vehicles/<int:vid>/edit', methods=['POST'])
def edit_vehicle(vid):
    return run(lambda: execute(
        "UPDATE Vehicle SET VehicleNo=%s, Type=%s, Capacity=%s, Model=%s, Status=%s, DriverID=%s WHERE VehicleID=%s",
        vehicle_args() + [vid]), 'Vehicle updated.', 'fleet')


@app.route('/vehicles/<int:vid>/delete', methods=['POST'])
def delete_vehicle(vid):
    return run(lambda: execute("DELETE FROM Vehicle WHERE VehicleID=%s", (vid,)), 'Vehicle deleted.', 'fleet')


@app.route('/drivers/add', methods=['POST'])
def add_driver():
    return run(lambda: execute(
        "INSERT INTO Driver (Name, LicenseNo, ShiftHours, Phone) VALUES (%s,%s,%s,%s)",
        [val('name'), val('license_no').upper(), val('shift_hours') or 8, opt('phone')]),
        f'Driver {val("name")} added.', 'fleet')


@app.route('/drivers/<int:did>/delete', methods=['POST'])
def delete_driver(did):
    return run(lambda: execute("DELETE FROM Driver WHERE DriverID=%s", (did,)), 'Driver deleted.', 'fleet')


# --- 8. Routes & schedules --------------------------------------------------
@app.route('/routes')
def routes():
    q = request.args.get('q', '').strip()
    where, args = '', []
    if q:
        where = 'WHERE RouteName LIKE %s OR VehicleNo LIKE %s'
        args = [like(q)] * 2
        if q.isdigit():
            where += ' OR ScheduleID = %s'
            args.append(int(q))
    total = query(f'SELECT COUNT(*) AS n FROM ScheduleOccupancy {where}', args)[0]['n']
    pg = page_info(total, 40)
    return render_template(
        'routes.html', pg=pg, q=q,
        routes=query("SELECT * FROM RouteRevenue R JOIN Route USING (RouteID) ORDER BY RouteID"),
        schedules=query(f'SELECT * FROM ScheduleOccupancy {where} ORDER BY ScheduleID LIMIT %s OFFSET %s', args + [pg['per_page'], pg['offset']]),
        vehicles=query("SELECT VehicleID, VehicleNo, Type FROM ActiveVehicle ORDER BY VehicleNo"),
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
    return run(lambda: execute(
        "INSERT INTO Schedule (RouteID, VehicleID, DepartureTime, ArrivalTime, Frequency) VALUES (%s,%s,%s,%s,%s)",
        [val('route_id'), val('vehicle_id'), val('departure'), val('arrival'), opt('frequency')]),
        'Schedule added.', 'routes')


@app.route('/schedules/<int:sid>/delete', methods=['POST'])
def delete_schedule(sid):
    return run(lambda: execute("DELETE FROM Schedule WHERE ScheduleID=%s", (sid,)), 'Schedule deleted.', 'routes')


# --- 9. Feedback ------------------------------------------------------------
@app.route('/feedback')
def feedback():
    total = query('SELECT COUNT(*) AS n FROM Feedback')[0]['n']
    pg = page_info(total, 24)
    return render_template(
        'feedback.html', pg=pg,
        feedbacks=query("""SELECT F.*, P.Name, R.RouteName FROM Feedback F
                           JOIN Passenger P ON F.PassengerID = P.PassengerID
                           LEFT JOIN Route R ON F.RouteID = R.RouteID
                           ORDER BY F.FeedbackID DESC LIMIT %s OFFSET %s""", (pg['per_page'], pg['offset'])),
        passengers=query("SELECT PassengerID, Name FROM Passenger ORDER BY Name"),
        route_list=query("SELECT RouteID, RouteName FROM Route ORDER BY RouteName"),
        ratings=query("SELECT * FROM RouteRatings WHERE Reviews > 0 ORDER BY AvgRating DESC, Reviews DESC LIMIT 6"))


@app.route('/feedback/add', methods=['POST'])
def add_feedback():
    return run(lambda: execute(
        "INSERT INTO Feedback (PassengerID, RouteID, Rating, Comments) VALUES (%s,%s,%s,%s)",
        [val('passenger_id'), opt('route_id'), val('rating'), opt('comments')]),
        'Thanks! Feedback recorded.', 'feedback')


@app.route('/feedback/<int:fid>/delete', methods=['POST'])
def delete_feedback(fid):
    return run(lambda: execute("DELETE FROM Feedback WHERE FeedbackID=%s", (fid,)), 'Feedback deleted.', 'feedback')


# --- 10. Reports (joins, aggregates, HAVING, subqueries, LEFT JOIN ... IS NULL) ---
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
    {'title': 'Busier than average schedules',
     'desc': 'Subquery: schedules whose booked seats exceed the average across all schedules.',
     'sql': ("SELECT ScheduleID, RouteName, VehicleNo, Booked, OccupancyPct\n"
             "FROM ScheduleOccupancy\n"
             "WHERE Booked > (SELECT AVG(Booked) FROM ScheduleOccupancy)\n"
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
     'sql': ("SELECT A.AuditID, A.Action, A.TicketID, P.Name AS Passenger, A.SeatNo, A.Fare, A.ActionTime\n"
             "FROM TicketAudit A JOIN Passenger P ON P.PassengerID = A.PassengerID\n"
             "ORDER BY A.AuditID DESC\nLIMIT 10;")},
]


@app.route('/reports')
def reports():
    results = []
    for r in REPORTS:
        rows = query_proc(r['proc'])[:10] if r.get('proc') else query(r['sql'])
        results.append({**r, 'rows': rows, 'cols': list(rows[0].keys()) if rows else []})
    return render_template('reports.html', reports=results)


if __name__ == '__main__':
    # Debug mode is off by default (it exposes an interactive debugger). Use: FLASK_DEBUG=1 python app.py
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1')
