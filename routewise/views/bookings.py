"""Bookings: book, transfer and cancel through stored procedures; seat map and fare look-ups; search; CSV export."""
import csv
import io
import math
from datetime import date, timedelta

from flask import Response, flash, jsonify, redirect, render_template, request, url_for

from .. import app
from ..db import call, query
from ..search import SEAT, VEHICLE, boolean_query, like, prefix
from ..web import csv_safe, date_arg, fmt, page_info, run, val


def booking_filter(q):
    """FROM/WHERE for the bookings search box. Words are looked up in the passenger and route FULLTEXT indexes,
    and the matching ticket numbers are joined to the BookingSummary view."""
    if not q:
        return 'BookingSummary B', '', []
    if q.isdigit():
        return 'BookingSummary B', 'WHERE B.TicketID = %s', [int(q)]
    if SEAT.fullmatch(q):
        return 'BookingSummary B', 'WHERE B.SeatNo = %s', [q.upper()]
    if VEHICLE.fullmatch(q):
        return 'BookingSummary B', 'WHERE B.VehicleNo LIKE %s', [prefix(q.upper())]
    terms = boolean_query(q)
    if not terms:
        return 'BookingSummary B', 'WHERE B.PassengerName LIKE %s OR B.RouteName LIKE %s', [like(q)] * 2
    hits = """(SELECT T.TicketID FROM Ticket T JOIN Passenger P ON P.PassengerID = T.PassengerID
                 WHERE MATCH(P.Name, P.Email) AGAINST (%s IN BOOLEAN MODE)
               UNION
               SELECT T.TicketID FROM Ticket T JOIN Schedule S ON S.ScheduleID = T.ScheduleID JOIN Route R ON R.RouteID = S.RouteID
                 WHERE MATCH(R.RouteName, R.Origin, R.Destination) AGAINST (%s IN BOOLEAN MODE)) AS hit"""
    return f'BookingSummary B JOIN {hits} ON hit.TicketID = B.TicketID', '', [terms, terms]


@app.route('/bookings')
def bookings():
    q = request.args.get('q', '').strip()
    source, where, args = booking_filter(q)
    total = query(f'SELECT COUNT(*) AS n FROM {source} {where}', args)[0]['n']
    pg = page_info(total)
    rows = query(f'SELECT B.* FROM {source} {where} ORDER BY B.TicketID DESC LIMIT %s OFFSET %s', args + [pg['per_page'], pg['offset']])
    return render_template(
        'bookings.html', bookings=rows, pg=pg, q=q,
        passengers=query("SELECT PassengerID, Name, CardType FROM Passenger ORDER BY Name"),
        schedules=query("""SELECT * FROM ScheduleOccupancy WHERE VehicleStatus = 'Active' ORDER BY ScheduleID"""),
        today=date.today().isoformat(), last_day=(date.today() + timedelta(days=30)).isoformat())


def seat_and_date(missing_message):
    """Validate the schedule / seat / travel-date fields; returns (schedule, seat, date) or flashes and returns None."""
    s_id, seat, travel = val('s_id'), val('seat').upper(), date_arg(val('travel_date'))
    if not (s_id.isdigit() and seat):
        flash(missing_message, 'danger')
        return None
    if travel is False:
        flash('The travel date is not a valid date.', 'danger')
        return None
    return s_id, seat, travel


