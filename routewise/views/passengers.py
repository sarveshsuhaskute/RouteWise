"""Passengers: search (FULLTEXT), pagination and full CRUD."""
import re
from datetime import date

from flask import flash, redirect, render_template, request, url_for

from .. import app
from ..db import execute, query
from ..search import boolean_query, like, prefix
from ..web import opt, page_info, run, val


def passenger_filter(q):
    """WHERE clause and arguments for the passenger search box (each branch can use an index)."""
    if not q:
        return '', []
    if q.isdigit():                                                   # passenger number or the start of a phone number
        return 'WHERE P.PassengerID = %s OR P.Phone LIKE %s', [int(q), prefix(q)]
    terms = boolean_query(q)
    if terms:                                                         # words in the name or email: FULLTEXT index
        return 'WHERE MATCH(P.Name, P.Email) AGAINST (%s IN BOOLEAN MODE)', [terms]
    return 'WHERE P.Name LIKE %s OR P.Email LIKE %s', [like(q)] * 2    # only very short input falls back to a scan


@app.route('/passengers')
def passengers():
    q = request.args.get('q', '').strip()
    where, args = passenger_filter(q)
    total = query(f'SELECT COUNT(*) AS n FROM Passenger P {where}', args)[0]['n']
    pg = page_info(total)
    rows = query(f"""SELECT P.*,
                            (SELECT COUNT(*) FROM Ticket T WHERE T.PassengerID = P.PassengerID) AS Trips,
                            (SELECT COALESCE(SUM(T.Fare), 0) FROM Ticket T WHERE T.PassengerID = P.PassengerID) AS Spent
                     FROM Passenger P {where} ORDER BY P.PassengerID LIMIT %s OFFSET %s""", args + [pg['per_page'], pg['offset']])
    return render_template('passengers.html', passengers=rows, pg=pg, q=q,
                           cards=query('SELECT CardType, DiscountPct FROM CardDiscount ORDER BY DiscountPct'))


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
