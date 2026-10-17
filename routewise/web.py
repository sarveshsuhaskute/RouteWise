"""Request helpers shared by the pages, template filters, error pages and response headers."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import mysql.connector
from flask import flash, redirect, render_template, request, session, url_for

from . import app
from .db import friendly_error

PUBLIC_ENDPOINTS = {'login', 'static', 'healthz', 'metrics'}   # /metrics checks its own token


def val(name):
    return request.form.get(name, '').strip()


def opt(name):
    """Optional form field: empty string -> NULL."""
    return val(name) or None


def run(action, ok_message, endpoint):
    """Run a write action, flash the outcome (trigger/constraint errors included), redirect."""
    try:
        action()
        flash(ok_message, 'success')
    except mysql.connector.Error as err:
        flash(friendly_error(err), 'danger')
    return redirect(url_for(endpoint))


def back():
    ref = request.referrer or ''
    return redirect(ref if ref.startswith(request.host_url) else url_for('index'))


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


def date_arg(raw):
    """An ISO date string -> date, None when empty, False when it is not a valid date."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return False


def csv_safe(v):
    """Stop Excel/Sheets from running a cell that starts with = + - @ as a formula (CSV injection)."""
    v = '' if v is None else str(v)
    return "'" + v if v[:1] in ('=', '+', '-', '@', '\t', '\r') else v


@app.route('/healthz')
def healthz():
    """For container health checks and monitors: the application is up and can reach the database."""
    from .db import query
    query('SELECT 1 AS ok')
    return {'status': 'ok'}


# --- template filters ---------------------------------------------------------
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
    if isinstance(v, date):
        return v.strftime('%d %b %Y')
    if isinstance(v, Decimal):
        return f'{v:,.2f}' if v.as_tuple().exponent < 0 else f'{v:,}'
    if isinstance(v, set):                       # a MySQL SET column, e.g. service days
        return days(v)
    return v


@app.template_filter('money')
def money(v):
    return f'₹{float(v or 0):,.2f}'


@app.template_filter('stars')
def stars(n):
    n = int(n or 0)
    return '★' * n + '☆' * (5 - n)


WEEK = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


@app.template_filter('days')
def days(v):
    """Service days ('Mon,Tue,...' or a set) in a short readable form: Daily, Mon–Fri, Sat, Sun ..."""
    chosen = set(v.split(',') if isinstance(v, str) else v or ())
    if chosen >= set(WEEK):
        return 'Daily'
    if chosen == set(WEEK[:5]):
        return 'Mon–Fri'
    if chosen == set(WEEK[5:]):
        return 'Weekends'
    return ', '.join(d for d in WEEK if d in chosen)


# --- errors and headers ----------------------------------------------------------
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