@app.route('/book', methods=['POST'])
def book_ticket():
    p_id, fare = val('p_id'), val('fare')
    if not p_id.isdigit():
        flash('Passenger, schedule and seat number are required.', 'danger')
        return redirect(url_for('bookings'))
    picked = seat_and_date('Passenger, schedule and seat number are required.')
    if not picked:
        return redirect(url_for('bookings'))
    s_id, seat, travel = picked
    if fare:
        try:
            f = float(fare)
            if not math.isfinite(f) or f <= 0 or f > 9999.99:
                raise ValueError
        except ValueError:
            flash('Fare must be a number between 0.01 and 9999.99 (or leave it blank to calculate it automatically).', 'danger')
            return redirect(url_for('bookings'))
    # BookTicket inserts a Ticket (blank fare -> CalcFare, blank date -> today); the triggers reject a taken seat, a seat
    # that is not on the vehicle, a full or idle vehicle and a date on which the departure does not run
    return run(lambda: call('BookTicket', [p_id, s_id, seat, fare or None, travel]),
               f'Ticket booked: seat {seat} on schedule #{s_id}' + (f', {travel.isoformat()}.' if travel else '.'), 'bookings')


@app.route('/transfer/<int:ticket_id>', methods=['POST'])
def transfer_ticket(ticket_id):
    picked = seat_and_date('Choose the new departure and enter the new seat number.')
    if not picked:
        return redirect(url_for('bookings'))
    s_id, seat, travel = picked
    # TransferTicket is one all-or-nothing transaction: on any failure the original ticket is untouched
    return run(lambda: call('TransferTicket', [ticket_id, s_id, seat, travel]),
               f'Ticket #{ticket_id} moved to schedule #{s_id}, seat {seat}.', 'bookings')


@app.route('/cancel/<int:ticket_id>', methods=['POST'])
def cancel_ticket(ticket_id):
    return run(lambda: call('CancelTicket', [ticket_id]), f'Ticket #{ticket_id} cancelled.', 'bookings')


@app.route('/api/seats')
def api_seats():
    """The seat map of one trip: every seat of the vehicle with its position and whether it is taken on that date."""
    s, day = request.args.get('s', ''), date_arg(request.args.get('d', ''))
    if not s.isdigit() or not day:
        return jsonify(left=None)
    info = query("""SELECT V.Capacity, V.SeatsPerRow, SeatsLeft(S.ScheduleID, %s) AS left_, RunsOn(S.ScheduleID, %s) AS runs
                    FROM Schedule S JOIN Vehicle V ON V.VehicleID = S.VehicleID WHERE S.ScheduleID = %s""", (day, day, s))
    if not info:
        return jsonify(left=None)
    seats = query("""SELECT ST.SeatNo, ST.Position, T.TicketID IS NOT NULL AS Taken
                     FROM Schedule S JOIN Seat ST ON ST.VehicleID = S.VehicleID
                     LEFT JOIN Ticket T ON T.ScheduleID = S.ScheduleID AND T.TravelDate = %s AND T.SeatNo = ST.SeatNo
                     WHERE S.ScheduleID = %s ORDER BY LENGTH(ST.SeatNo), ST.SeatNo""", (day, s))
    i = info[0]
    return jsonify(left=i['left_'], capacity=i['Capacity'], per_row=i['SeatsPerRow'], runs=bool(i['runs']),
                   seats=[{'no': r['SeatNo'], 'pos': r['Position'], 'taken': bool(r['Taken'])} for r in seats])


@app.route('/api/fare')
def api_fare():
    p, s = request.args.get('p', ''), request.args.get('s', '')
    if not (p.isdigit() and s.isdigit()):
        return jsonify(fare=None)
    fare = query("SELECT CalcFare(%s, %s) AS fare", (p, s))[0]['fare']
    return jsonify(fare=float(fare) if fare is not None else None)


@app.route('/bookings/export.csv')
def export_bookings():
    rows = query("SELECT * FROM BookingSummary ORDER BY TicketID")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['TicketID', 'Passenger', 'Route', 'Vehicle', 'Departure', 'TravelDate', 'Seat', 'Fare', 'BookedOn'])
    for r in rows:
        writer.writerow([csv_safe(x) for x in (r['TicketID'], r['PassengerName'], r['RouteName'], r['VehicleNo'],
                                               fmt(r['DepartureTime']), r['TravelDate'], r['SeatNo'], r['Fare'], r['BookingDate'])])
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=bookings.csv'})
