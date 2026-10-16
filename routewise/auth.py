"""Sign-in, roles, CSRF protection, the sign-in attempt limit, own-password change and user management."""
import secrets

from flask import abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import app
from .db import execute, query
from .web import PUBLIC_ENDPOINTS, back, run, val

# viewer: read-only | staff: bookings, passengers, feedback | admin: everything (fleet, routes, users)
ROLES = {'viewer': 0, 'staff': 1, 'admin': 2}
STAFF_ENDPOINTS = {'book_ticket', 'cancel_ticket', 'transfer_ticket', 'hold_seat', 'add_passenger', 'edit_passenger', 'add_feedback'}
ANY_USER_POSTS = {'logout', 'change_password'}      # POSTs every signed-in user may make, whatever the role
ADMIN_PAGES = {'users'}
DEMO_USERS = [  # demo accounts created on first start when the AppUser table is empty (unless DEMO_USERS=0)
    ('admin', 'Administrator', 'admin123', 'admin'),
    ('staff', 'Booking Clerk', 'staff123', 'staff'),
    ('viewer', 'Guest Viewer', 'viewer123', 'viewer'),
]
DEMO_PASSWORDS = {u: p for u, _, p, _ in DEMO_USERS}


def ensure_default_users():
    if app.config['DEMO_USERS'] and query("SELECT COUNT(*) AS n FROM AppUser")[0]['n'] == 0:
        for username, full, pwd, role in DEMO_USERS:
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
    return {'current_user': user, 'role': user['role'] if user else None, 'weak_password': session.get('weak_password')}


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


# --- sign-in attempt limit, stored in the LoginAttempt table (shared by every server process, survives restarts) ---
def _attempt_key():
    return val('username').lower()[:50], (request.remote_addr or '-')[:45]


def login_wait_seconds():
    """Seconds the current username/address pair must still wait, or 0 when sign-in is allowed."""
    rows = query('SELECT Failures, TIMESTAMPDIFF(SECOND, FirstFailure, NOW()) AS age FROM LoginAttempt '
                 'WHERE Username = %s AND Address = %s', _attempt_key())
    lock = app.config['LOGIN_LOCK_SECONDS']
    if rows and rows[0]['Failures'] >= app.config['LOGIN_MAX_FAILURES'] and rows[0]['age'] < lock:
        return lock - rows[0]['age']
    return 0


def record_failed_login():
    # One statement, so two simultaneous failures cannot overwrite each other's count. A failure after the window
    # has passed starts a new count. (MySQL applies the assignments left to right: FirstFailure sees the new Failures.)
    execute("""INSERT INTO LoginAttempt (Username, Address, Failures, FirstFailure) VALUES (%s, %s, 1, NOW())
               ON DUPLICATE KEY UPDATE Failures = IF(FirstFailure < NOW() - INTERVAL %s SECOND, 1, Failures + 1),
                                       FirstFailure = IF(Failures = 1, NOW(), FirstFailure)""",
            (*_attempt_key(), app.config['LOGIN_LOCK_SECONDS']))


@app.route('/login', methods=['GET', 'POST'])
def login():
    ensure_default_users()
    if request.method == 'GET' and session.get('user'):
        return redirect(url_for('index'))
    if request.method == 'POST':
        wait = login_wait_seconds()
        if wait:
            flash(f'Too many failed sign-in attempts. Try again in {(wait + 59) // 60} minute(s).', 'danger')
            return render_template('login.html'), 429
        username, password = val('username').lower(), request.form.get('password', '')
        rows = query("SELECT * FROM AppUser WHERE Username = %s", (username,))
        if rows and check_password_hash(rows[0]['PasswordHash'], password):
            execute('DELETE FROM LoginAttempt WHERE Username = %s AND Address = %s', _attempt_key())
            token = csrf_token()
            session.clear()
            session['csrf'] = token
            session['user'] = {'id': rows[0]['UserID'], 'name': rows[0]['FullName'],
                               'username': rows[0]['Username'], 'role': rows[0]['Role']}
            if DEMO_PASSWORDS.get(username) == password:
                session['weak_password'] = True          # a published demo password: the layout shows a warning
            nxt = request.args.get('next', '')
            return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else url_for('index'))
        record_failed_login()
        flash('Invalid username or password.', 'danger')
    return render_template('login.html')


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/account')
def account():
    return render_template('account.html')


@app.route('/account/password', methods=['POST'])
def change_password():
    current, new, confirm = (request.form.get(k, '') for k in ('current', 'new', 'confirm'))
    rows = query('SELECT PasswordHash FROM AppUser WHERE UserID = %s', (session['user']['id'],))
    if not rows or not check_password_hash(rows[0]['PasswordHash'], current):
        flash('Your current password is not correct.', 'danger')
    elif len(new) < 8:
        flash('The new password must be at least 8 characters long.', 'danger')
    elif new != confirm:
        flash('The new password and its confirmation do not match.', 'danger')
    elif new == current:
        flash('Choose a password different from the current one.', 'danger')
    elif new in DEMO_PASSWORDS.values():
        flash('That is one of the published demo passwords. Choose another one.', 'danger')
    else:
        execute('UPDATE AppUser SET PasswordHash = %s WHERE UserID = %s', (generate_password_hash(new), session['user']['id']))
        session.pop('weak_password', None)
        flash('Password changed.', 'success')
    return redirect(url_for('account'))


@app.route('/users')
def users():
    return render_template('users.html', users=query("SELECT * FROM AppUser ORDER BY UserID"))


@app.route('/users/add', methods=['POST'])
def add_user():
    if len(request.form.get('password', '')) < 8 or not val('username') or val('role') not in ROLES:
        flash('Username, a valid role and a password of at least 8 characters are required.', 'danger')
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
