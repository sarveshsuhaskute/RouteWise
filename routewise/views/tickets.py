"""E-tickets with a signed QR code, the check a conductor makes by scanning it, and today's departure board."""
import hashlib
import hmac
from datetime import date

import segno
from flask import render_template, url_for
from markupsafe import Markup

from .. import app
from ..db import query


def ticket_code(t):
    """Ticket number plus a signature over the facts that make the ticket valid; changing any of them breaks the code."""
    facts = f"{t['TicketID']}|{t['ScheduleID']}|{t['TravelDate']}|{t['SeatNo']}|{t['PassengerID']}"
    sig = hmac.new(app.secret_key.encode(), facts.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{t['TicketID']}-{sig}"


def load_ticket(ticket_id):
    rows = query("""SELECT T.TicketID, T.ScheduleID, T.TravelDate, T.SeatNo, T.Fare, T.BookingDate, T.PassengerID,
                           P.Name AS PassengerName, P.CardType, R.RouteName, R.Origin, R.Destination,
                           V.VehicleNo, S.DepartureTime, S.ArrivalTime, ST.Position
                    FROM Ticket T
                    JOIN Passenger P ON P.PassengerID = T.PassengerID
                    JOIN Schedule  S ON S.ScheduleID  = T.ScheduleID
                    JOIN Route     R ON R.RouteID     = S.RouteID
                    JOIN Vehicle   V ON V.VehicleID   = S.VehicleID
                    LEFT JOIN Seat ST ON ST.VehicleID = S.VehicleID AND ST.SeatNo = T.SeatNo
                    WHERE T.TicketID = %s""", (ticket_id,))
    return rows[0] if rows else None


@app.route('/tickets/<int:ticket_id>')
def e_ticket(ticket_id):
    t = load_ticket(ticket_id)
    if not t:
        return render_template('verify.html', state='missing', ticket_id=ticket_id), 404
    link = url_for('verify_ticket', code=ticket_code(t), _external=True)
    qr = Markup(segno.make(link, error='m').svg_inline(scale=5, dark='#0b2a2e', border=2))
    return render_template('ticket.html', t=t, qr=qr, link=link)


@app.route('/verify/<code>')
def verify_ticket(code):
    """What a conductor sees after scanning the QR code: valid today, valid for a later date, used, cancelled or forged."""
    number, _, _sig = code.partition('-')
    if not number.isdigit():
        return render_template('verify.html', state='invalid'), 400
    t = load_ticket(int(number))
    if t is None:
        cancelled = query("SELECT ActionTime, Actor FROM TicketAudit WHERE TicketID = %s AND Action = 'CANCELLED' "
                          "ORDER BY AuditID DESC LIMIT 1", (int(number),))
        return render_template('verify.html', state='cancelled' if cancelled else 'missing',
                               audit=cancelled[0] if cancelled else None, ticket_id=number), 410 if cancelled else 404
    if not hmac.compare_digest(ticket_code(t), code):
        return render_template('verify.html', state='invalid'), 400
    today = date.today()
    state = 'valid' if t['TravelDate'] == today else 'future' if t['TravelDate'] > today else 'expired'
    return render_template('verify.html', state=state, t=t)


@app.route('/board')
def departure_board():
    # what is moving or about to leave first, then the rest of the day, then what has already arrived
    rows = query("""SELECT * FROM DepartureBoard
                    ORDER BY FIELD(Status, 'En route', 'Boarding', 'Scheduled', 'Cancelled', 'Arrived'), DepartureTime, ScheduleID""")
    return render_template('board.html', rows=rows, today=date.today())
