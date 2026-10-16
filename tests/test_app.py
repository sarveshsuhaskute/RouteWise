"""End-to-end tests: Flask test client + a throwaway MySQL database built from schema.sql.

Run from the project folder:   python -m unittest discover -s tests -v
(the tests use the MySQL admin account to build a throw-away database; set MYSQL_ADMIN_USER / MYSQL_ADMIN_PASSWORD if
yours is not root with an empty password. The tests never touch the real database)
"""
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import date, timedelta
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = 'routewise_test'
os.environ['DB_NAME'] = TEST_DB          # must be set before the application is imported
sys.path.insert(0, ROOT)


def mysql(sql=None, stdin=None):
    env = dict(os.environ, MYSQL_PWD=os.environ.get('MYSQL_ADMIN_PASSWORD', ''))
    cmd = ['mysql', '-u', os.environ.get('MYSQL_ADMIN_USER', 'root'), '-h', os.environ.get('DB_HOST', 'localhost')]
    if sql:
        cmd += ['-e', sql]
    return subprocess.run(cmd, input=stdin, text=True, env=env, capture_output=True, check=True)


def setUpModule():
    with open(os.path.join(ROOT, 'schema.sql')) as f:
        schema = f.read().replace('public_transport', TEST_DB)
    mysql(f'DROP DATABASE IF EXISTS {TEST_DB}')
    mysql(stdin=schema)


def tearDownModule():
    mysql(f'DROP DATABASE IF EXISTS {TEST_DB}')


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as entry                                         # the same import the production server uses
        from routewise import db
        from routewise.views import reports
        cls.appmod = SimpleNamespace(app=entry.app, query=db.query, execute=db.execute, call=db.call, query_proc=db.query_proc,
                                     db_config=db.db_config, REPORTS=reports.REPORTS, reports=reports)
        entry.app.config['TESTING'] = True
        entry.app.config['REPORT_CACHE_SECONDS'] = 0                # tests always see fresh report data

    def setUp(self):
        self.client = self.appmod.app.test_client()

    def token(self, path='/login'):
        html = self.client.get(path).get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def login(self, user='admin', pwd=None):
        pwd = pwd or {'admin': 'admin123', 'staff': 'staff123', 'viewer': 'viewer123'}[user]
        return self.client.post('/login', data={'username': user, 'password': pwd, 'csrf_token': self.token()},
                                follow_redirects=True)

    def post(self, path, **data):
        data['csrf_token'] = self.token('/')
        return self.client.post(path, data=data, follow_redirects=True).get_data(as_text=True)


class AuthTests(Base):
    def test_pages_require_login(self):
        for path in ['/', '/bookings', '/passengers', '/fleet', '/routes', '/feedback', '/reports']:
            r = self.client.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn('/login', r.headers['Location'])

    def test_bad_password_rejected(self):
        self.assertIn('Invalid username or password', self.login('admin', 'wrong').get_data(as_text=True))

    def test_post_without_csrf_token_is_rejected(self):
        self.login()
        self.assertEqual(self.client.post('/book', data={'p_id': 1, 's_id': 1, 'seat': 'X1'}).status_code, 400)

    def test_login_then_all_pages_render(self):
        self.login()
        for path in ['/', '/bookings', '/passengers', '/fleet', '/routes', '/feedback', '/reports', '/users']:
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_logout(self):
        self.login()
        self.post('/logout')
        self.assertEqual(self.client.get('/').status_code, 302)


class RoleTests(Base):
    def test_viewer_is_read_only(self):
        self.login('viewer')
        self.assertEqual(self.client.get('/reports').status_code, 200)
        self.assertIn('not allowed', self.post('/book', p_id=1, s_id=1, seat='V1', fare=10))
        self.assertIn('not allowed', self.post('/passengers/add', name='X', email='x@x.com'))

    def test_staff_can_book_but_not_manage_fleet(self):
        self.login('staff')
        self.assertIn('Ticket booked', self.post('/book', p_id=5, s_id=8, seat='6A', fare=40))
        self.assertIn('not allowed', self.post('/vehicles/add', vehicle_no='KA-00-S-1', type='Bus', capacity=10))
        self.assertEqual(self.client.get('/users').status_code, 302)       # admin-only page

    def test_admin_manages_users(self):
        self.login('admin')
        self.assertIn('created', self.post('/users/add', username='newbie', full_name='New', password='secret123', role='staff'))
        self.assertIn('at least 8', self.post('/users/add', username='weak', password='123', role='staff'))
        self.assertIn('cannot delete your own', self.post('/users/1/delete'))


class BookingTests(Base):
    def setUp(self):
        super().setUp()
        self.login('admin')

    def test_booking_and_double_seat(self):
        self.assertIn('Ticket booked: seat 9A', self.post('/book', p_id=1, s_id=2, seat='9a', fare=45))       # labels are upper-cased
        self.assertIn('Seat already booked', self.post('/book', p_id=2, s_id=2, seat='9A', fare=45))

    def test_auto_fare_uses_card_discount(self):
        # passenger 1 = Monthly card (30% off); schedule 9 = route 335E, 35 km bus -> 35 * 1.5 = 52.50 -> 36.75
        self.assertIn('Ticket booked', self.post('/book', p_id=1, s_id=9, seat='7A'))
        rows = self.appmod.query("SELECT Fare FROM Ticket WHERE ScheduleID=9 AND SeatNo='7A'")
        self.assertEqual(float(rows[0]['Fare']), 36.75)

    def test_fare_api_matches_function(self):
        r = self.client.get('/api/fare?p=1&s=1').get_json()
        self.assertEqual(r['fare'], 26.78)
        self.assertEqual(self.client.get('/api/fare?p=5&s=1').get_json()['fare'], 38.25)
        self.assertIsNone(self.client.get('/api/fare?p=x&s=1').get_json()['fare'])

    def test_vehicle_full(self):
        self.post('/vehicles/add', vehicle_no='KA-00-T-2', type='Bus', capacity=1, status='Active')
        vid = self.appmod.query("SELECT VehicleID FROM Vehicle WHERE VehicleNo='KA-00-T-2'")[0]['VehicleID']
        self.post('/schedules/add', route_id=1, vehicle_id=vid, departure='01:00', arrival='02:00')
        sid = self.appmod.query("SELECT MAX(ScheduleID) m FROM Schedule")[0]['m']
        self.assertIn('Ticket booked', self.post('/book', p_id=1, s_id=sid, seat='1A', fare=10))
        self.assertIn('Vehicle is full', self.post('/book', p_id=2, s_id=sid, seat='1B', fare=10))

    def test_vehicle_in_maintenance_cannot_be_booked(self):
        self.post('/vehicles/add', vehicle_no='KA-00-M-3', type='Bus', capacity=10, status='Maintenance')
        vid = self.appmod.query("SELECT VehicleID FROM Vehicle WHERE VehicleNo='KA-00-M-3'")[0]['VehicleID']
        # schedules can only be created for active vehicles in the UI, so insert directly
        self.appmod.execute("INSERT INTO Schedule (RouteID, VehicleID, DepartureTime, ArrivalTime) VALUES (1, %s, '02:00', '03:00')", (vid,))
        sid = self.appmod.query("SELECT MAX(ScheduleID) m FROM Schedule")[0]['m']
        self.assertIn('not in service', self.post('/book', p_id=1, s_id=sid, seat='1A', fare=10))

    def test_cancel_writes_audit_log(self):
        self.post('/book', p_id=3, s_id=3, seat='8A', fare=30)
        tid = self.appmod.query("SELECT TicketID FROM Ticket WHERE ScheduleID=3 AND SeatNo='8A'")[0]['TicketID']
        self.assertIn('cancelled', self.post(f'/cancel/{tid}'))
        actions = [r['Action'] for r in self.appmod.query("SELECT Action FROM TicketAudit WHERE TicketID=%s ORDER BY AuditID", (tid,))]
        self.assertEqual(actions, ['BOOKED', 'CANCELLED'])
        self.assertIn('Ticket not found', self.post('/cancel/999999'))

    def test_input_validation(self):
        self.assertIn('required', self.post('/book', p_id='', s_id=1, seat='', fare=10))
        self.assertIn('between 0.01 and 9999.99', self.post('/book', p_id=1, s_id=1, seat='N1', fare=-5))

    def test_csv_export(self):
        r = self.client.get('/bookings/export.csv')
        self.assertEqual(r.mimetype, 'text/csv')
        self.assertTrue(r.get_data(as_text=True).startswith('TicketID,Passenger'))


class CrudTests(Base):
    def setUp(self):
        super().setUp()
        self.login('admin')

    def test_passenger_crud_and_constraints(self):
        self.assertIn('added', self.post('/passengers/add', name='Crud Tester', email='crud@x.com', card_type='Daily'))
        self.assertIn('Duplicate', self.post('/passengers/add', name='Other', email='crud@x.com'))
        pid = self.appmod.query("SELECT PassengerID FROM Passenger WHERE Email='crud@x.com'")[0]['PassengerID']
        self.assertIn('updated', self.post(f'/passengers/{pid}/edit', name='Crud Edited', email='crud@x.com', card_type='Weekly'))
        self.assertEqual(self.appmod.query("SELECT CardType FROM Passenger WHERE PassengerID=%s", (pid,))[0]['CardType'], 'Weekly')
        self.assertIn('deleted', self.post(f'/passengers/{pid}/delete'))
        self.assertIn('Cannot delete', self.post('/passengers/1/delete'))      # has tickets (FK)

    def test_check_constraints(self):
        self.assertIn('violates a database rule', self.post('/vehicles/add', vehicle_no='KA-0-X', type='Bus', capacity=0))
        self.assertIn('violates a database rule', self.post('/feedback/add', passenger_id=1, rating=9))
        self.assertIn('violates a database rule', self.post('/schedules/add', route_id=1, vehicle_id=1, departure='10:00', arrival='09:00'))

    def test_route_schedule_feedback(self):
        self.assertIn('Route added', self.post('/routes/add', route_name='Test Line', origin='A', destination='B', type='Bus', distance='5'))
        self.assertIn('Thanks', self.post('/feedback/add', passenger_id=1, route_id=1, rating=4, comments='ok'))


class RegressionTests(Base):
    """One test per bug found during the bug hunt, so none of them can come back."""

    def setUp(self):
        super().setUp()
        self.login('admin')

    def test_concurrent_bookings_neither_deadlock_nor_oversell(self):
        import threading
        import mysql.connector
        self.post('/vehicles/add', vehicle_no='KA-00-RACE', type='Bus', capacity=5, status='Active')
        vid = self.appmod.query("SELECT VehicleID v FROM Vehicle WHERE VehicleNo='KA-00-RACE'")[0]['v']
        self.post('/schedules/add', route_id=1, vehicle_id=vid, departure='03:00', arrival='04:00')
        sid = self.appmod.query("SELECT MAX(ScheduleID) m FROM Schedule")[0]['m']
        barrier, errors, seats = threading.Barrier(20), [], ['1A', '1B', '1C', '1D', '2A']   # the whole seat map

        def book(i):
            conn = mysql.connector.connect(**self.appmod.db_config)
            barrier.wait()
            try:
                cur = conn.cursor()
                cur.callproc('BookTicket', [1 + i % 13, sid, seats[i % 5], 10, None])     # four sessions fight for every seat
                conn.commit()
            except mysql.connector.Error as e:
                errors.append(e.msg)
            finally:
                conn.close()
        threads = [threading.Thread(target=book, args=(i,)) for i in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        stored = self.appmod.query("SELECT COUNT(*) n FROM Ticket WHERE ScheduleID=%s", (sid,))[0]['n']
        self.assertEqual(stored, 5)                                              # exactly the capacity
        self.assertEqual(len(errors), 15)
        self.assertTrue(all('Vehicle is full' in e or 'Seat already booked' in e for e in errors), errors)   # no deadlock errors

    def test_vehicle_cannot_have_overlapping_runs(self):
        # vehicle 1 already runs 06:00-07:30
        self.assertIn('already has a run', self.post('/schedules/add', route_id=2, vehicle_id=1, departure='06:30', arrival='07:00'))
        self.assertIn('Schedule added', self.post('/schedules/add', route_id=2, vehicle_id=1, departure='07:30', arrival='08:00'))   # back-to-back is fine

    def test_csv_export_neutralises_formulas(self):
        self.post('/passengers/add', name='=HYPERLINK("http://evil","x")', email='csvinj@x.com')
        pid = self.appmod.query("SELECT PassengerID p FROM Passenger WHERE Email='csvinj@x.com'")[0]['p']
        self.post('/book', p_id=pid, s_id=3, seat='8B', fare=5)
        text = self.client.get('/bookings/export.csv').get_data(as_text=True)
        self.assertIn("\"'=HYPERLINK", text)
        self.assertNotIn(',"=HYPERLINK', text)

    def test_invalid_fares_and_values_are_rejected_cleanly(self):
        for bad in ('nan', 'inf', '1e10', '0', '-3', '10000'):
            self.assertIn('between 0.01 and 9999.99', self.post('/book', p_id=1, s_id=3, seat='BAD1', fare=bad), bad)
        out = self.post('/book', p_id=1, s_id=3, seat='X' * 40, fare=5)
        self.assertIn('too long', out)
        self.assertNotIn('p_seat', out)                                          # no internal names leaked

    def test_passenger_dates_and_phone_are_validated(self):
        self.assertIn('between 1900 and today', self.post('/passengers/add', name='F', email='f@x.com', dob='2999-01-01'))
        self.assertIn('Phone may contain only', self.post('/passengers/add', name='P', email='p@x.com', phone='abc!!'))
        self.assertIn('added', self.post('/passengers/add', name='Ok', email='ok@x.com', phone='+91 98765-43210', dob='2004-05-10'))


class _Scan(__import__('html.parser', fromlist=['HTMLParser']).HTMLParser):
    """Collect POST forms and links from a page, noting which ones CSS would hide for a role."""
    VOID = {'input', 'br', 'img', 'meta', 'link', 'hr'}

    def __init__(self):
        super().__init__()
        self.stack, self.forms, self.links, self.cur = [], [], [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get('class') or '').split()
        if tag not in self.VOID:
            self.stack.append((tag, cls))
        hide = lambda name: any(name in c for _, c in self.stack)
        if tag == 'form' and (a.get('method') or '').upper() == 'POST':
            self.cur = {'action': a.get('action'), 'staff': hide('w-staff'), 'admin': hide('w-admin'), 'fields': {}}
            self.forms.append(self.cur)
        if tag in ('input', 'select', 'textarea') and self.cur is not None and a.get('name'):
            self.cur['fields'][a['name']] = a.get('value', '') or '1'
        if tag == 'a' and (a.get('href') or '').startswith('/') and not a['href'].startswith('/static'):
            self.links.append({'href': a['href'].split('#')[0], 'staff': hide('w-staff'), 'admin': hide('w-admin')})

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag == 'form':
            self.cur = None


class UiAuditTests(Base):
    """Every role, every page, every visible button, on a private database (the buttons really are pressed)."""
    DB = 'routewise_matrix'
    PW = {'admin': 'admin123', 'staff': 'staff123', 'viewer': 'viewer123'}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(os.path.join(ROOT, 'schema.sql')) as f:
            schema = f.read().replace('public_transport', cls.DB)
        mysql(f'DROP DATABASE IF EXISTS {cls.DB}')
        mysql(stdin=schema)
        cls.original_db = cls.appmod.db_config['database']
        cls.appmod.db_config['database'] = cls.DB

    @classmethod
    def tearDownClass(cls):
        cls.appmod.db_config['database'] = cls.original_db
        mysql(f'DROP DATABASE IF EXISTS {cls.DB}')

    def as_role(self, role):
        self.client = self.appmod.app.test_client()
        self.client.post('/login', data={'username': role, 'password': self.PW[role], 'csrf_token': self.token()})
        return self.client

    @staticmethod
    def hidden(role, o):
        return (o['admin'] and role != 'admin') or (o['staff'] and role == 'viewer')

    def pages(self):
        return sorted({r.rule for r in self.appmod.app.url_map.iter_rules()
                       if 'GET' in r.methods and not r.arguments and r.endpoint not in ('static', 'login')})

    def test_every_role_can_sign_out(self):
        for role in self.PW:
            c = self.as_role(role)
            r = c.post('/logout', data={'csrf_token': self.token('/')})
            self.assertEqual(r.status_code, 302, role)
            self.assertIn('/login', r.headers['Location'], role)
            self.assertEqual(c.get('/').status_code, 302, f'{role} is still signed in after logout')

    def test_every_visible_link_and_button_works_for_its_role(self):
        for role in self.PW:
            c = self.as_role(role)
            forms = {}
            for page in self.pages():
                if page == '/users' and role != 'admin':
                    continue
                r = c.get(page)
                self.assertEqual(r.status_code, 200, f'{role}: GET {page}')
                sc = _Scan()
                sc.feed(r.get_data(as_text=True))
                for f in sc.forms:
                    if f['action'] and f['action'] != '/logout' and not f['action'].startswith('/users/'):   # deleting users would remove the other roles' accounts
                        forms[f['action']] = f
                for link in sc.links:
                    if not self.hidden(role, link):
                        self.assertIn(c.get(link['href']).status_code, (200,), f'{role}: link {link["href"]} on {page}')
            for action, f in sorted(forms.items()):
                data = dict(f['fields'])
                data['csrf_token'] = self.token('/')
                resp = c.post(action, data=data, follow_redirects=True)
                self.assertLess(resp.status_code, 500, f'{role}: POST {action}')
                refused = 'not allowed to do that' in resp.get_data(as_text=True)
                if not self.hidden(role, f):
                    self.assertFalse(refused, f'{role}: a visible button ({action}) is refused by the server')

    def test_viewer_and_staff_pages_contain_no_forbidden_forms(self):
        for role, forbidden in (('viewer', ('/vehicles/add', '/drivers/add', '/routes/add', '/schedules/add', '/passengers/add', '/feedback/add')),
                                ('staff', ('/vehicles/add', '/drivers/add', '/routes/add', '/schedules/add'))):
            c = self.as_role(role)
            html = ''.join(c.get(p).get_data(as_text=True) for p in self.pages() if p != '/users')
            for action in forbidden:
                self.assertNotIn(f'action="{action}"', html, f'{role} page still contains the form for {action}')

    def test_error_pages_are_styled_and_private_pages_are_not_cached(self):
        c = self.as_role('admin')
        for path, code in (('/no-such-page', 404), ('/book', 405)):
            r = c.get(path)
            self.assertEqual(r.status_code, code)
            self.assertIn('RouteWise', r.get_data(as_text=True))
        r = c.post('/book', data={'p_id': 1})                                       # no CSRF token
        self.assertEqual(r.status_code, 400)
        self.assertIn('session may have expired', r.get_data(as_text=True))
        self.assertEqual(c.get('/').headers.get('Cache-Control'), 'no-store')
        self.assertEqual(c.get('/login').status_code, 302)                         # already signed in -> dashboard

    def test_login_redirect_cannot_leave_the_site(self):
        for target, expected in (('//evil.com', '/'), ('http://evil.com', '/'), ('/bookings', '/bookings')):
            c = self.client = self.appmod.app.test_client()
            r = c.post(f'/login?next={target}', data={'username': 'admin', 'password': 'admin123', 'csrf_token': self.token()})
            self.assertEqual(r.headers['Location'], expected)

    def test_pages_survive_completely_empty_tables(self):
        self.as_role('admin')
        # emptied by the admin account: the application's own user is (rightly) not allowed to wipe the audit table
        mysql('SET FOREIGN_KEY_CHECKS=0; ' + ' '.join(f'DELETE FROM {self.DB}.{t};' for t in
              ('TicketAudit', 'Ticket', 'Feedback', 'Schedule', 'Vehicle', 'Driver', 'Route', 'Passenger')))
        for page in self.pages():
            self.assertEqual(self.client.get(page).status_code, 200, f'empty database: GET {page}')


class TransactionTests(Base):
    """TransferTicket is one all-or-nothing transaction: it either fully succeeds or leaves no trace."""

    def setUp(self):
        super().setUp()
        self.login('admin')

    def ticket(self, schedule, seat):
        rows = self.appmod.query("SELECT * FROM Ticket WHERE ScheduleID=%s AND SeatNo=%s", (schedule, seat))
        return rows[0] if rows else None

    def counts(self):
        return (self.appmod.query("SELECT COUNT(*) n FROM Ticket")[0]['n'], self.appmod.query("SELECT COUNT(*) n FROM TicketAudit")[0]['n'])

    def test_transfer_moves_the_ticket_recalculates_the_fare_and_audits_both_steps(self):
        self.post('/book', p_id=5, s_id=8, seat='6B', fare=40)                     # passenger 5 has no card (no discount)
        old = self.ticket(8, '6B')
        self.assertIn('moved to schedule #2', self.post(f"/transfer/{old['TicketID']}", s_id=2, seat='9b'))
        self.assertIsNone(self.ticket(8, '6B'))                                    # old seat freed
        new = self.ticket(2, '9B')                                                 # seat upper-cased
        self.assertEqual(new['PassengerID'], 5)
        self.assertEqual(float(new['Fare']), 38.25)                                # CalcFare for the new route (25.5 km x 1.5)
        actions = [r['Action'] for r in self.appmod.query(
            "SELECT Action FROM TicketAudit WHERE TicketID IN (%s, %s) ORDER BY AuditID", (old['TicketID'], new['TicketID']))]
        self.assertEqual(actions, ['BOOKED', 'CANCELLED', 'BOOKED'])

    def test_failed_transfer_rolls_everything_back(self):
        self.post('/book', p_id=5, s_id=8, seat='6C', fare=40)
        self.post('/book', p_id=6, s_id=2, seat='9C', fare=40)                     # the seat the first passenger wants
        before = self.counts()
        mine = self.ticket(8, '6C')
        self.assertIn('Seat already booked', self.post(f"/transfer/{mine['TicketID']}", s_id=2, seat='9C'))
        self.assertEqual(self.ticket(8, '6C')['TicketID'], mine['TicketID'])       # original ticket untouched
        self.assertEqual(self.counts(), before)                                    # no ticket and no audit row appeared or vanished

    def test_transfer_to_a_vehicle_under_maintenance_rolls_back(self):
        self.post('/vehicles/add', vehicle_no='KA-00-TRM', type='Bus', capacity=10, status='Maintenance')
        vid = self.appmod.query("SELECT VehicleID v FROM Vehicle WHERE VehicleNo='KA-00-TRM'")[0]['v']
        self.post('/schedules/add', route_id=1, vehicle_id=vid, departure='23:00', arrival='23:30')
        sid = self.appmod.query("SELECT MAX(ScheduleID) m FROM Schedule")[0]['m']
        self.post('/book', p_id=7, s_id=8, seat='7A', fare=40)
        before, mine = self.counts(), self.ticket(8, '7A')
        self.assertIn('not in service', self.post(f"/transfer/{mine['TicketID']}", s_id=sid, seat='1A'))
        self.assertIsNotNone(self.ticket(8, '7A'))
        self.assertEqual(self.counts(), before)

    def test_unknown_ticket_and_missing_fields(self):
        self.assertIn('Ticket not found', self.post('/transfer/999999', s_id=2, seat='11A'))
        self.assertIn('Choose the new departure', self.post('/transfer/1', s_id='', seat=''))

    def test_only_staff_and_admin_can_transfer(self):
        self.client = self.appmod.app.test_client()
        self.client.post('/login', data={'username': 'viewer', 'password': 'viewer123', 'csrf_token': self.token()})
        self.assertIn('not allowed', self.post('/transfer/1', s_id=2, seat='11A'))
        self.assertNotIn('transferModal', self.client.get('/bookings').get_data(as_text=True))


class PrivilegeTests(Base):
    """The application never runs as root: it has just the rights it needs."""

    def app_conn(self):
        import mysql.connector
        return mysql.connector.connect(**self.appmod.db_config)

    def test_application_does_not_connect_as_root(self):
        self.assertNotEqual(self.appmod.db_config['user'], 'root')

    def test_destructive_and_structural_statements_are_denied(self):
        import mysql.connector
        for stmt in ('DROP TABLE Feedback', 'ALTER TABLE Ticket ADD COLUMN zz INT', 'CREATE TABLE zz (a INT)',
                     "UPDATE TicketAudit SET Action = 'FORGED'", 'DELETE FROM TicketAudit', 'DROP TRIGGER AuditBooking',
                     "CREATE USER 'x'@'localhost'", 'TRUNCATE TABLE Ticket', "INSERT INTO Seat VALUES (1, '99A', 'Window')",
                     'DELETE FROM Seat', 'DROP EVENT PurgeLoginAttempts', 'DELETE FROM SeatHold',
                     'CALL GenerateSeats(1, 50, 4)', 'CALL ExtendAuditPartitions(48)',       # invoker-rights routines
                     "INSERT INTO Payment (TicketID, PassengerID, Kind, Method, Amount) VALUES (1, 1, 'Sale', 'Cash', 5)",
                     'UPDATE Payment SET Amount = 1', 'DELETE FROM Payment'):                  # the ledger is written by triggers only
            conn = self.app_conn()
            try:
                with self.assertRaises(mysql.connector.Error, msg=stmt) as cm:
                    conn.cursor().execute(stmt)
                self.assertIn(cm.exception.errno, (1142, 1044, 1227), stmt)         # privilege errors
            finally:
                conn.close()

    def test_audit_table_is_written_only_by_triggers(self):
        conn = self.app_conn()
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM TicketAudit'); before = cur.fetchone()[0]
        cur.callproc('BookTicket', [13, 9, '7B', 10, None]); conn.commit()               # the app has no INSERT right on TicketAudit...
        cur.execute('SELECT COUNT(*) FROM TicketAudit'); after = cur.fetchone()[0]
        conn.close()
        self.assertEqual(after, before + 1)                                          # ...yet the trigger recorded the booking

    def test_updatable_view_with_check_option(self):
        active = self.appmod.query("SELECT COUNT(*) n FROM Vehicle WHERE Status='Active'")[0]['n']
        self.assertEqual(self.appmod.query("SELECT COUNT(*) n FROM ActiveVehicle")[0]['n'], active)
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            mysql_run = dict(os.environ, MYSQL_PWD=os.environ.get('MYSQL_ADMIN_PASSWORD', ''))
            subprocess.run(['mysql', '-u', os.environ.get('MYSQL_ADMIN_USER', 'root'), TEST_DB, '-e',
                            "UPDATE ActiveVehicle SET Status='Retired' WHERE VehicleID=1"],
                           env=mysql_run, check=True, capture_output=True, text=True)
        self.assertIn('CHECK OPTION failed', cm.exception.stderr)
        self.assertEqual(self.appmod.query("SELECT Status FROM Vehicle WHERE VehicleID=1")[0]['Status'], 'Active')


class AnalyticsTests(Base):
    """The window-function, CTE, recursive-CTE and cursor reports return correct results."""

    def report(self, title):
        r = next(x for x in self.appmod.REPORTS if x['title'] == title)
        return self.appmod.query_proc(r['proc']) if r.get('proc') else self.appmod.query(r['sql'])

    def test_running_revenue_is_a_true_running_total(self):
        rows = self.report('Running revenue per route')
        self.assertTrue(rows)
        totals = {}
        for r in rows:
            totals[r['RouteName']] = totals.get(r['RouteName'], 0) + r['Fare']
            self.assertEqual(r['RunningRevenue'], totals[r['RouteName']])

    def test_rank_within_card_type(self):
        rows = self.report('Passenger rank within each travel-card type')
        by_card = {}
        for r in rows:
            by_card.setdefault(r['CardType'], []).append(r)
        for card, group in by_card.items():
            self.assertEqual(group[0]['RankInCard'], 1, card)                        # every card type has a rank 1
            spend = [g['TotalSpent'] for g in group]
            self.assertEqual(spend, sorted(spend, reverse=True), card)               # ranked from highest spend down

    def test_recursive_cte_covers_every_hour_and_counts_every_departure(self):
        rows = self.report('Departures per hour of the day')
        self.assertEqual([r['HourOfDay'] for r in rows], [f'{h:02d}:00' for h in range(5, 21)])
        expected = self.appmod.query("SELECT COUNT(*) n FROM Schedule WHERE HOUR(DepartureTime) BETWEEN 5 AND 20")[0]['n']
        self.assertEqual(sum(r['Departures'] for r in rows), expected)

    def test_cursor_procedure_scores_every_route_consistently(self):
        rows = self.report('Route scorecard (stored procedure with a cursor)')
        routes = self.appmod.query("SELECT COUNT(*) n FROM Route")[0]['n']
        self.assertEqual(len(rows), routes)
        self.assertEqual([r['Score'] for r in rows], sorted((r['Score'] for r in rows), reverse=True))
        for r in rows:
            expected = round(float(r['Revenue']) / 10 + float(r['AvgRating'] or 0) * 10, 1)
            self.assertAlmostEqual(float(r['Score']), expected, places=1)
            self.assertEqual(r['Grade'], 'A' if r['Score'] >= 60 else 'B' if r['Score'] >= 50 else 'C')


class OperationsTests(Base):
    """Backup and restore reproduce the database exactly (data, views, routines, triggers)."""

    def test_backup_then_restore_round_trip(self):
        import tempfile
        target = 'routewise_restore_test'
        env = dict(os.environ, DB_NAME=TEST_DB, MYSQL_PWD=os.environ.get('MYSQL_ADMIN_PASSWORD', ''))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, 'backup.sql')
                subprocess.run([os.path.join(ROOT, 'scripts', 'backup.sh'), out], env=env, check=True, capture_output=True)
                self.assertGreater(os.path.getsize(out), 5000)
                mysql(f'DROP DATABASE IF EXISTS {target}')
                subprocess.run([os.path.join(ROOT, 'scripts', 'restore.sh'), out, target], env=env, check=True, capture_output=True)

            def snapshot(db):
                q = (f"SELECT (SELECT COUNT(*) FROM {db}.Ticket), (SELECT COUNT(*) FROM {db}.Passenger), (SELECT COUNT(*) FROM {db}.TicketAudit),"
                     f" (SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='{db}' AND table_type='BASE TABLE'),"
                     f" (SELECT COUNT(*) FROM information_schema.views WHERE table_schema='{db}'),"
                     f" (SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema='{db}'),"
                     f" (SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema='{db}'),"
                     f" (SELECT COUNT(*) FROM information_schema.events WHERE event_schema='{db}'), (SELECT COUNT(*) FROM {db}.Seat)")
                env2 = dict(os.environ, MYSQL_PWD=os.environ.get('MYSQL_ADMIN_PASSWORD', ''))
                return subprocess.run(['mysql', '-N', '-u', os.environ.get('MYSQL_ADMIN_USER', 'root'), '-e', q],
                                      env=env2, check=True, capture_output=True, text=True).stdout
            self.assertEqual(snapshot(TEST_DB), snapshot(target))
        finally:
            mysql(f'DROP DATABASE IF EXISTS {target}')


class LargeDataTests(Base):
    """The sample-data loader fills a database with thousands of rows that obey every rule, and the app still copes."""
    DB = 'routewise_large'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(os.path.join(ROOT, 'schema.sql')) as f:
            schema = f.read().replace('public_transport', cls.DB)
        mysql(f'DROP DATABASE IF EXISTS {cls.DB}')
        mysql(stdin=schema)
        cls.env = dict(os.environ, DB_NAME=cls.DB)
        cls.load = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'load_sample_data.py'), '--scale', '0.15'],
                                  env=cls.env, capture_output=True, text=True, check=True)
        cls.original_db = cls.appmod.db_config['database']
        cls.appmod.db_config['database'] = cls.DB

    @classmethod
    def tearDownClass(cls):
        cls.appmod.db_config['database'] = cls.original_db
        mysql(f'DROP DATABASE IF EXISTS {cls.DB}')

    def count(self, sql):
        return self.appmod.query(sql)[0]['n']

    def rows(self, html):
        return html.count('<tr') - 1                                              # minus the header row

    def test_loader_adds_thousands_of_rows(self):
        self.assertGreater(self.count('SELECT COUNT(*) n FROM Passenger'), 400)
        self.assertGreater(self.count('SELECT COUNT(*) n FROM Ticket'), 2000)
        self.assertGreater(self.count('SELECT COUNT(*) n FROM Schedule'), 80)
        self.assertGreater(self.count('SELECT COUNT(*) n FROM Feedback'), 200)
        self.assertIn('Database now holds', self.load.stdout)

    def test_loaded_data_obeys_every_rule(self):
        self.assertEqual(self.count('SELECT COUNT(*) n FROM TripOccupancy WHERE SeatsLeft < 0'), 0)                 # no overbooking
        self.assertEqual(self.count('SELECT COUNT(*) n FROM (SELECT ScheduleID, TravelDate, SeatNo FROM Ticket GROUP BY 1, 2, 3 HAVING COUNT(*) > 1) d'), 0)
        self.assertEqual(self.count('SELECT COUNT(*) n FROM Schedule a JOIN Schedule b ON a.VehicleID = b.VehicleID AND a.ScheduleID < b.ScheduleID '
                                    'AND a.DepartureTime < b.ArrivalTime AND a.ArrivalTime > b.DepartureTime'), 0)    # no overlapping runs
        self.assertEqual(self.count("SELECT COUNT(*) n FROM Ticket T JOIN Schedule S USING (ScheduleID) JOIN Vehicle V USING (VehicleID) "
                                    "WHERE V.Status <> 'Active'"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) n FROM Ticket T WHERE NOT EXISTS "
                                    "(SELECT 1 FROM TicketAudit A WHERE A.TicketID = T.TicketID AND A.Action = 'BOOKED')"), 0)   # every ticket audited
        self.assertEqual(self.count("SELECT COUNT(*) n FROM Ticket T JOIN Schedule S USING (ScheduleID) WHERE NOT EXISTS "
                                    "(SELECT 1 FROM Seat ST WHERE ST.VehicleID = S.VehicleID AND ST.SeatNo = T.SeatNo)"), 0)   # real seats only
        self.assertEqual(self.count('SELECT COUNT(*) n FROM Ticket WHERE RunsOn(ScheduleID, TravelDate) = FALSE'), 0)        # only on service days
        self.assertGreater(self.count('SELECT COUNT(*) n FROM Schedule S WHERE NOT EXISTS '
                                      '(SELECT 1 FROM ScheduleDay D WHERE D.ScheduleID = S.ScheduleID AND D.Weekday IN (1, 7))'), 5)
        self.assertEqual(self.count('SELECT (SELECT SUM(Amount) FROM Payment) = (SELECT SUM(Fare) FROM Ticket) n'), 1)   # every ticket paid

    def test_loader_is_idempotent(self):
        before = self.count('SELECT COUNT(*) n FROM Ticket')
        again = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'load_sample_data.py'), '--scale', '0.15'],
                               env=self.env, capture_output=True, text=True, check=True)
        self.assertIn('already loaded', again.stdout)
        self.assertEqual(self.count('SELECT COUNT(*) n FROM Ticket'), before)

    def test_long_lists_are_paginated_and_small(self):
        self.login('admin')
        html = self.client.get('/bookings').get_data(as_text=True)
        self.assertEqual(self.rows(html), 50)
        self.assertIn('Showing 1–50 of', html)
        self.assertLess(len(html), 700_000)                                       # was tens of megabytes without pagination
        second = self.client.get('/bookings?page=2').get_data(as_text=True)
        self.assertNotEqual(re.findall(r'<td>#(\d+)</td>', html)[0], re.findall(r'<td>#(\d+)</td>', second)[0])
        self.assertIn('Showing 51–100 of', second)
        passengers = self.client.get('/passengers').get_data(as_text=True)
        self.assertEqual(self.rows(passengers), 50)
        self.assertLess(len(passengers), 200_000)

    def test_search_filters_on_the_server_and_pages_are_clamped(self):
        self.login('admin')
        name = self.appmod.query('SELECT Name FROM Passenger WHERE Email LIKE %s LIMIT 1', ('%@demo.example',))[0]['Name']
        hit = self.client.get('/passengers', query_string={'q': name}).get_data(as_text=True)
        self.assertIn(name, hit)
        self.assertLess(self.rows(hit), 50)                                       # narrowed, not the whole directory
        none = self.client.get('/passengers?q=zzzz-nobody').get_data(as_text=True)
        self.assertEqual(self.rows(none), 1)                                      # only the "no rows" line
        self.assertEqual(self.client.get('/bookings?page=99999').status_code, 200)   # clamped to the last page
        self.assertEqual(self.client.get('/bookings?page=abc').status_code, 200)     # junk page number ignored
        self.assertIn('%', 'x%')                                                  # LIKE wildcards are bound parameters, never SQL text
        self.assertEqual(self.client.get('/bookings', query_string={'q': "x' OR '1'='1"}).status_code, 200)

    def test_reports_stay_short_on_large_data(self):
        self.login('admin')
        html = self.client.get('/reports').get_data(as_text=True)
        self.assertLess(self.rows(html), 250)                                     # six thousand rows before the LIMITs
        self.assertLess(len(html), 150_000)
        self.assertEqual(self.client.get('/').status_code, 200)


class TripTests(Base):
    """A seat belongs to a trip: a schedule on a travel date."""

    def setUp(self):
        super().setUp()
        self.login('admin')
        self.today = date.today()

    def book(self, p, s, seat, day=None, fare=''):
        data = dict(p_id=p, s_id=s, seat=seat, fare=fare)
        if day:
            data['travel_date'] = day.isoformat()
        return self.post('/book', **data)

    def new_schedule(self, vehicle_no, capacity, departure, arrival, route=1):
        self.post('/vehicles/add', vehicle_no=vehicle_no, type='Bus', capacity=capacity, status='Active')
        vid = self.appmod.query('SELECT VehicleID v FROM Vehicle WHERE VehicleNo = %s', (vehicle_no,))[0]['v']
        self.post('/schedules/add', route_id=route, vehicle_id=vid, departure=departure, arrival=arrival)
        return self.appmod.query('SELECT MAX(ScheduleID) m FROM Schedule')[0]['m']

    def test_same_seat_on_another_day_is_free_but_not_twice_on_one_day(self):
        self.assertIn('Ticket booked', self.book(1, 2, '10A'))
        self.assertIn('Ticket booked', self.book(2, 2, '10A', self.today + timedelta(days=1)))      # same seat, tomorrow
        self.assertIn('Seat already booked', self.book(3, 2, '10A'))                                # same seat, same day
        self.assertIn('Seat already booked', self.book(3, 2, '10A', self.today + timedelta(days=1)))

    def test_past_and_too_distant_dates_are_refused(self):
        self.assertIn('in the past', self.book(1, 2, '10B', self.today - timedelta(days=1)))
        self.assertIn('at most 30 days ahead', self.book(1, 2, '10B', self.today + timedelta(days=31)))
        self.assertIn('Ticket booked', self.book(1, 2, '10B', self.today + timedelta(days=30)))
        self.assertIn('not a valid date', self.post('/book', p_id=1, s_id=2, seat='10C', fare='', travel_date='2026-13-45'))

    def test_capacity_is_counted_per_trip(self):
        sid = self.new_schedule('KA-00-TRIP', 2, '02:10', '02:40')
        tomorrow = self.today + timedelta(days=1)
        self.assertIn('Ticket booked', self.book(1, sid, '1A'))
        self.assertIn('Ticket booked', self.book(2, sid, '1B'))
        self.assertIn('Vehicle is full', self.book(3, sid, '1C'))                                   # today's trip is full ...
        self.assertIn('Ticket booked', self.book(3, sid, '1A', tomorrow))                           # ... tomorrow's is empty
        rows = self.appmod.query('SELECT TravelDate, Booked, SeatsLeft FROM TripOccupancy WHERE ScheduleID = %s ORDER BY TravelDate', (sid,))
        self.assertEqual([(r['Booked'], r['SeatsLeft']) for r in rows], [(2, 0), (1, 1)])

    def test_seats_left_api_and_function_use_the_chosen_date(self):
        sid = self.new_schedule('KA-00-API', 3, '02:50', '03:20')
        self.book(1, sid, '1A')
        today = self.client.get(f'/api/seats?s={sid}&d={self.today.isoformat()}').get_json()
        tomorrow = self.client.get(f'/api/seats?s={sid}&d={(self.today + timedelta(days=1)).isoformat()}').get_json()
        self.assertEqual((today['left'], today['capacity']), (2, 3))
        self.assertEqual(tomorrow['left'], 3)
        self.assertIsNone(self.client.get('/api/seats?s=x&d=nonsense').get_json()['left'])
        self.assertEqual([(x['no'], x['taken']) for x in today['seats']], [('1A', True), ('1B', False), ('1C', False)])

    def test_booking_form_and_list_show_travel_dates(self):
        html = self.client.get('/bookings').get_data(as_text=True)
        self.assertIn(f'value="{self.today.isoformat()}"', html)
        self.assertIn('name="travel_date"', html)
        self.assertIn('<th>Travels</th>', html)
        self.assertIn('TravelDate', self.client.get('/bookings/export.csv').get_data(as_text=True).splitlines()[0])

    def test_transfer_can_move_the_ticket_to_another_day(self):
        self.book(5, 8, '7B')
        tid = self.appmod.query("SELECT TicketID FROM Ticket WHERE ScheduleID = 8 AND SeatNo = '7B' AND TravelDate = CURDATE()")[0]['TicketID']
        tomorrow = self.today + timedelta(days=1)
        self.assertIn('moved to schedule', self.post(f'/transfer/{tid}', s_id=2, seat='10C', travel_date=tomorrow.isoformat()))
        row = self.appmod.query("SELECT TravelDate, ScheduleID FROM Ticket WHERE PassengerID = 5 AND SeatNo = '10C'")[0]
        self.assertEqual((row['TravelDate'], row['ScheduleID']), (tomorrow, 2))
        tid2 = self.appmod.query("SELECT TicketID FROM Ticket WHERE ScheduleID = 2 AND SeatNo = '10C'")[0]['TicketID']
        self.assertIn('in the past', self.post(f'/transfer/{tid2}', s_id=2, seat='10D', travel_date=(self.today - timedelta(days=2)).isoformat()))
        self.assertIsNotNone(self.appmod.query('SELECT 1 x FROM Ticket WHERE TicketID = %s', (tid2,)))


class ReferenceDataTests(Base):
    """Lookup tables, type consistency, data-driven fares and the audit actor."""

    def setUp(self):
        super().setUp()
        self.login('admin')

    def test_unknown_card_and_vehicle_types_are_refused(self):
        self.assertIn('Invalid reference', self.post('/passengers/add', name='Plat Inum', email='plat@x.com', card_type='Platinum'))
        self.assertIn('Invalid reference', self.post('/vehicles/add', vehicle_no='KA-00-TRAM', type='Tram', capacity=10))
        self.assertNotIn('Tram', self.client.get('/fleet').get_data(as_text=True))                  # the form offers only known types

    def test_vehicle_must_match_the_route_type(self):
        self.post('/vehicles/add', vehicle_no='KA-00-MIX', type='Bus', capacity=10, status='Active')
        vid = self.appmod.query("SELECT VehicleID v FROM Vehicle WHERE VehicleNo = 'KA-00-MIX'")[0]['v']
        out = self.post('/schedules/add', route_id=3, vehicle_id=vid, departure='04:00', arrival='04:30')   # route 3 is a metro line
        self.assertIn('does not match the route type', out)
        self.assertIn('Schedule added', self.post('/schedules/add', route_id=1, vehicle_id=vid, departure='04:00', arrival='04:30'))

    def test_changing_a_fare_rule_changes_new_fares_immediately(self):
        try:
            self.assertIn('updated', self.post('/fares/update', kind='route', key='Bus', rate='2.00', min_fare='10'))
            self.assertEqual(self.client.get('/api/fare?p=5&s=1').get_json()['fare'], 51.0)           # 25.5 km x 2.00, no card
            self.assertIn('updated', self.post('/fares/update', kind='card', key='Weekly', pct='50'))
            self.assertEqual(self.client.get('/api/fare?p=2&s=1').get_json()['fare'], 25.5)           # 51.00 less 50 %
        finally:
            self.post('/fares/update', kind='route', key='Bus', rate='1.50', min_fare='10')
            self.post('/fares/update', kind='card', key='Weekly', pct='20')
        self.assertEqual(self.client.get('/api/fare?p=2&s=1').get_json()['fare'], 30.6)

    def test_fare_rules_are_validated_and_admin_only(self):
        self.assertIn('valid numbers', self.post('/fares/update', kind='route', key='Bus', rate='-1', min_fare='10'))
        self.assertIn('valid numbers', self.post('/fares/update', kind='card', key='Weekly', pct='95'))
        self.assertIn('valid numbers', self.post('/fares/update', kind='route', key='Bus', rate='nan', min_fare='10'))
        self.client = self.appmod.app.test_client()
        self.client.post('/login', data={'username': 'staff', 'password': 'staff123', 'csrf_token': self.token()})
        self.assertIn('not allowed', self.post('/fares/update', kind='route', key='Bus', rate='9', min_fare='10'))
        self.assertEqual(self.client.get('/api/fare?p=5&s=1').get_json()['fare'], 38.25)

    def test_audit_trail_records_who_acted(self):
        self.client = self.appmod.app.test_client()
        self.client.post('/login', data={'username': 'staff', 'password': 'staff123', 'csrf_token': self.token()})
        self.post('/book', p_id=7, s_id=8, seat='7C', fare='')
        tid = self.appmod.query("SELECT TicketID FROM Ticket WHERE ScheduleID = 8 AND SeatNo = '7C'")[0]['TicketID']
        self.client = self.appmod.app.test_client()
        self.client.post('/login', data={'username': 'admin', 'password': 'admin123', 'csrf_token': self.token()})
        self.post(f'/cancel/{tid}')
        rows = self.appmod.query('SELECT Action, Actor, TravelDate FROM TicketAudit WHERE TicketID = %s ORDER BY AuditID', (tid,))
        self.assertEqual([(r['Action'], r['Actor']) for r in rows], [('BOOKED', 'staff'), ('CANCELLED', 'admin')])
        self.assertEqual(rows[0]['TravelDate'], date.today())

    def test_schema_version_is_recorded(self):
        self.assertEqual(self.appmod.query('SELECT MAX(Version) v FROM SchemaVersion')[0]['v'], 5)


class AccountSecurityTests(Base):
    """Own-password change and the sign-in attempt limit."""

    def make_user(self, name, password='initial-pass1', role='staff'):
        self.client = self.appmod.app.test_client()
        self.login('admin')
        self.post('/users/add', username=name, full_name=name, password=password, role=role)

    def sign_in(self, name, password):
        self.client = self.appmod.app.test_client()
        return self.client.post('/login', data={'username': name, 'password': password, 'csrf_token': self.token()})

    def test_any_user_can_change_their_own_password(self):
        self.make_user('pwuser')
        self.sign_in('pwuser', 'initial-pass1')
        self.assertIn('Password changed', self.post('/account/password', current='initial-pass1', new='better-pass22', confirm='better-pass22'))
        self.assertEqual(self.sign_in('pwuser', 'initial-pass1').status_code, 200)                 # old password no longer works (form again)
        self.assertEqual(self.sign_in('pwuser', 'better-pass22').status_code, 302)                 # the new one does
        self.assertEqual(self.client.get('/account').status_code, 200)

    def test_password_change_is_validated(self):
        self.make_user('pwcheck')
        self.sign_in('pwcheck', 'initial-pass1')
        self.assertIn('current password is not correct', self.post('/account/password', current='nope', new='better-pass22', confirm='better-pass22'))
        self.assertIn('at least 8', self.post('/account/password', current='initial-pass1', new='short', confirm='short'))
        self.assertIn('do not match', self.post('/account/password', current='initial-pass1', new='better-pass22', confirm='other-pass22'))
        self.assertIn('different from the current', self.post('/account/password', current='initial-pass1', new='initial-pass1', confirm='initial-pass1'))
        self.assertEqual(self.sign_in('pwcheck', 'initial-pass1').status_code, 302)                # nothing changed

    def test_viewer_can_change_own_password_but_account_needs_login(self):
        self.make_user('pwviewer', role='viewer')
        self.sign_in('pwviewer', 'initial-pass1')
        self.assertIn('Password changed', self.post('/account/password', current='initial-pass1', new='viewer-pass22', confirm='viewer-pass22'))
        anonymous = self.appmod.app.test_client()
        self.assertEqual(anonymous.get('/account').status_code, 302)

    def test_repeated_wrong_passwords_lock_the_account_for_a_while(self):
        self.make_user('lockme')
        try:
            for _ in range(self.appmod.app.config['LOGIN_MAX_FAILURES']):
                self.assertEqual(self.sign_in('lockme', 'wrong-password').status_code, 200)
            locked = self.sign_in('lockme', 'initial-pass1')                                       # even the CORRECT password is refused now
            self.assertEqual(locked.status_code, 429)
            self.assertIn('Too many failed sign-in attempts', locked.get_data(as_text=True))
            self.assertEqual(self.sign_in('admin', 'admin123').status_code, 302)                   # other users are not affected
            # the count lives in the database, so it survives a restart and is shared by every server process
            row = self.appmod.query("SELECT Failures FROM LoginAttempt WHERE Username = 'lockme'")[0]
            self.assertEqual(row['Failures'], self.appmod.app.config['LOGIN_MAX_FAILURES'])
        finally:
            self.appmod.execute("DELETE FROM LoginAttempt WHERE Username = 'lockme'")                # what the lock expiring does
        self.assertEqual(self.sign_in('lockme', 'initial-pass1').status_code, 302)
        self.assertEqual(self.appmod.query("SELECT COUNT(*) n FROM LoginAttempt WHERE Username = 'lockme'")[0]['n'], 0)   # cleared on success

    def test_a_failure_after_the_window_starts_a_new_count(self):
        self.make_user('slowpoke')
        for _ in range(4):
            self.sign_in('slowpoke', 'wrong-password')
        mysql(f"UPDATE {TEST_DB}.LoginAttempt SET FirstFailure = NOW() - INTERVAL 11 MINUTE WHERE Username = 'slowpoke'")
        self.sign_in('slowpoke', 'wrong-password')
        self.assertEqual(self.appmod.query("SELECT Failures FROM LoginAttempt WHERE Username = 'slowpoke'")[0]['Failures'], 1)
        self.assertEqual(self.sign_in('slowpoke', 'initial-pass1').status_code, 302)

    def test_demo_passwords_are_flagged_and_cannot_be_chosen_again(self):
        self.login('staff')
        self.assertIn('published demo password', self.client.get('/').get_data(as_text=True))
        self.assertIn('demo passwords', self.post('/account/password', current='staff123', new='admin123', confirm='admin123'))
        self.make_user('notdemo')
        self.sign_in('notdemo', 'initial-pass1')
        self.assertNotIn('published demo password', self.client.get('/').get_data(as_text=True))

    def test_production_server_serves_the_application(self):
        """Runs the exact command from the README (python -m waitress ... app:app) as a separate process."""
        import socket
        import urllib.request
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen([sys.executable, '-m', 'waitress', f'--listen=127.0.0.1:{port}', 'app:app'], cwd=ROOT,
                                env=dict(os.environ, DB_NAME=TEST_DB), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            body = None
            for _ in range(50):
                try:
                    body = urllib.request.urlopen(f'http://127.0.0.1:{port}/login', timeout=2).read().decode()
                    break
                except OSError:
                    time.sleep(0.2)
            self.assertIsNotNone(body, 'the production server did not start')
            self.assertIn('RouteWise', body)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


class MigrationTests(unittest.TestCase):
    """Upgrading a version-1 database with scripts/migrate.py gives exactly a fresh install and keeps the data."""
    OLD, NEW = 'routewise_mig_old', 'routewise_mig_new'
    SIGNATURE = (
        "SELECT 'col', CONCAT_WS('|', table_name, ordinal_position, column_name, column_type, is_nullable, IFNULL(column_default,'-'), extra) FROM information_schema.columns WHERE table_schema = DATABASE() "
        "UNION ALL SELECT 'idx', CONCAT_WS('|', table_name, index_name, non_unique, index_type, GROUP_CONCAT(column_name ORDER BY seq_in_index)) FROM information_schema.statistics WHERE table_schema = DATABASE() GROUP BY table_name, index_name, non_unique, index_type "
        "UNION ALL SELECT 'chk', CONCAT_WS('|', tc.table_name, tc.constraint_name, cc.check_clause) FROM information_schema.table_constraints tc JOIN information_schema.check_constraints cc ON cc.constraint_schema = tc.constraint_schema AND cc.constraint_name = tc.constraint_name WHERE tc.table_schema = DATABASE() AND tc.constraint_type = 'CHECK' "
        "UNION ALL SELECT 'fk', CONCAT_WS('|', table_name, constraint_name, referenced_table_name, delete_rule) FROM information_schema.referential_constraints WHERE constraint_schema = DATABASE() "
        "UNION ALL SELECT 'trg', CONCAT_WS('|', trigger_name, event_object_table, action_timing, event_manipulation, action_order, action_statement) FROM information_schema.triggers WHERE trigger_schema = DATABASE() "
        "UNION ALL SELECT 'rtn', CONCAT_WS('|', routine_name, routine_type, security_type, routine_definition) FROM information_schema.routines WHERE routine_schema = DATABASE() "
        "UNION ALL SELECT 'part', CONCAT_WS('|', table_name, partition_name, partition_ordinal_position, partition_method, partition_expression, partition_description) FROM information_schema.partitions WHERE table_schema = DATABASE() AND partition_name IS NOT NULL "
        "UNION ALL SELECT 'evt', CONCAT_WS('|', event_name, interval_value, interval_field, event_definition) FROM information_schema.events WHERE event_schema = DATABASE() "
        "UNION ALL SELECT 'view', CONCAT_WS('|', table_name, view_definition) FROM information_schema.views WHERE table_schema = DATABASE() ORDER BY 1, 2")

    def setUp(self):
        self.env = dict(os.environ, MYSQL_PWD=os.environ.get('MYSQL_ADMIN_PASSWORD', ''), DB_NAME=self.OLD)
        for db in (self.OLD, self.NEW):
            mysql(f'DROP DATABASE IF EXISTS {db}')

    def tearDown(self):
        for db in (self.OLD, self.NEW):
            mysql(f'DROP DATABASE IF EXISTS {db}')

    def scalar(self, db, sql):
        cmd = ['mysql', '-N', '-B', '-u', os.environ.get('MYSQL_ADMIN_USER', 'root'), db, '-e', sql]
        return subprocess.run(cmd, env=self.env, check=True, capture_output=True, text=True).stdout.strip()

    def migrate(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'migrate.py'), '--db', self.OLD, *args],
                              env=self.env, capture_output=True, text=True)

    def test_upgrade_equals_fresh_install_and_keeps_data(self):
        with open(os.path.join(ROOT, 'migrations', 'baseline_v1.sql')) as f:
            mysql(stdin=f.read().replace('public_transport', self.OLD))
        mysql(f"USE {self.OLD}; CALL BookTicket(8, 1, 'F1', NULL); CALL BookTicket(6, 2, 'F2', 50.00);")      # version-1 procedure: 4 arguments
        before = self.scalar(self.OLD, 'SELECT COUNT(*), SUM(Fare) FROM Ticket')
        self.assertIn('version 1', self.migrate('--status').stdout)
        done = self.migrate('--no-backup')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.scalar(self.OLD, 'SELECT COUNT(*), SUM(Fare) FROM Ticket'), before)                 # no ticket or fare lost
        self.assertEqual(self.scalar(self.OLD, 'SELECT COUNT(*) FROM Ticket WHERE TravelDate = DATE(BookingDate)'), before.split()[0])
        self.assertEqual(self.scalar(self.OLD, 'SELECT MAX(Version) FROM SchemaVersion'), '5')
        self.assertEqual(self.scalar(self.OLD, 'SELECT (SELECT SUM(Amount) FROM Payment) = (SELECT SUM(Fare) FROM Ticket)'), '1')   # ledger opened
        self.assertEqual(self.scalar(self.OLD, 'SELECT COUNT(*) FROM Ticket T JOIN Schedule S USING (ScheduleID) WHERE NOT EXISTS '
                                               '(SELECT 1 FROM Seat ST WHERE ST.VehicleID = S.VehicleID AND ST.SeatNo = T.SeatNo)'), '0')   # seats relabelled
        self.assertEqual(self.scalar(self.OLD, 'SELECT COUNT(*) FROM Vehicle V WHERE Capacity <> (SELECT COUNT(*) FROM Seat ST WHERE ST.VehicleID = V.VehicleID)'), '0')
        with open(os.path.join(ROOT, 'schema.sql')) as f:
            mysql(stdin=f.read().replace('public_transport', self.NEW))
        upgraded = self.scalar(self.OLD, self.SIGNATURE).replace(self.OLD, 'DB')
        fresh = self.scalar(self.NEW, self.SIGNATURE).replace(self.NEW, 'DB')
        self.assertGreater(len(fresh.splitlines()), 280)
        self.assertEqual(upgraded.splitlines(), fresh.splitlines())                                              # structure, constraints, triggers, routines, views
        again = self.migrate()
        self.assertIn('Up to date', again.stdout)
        self.assertEqual(self.scalar(self.OLD, 'SELECT COUNT(*) FROM Ticket'), before.split()[0])

    def test_migrate_refuses_an_empty_database(self):
        mysql(f'CREATE DATABASE {self.OLD}')
        result = self.migrate('--no-backup')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Import schema.sql first', result.stderr + result.stdout)


class ConcurrencyTests(Base):
    """Several sessions at once: transfers in opposite directions neither lose nor duplicate a ticket."""

    def test_opposite_transfers_never_lose_or_duplicate_tickets(self):
        import threading
        self.login('admin')
        group_a, group_b = range(1, 7), range(7, 13)
        seats_a, seats_b = ['8A', '8B', '8C', '9A', '9B', '9C'], ['8A', '8B', '8C', '8D', '9A', '9B']   # on schedules 8 and 9
        new_a, new_b = ['10A', '10B', '10C', '10D', '6A', '6B'], ['1A', '1B', '1C', '2A', '2B', '2C']   # free seats on 9 and 8
        for i, seat in zip(group_a, seats_a):
            self.post('/book', p_id=i, s_id=8, seat=seat, fare='')
        for i, seat in zip(group_b, seats_b):
            self.post('/book', p_id=i, s_id=9, seat=seat, fare='')
        ids = {r['PassengerID']: r['TicketID'] for r in self.appmod.query(
            "SELECT PassengerID, TicketID FROM Ticket WHERE TravelDate = CURDATE() AND "
            "((ScheduleID = 8 AND SeatNo IN ('8A','8B','8C','9A','9B','9C')) OR (ScheduleID = 9 AND SeatNo IN ('8A','8B','8C','8D','9A','9B')))")}
        self.assertEqual(len(ids), 12)
        barrier, errors = threading.Barrier(12), []

        def move(passenger, target, seat):
            barrier.wait()
            try:
                self.appmod.call('TransferTicket', [ids[passenger], target, seat, None])     # call() retries a deadlock victim
            except Exception as err:                                                         # noqa: BLE001
                errors.append(repr(err))
        threads = [threading.Thread(target=move, args=(i, 9, seat)) for i, seat in zip(group_a, new_a)] + \
                  [threading.Thread(target=move, args=(i, 8, seat)) for i, seat in zip(group_b, new_b)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        rows = self.appmod.query("SELECT PassengerID, ScheduleID FROM Ticket WHERE TravelDate = CURDATE() AND "
                                 "((ScheduleID = 9 AND SeatNo IN ('10A','10B','10C','10D','6A','6B')) OR (ScheduleID = 8 AND SeatNo IN ('1A','1B','1C','2A','2B','2C')))")
        self.assertEqual(sorted(r['PassengerID'] for r in rows), list(range(1, 13)))         # exactly one ticket each ...
        self.assertTrue(all((r['ScheduleID'] == 9) == (r['PassengerID'] <= 6) for r in rows))  # ... on the other departure
        self.assertEqual(self.appmod.query('SELECT COUNT(*) n FROM Ticket WHERE TicketID IN (%s)' % ','.join(map(str, ids.values())))[0]['n'], 0)


class IsolationDemoTests(unittest.TestCase):
    def test_demo_script_shows_the_documented_effects(self):
        import json
        out = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'isolation_demo.py'), '--json'], capture_output=True, text=True,
                             env=dict(os.environ, MYSQL_PWD=os.environ.get('MYSQL_ADMIN_PASSWORD', '')), check=True).stdout
        r = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(r['non_repeatable_read'], {'READ COMMITTED': [10, 200], 'REPEATABLE READ': [10, 10]})
        self.assertEqual(r['phantom'], {'READ COMMITTED': [1, 2], 'REPEATABLE READ': [1, 1]})
        self.assertEqual(r['lost_update'], {'unsafe': 11, 'lock': 12, 'atomic': 12})        # the lost update, and the two cures


class OperationsToolsTests(Base):
    def test_report_cache_reuses_results_and_can_be_refreshed(self):
        self.login('admin')
        config, cache = self.appmod.app.config, self.appmod.reports._report_cache
        config['REPORT_CACHE_SECONDS'] = 60
        try:
            cache.update(results=None)
            first = self.client.get('/reports').get_data(as_text=True)
            self.assertIn('kept for 60 s', first)
            cached_at = cache['at']
            self.client.get('/reports')
            self.assertEqual(cache['at'], cached_at)                                      # second view reused the results
            self.client.get('/reports?refresh=1')
            self.assertGreater(cache['at'], cached_at)                                    # Refresh recalculated them
        finally:
            config['REPORT_CACHE_SECONDS'] = 0

    def test_report_timing_script_runs(self):
        out = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'report_timings.py'), '--runs', '1'], capture_output=True,
                             text=True, env=dict(os.environ, DB_NAME=TEST_DB), check=True).stdout
        self.assertIn('All 14 reports together', out)
        self.assertIn('Running revenue per route', out)


class ReportTests(Base):
    def test_every_report_query_runs(self):
        self.login()
        html = self.client.get('/reports').get_data(as_text=True)
        for r in self.appmod.REPORTS:
            self.assertIn(r['title'], html)

    def test_dashboard_numbers(self):
        self.login()
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('Tickets sold', html)

class SeatMapTests(Base):
    """Every vehicle has a seat map; a ticket must name a real seat; the map follows capacity and layout changes."""

    def setUp(self):
        super().setUp()
        self.login('admin')

    def vehicle(self, number, capacity, per_row=4):
        self.post('/vehicles/add', vehicle_no=number, type='Bus', capacity=capacity, seats_per_row=per_row, status='Active')
        return self.appmod.query('SELECT VehicleID v FROM Vehicle WHERE VehicleNo = %s', (number,))[0]['v']

    def seats(self, vid):
        return [r['SeatNo'] for r in self.appmod.query('SELECT SeatNo FROM Seat WHERE VehicleID = %s ORDER BY LENGTH(SeatNo), SeatNo', (vid,))]

    def test_every_vehicle_has_exactly_its_capacity_in_seats(self):
        self.assertEqual(self.appmod.query('SELECT COUNT(*) n FROM Vehicle V WHERE Capacity <> '
                                           '(SELECT COUNT(*) FROM Seat ST WHERE ST.VehicleID = V.VehicleID)')[0]['n'], 0)
        row1 = self.appmod.query("SELECT SeatNo, Position FROM Seat WHERE VehicleID = 4 AND SeatNo LIKE '1_' ORDER BY SeatNo")
        self.assertEqual([(r['SeatNo'], r['Position']) for r in row1],                  # a six-across metro car
                         [('1A', 'Window'), ('1B', 'Middle'), ('1C', 'Aisle'), ('1D', 'Aisle'), ('1E', 'Middle'), ('1F', 'Window')])
        self.assertEqual(self.seats(self.vehicle('KA-00-SM1', 6, 4)), ['1A', '1B', '1C', '1D', '2A', '2B'])

    def test_a_ticket_must_name_a_seat_of_the_vehicle(self):
        self.assertIn('No such seat on this vehicle', self.post('/book', p_id=1, s_id=2, seat='13A', fare=''))   # 45 seats: 12A is the last
        self.assertIn('No such seat on this vehicle', self.post('/book', p_id=1, s_id=8, seat='1D', fare=''))    # three across: no D
        self.assertIn('No such seat on this vehicle', self.post('/book', p_id=1, s_id=2, seat='A1', fare=''))    # not a seat-map label
        self.assertIn('Ticket booked', self.post('/book', p_id=1, s_id=2, seat='12A', fare=''))

    def test_seat_map_api_marks_taken_seats(self):
        x = self.client.get(f'/api/seats?s=1&d={date.today().isoformat()}').get_json()
        self.assertEqual((x['capacity'], x['per_row'], len(x['seats'])), (40, 4, 40))
        taken = {s['no'] for s in x['seats'] if s['taken']}
        self.assertTrue({'1A', '1B', '2A', '3A'} <= taken)                              # the seed bookings
        self.assertEqual(x['left'], 40 - len(taken))

    def test_resizing_rebuilds_the_map_but_never_drops_a_booked_seat(self):
        vid = self.vehicle('KA-00-SM2', 8)
        self.post('/schedules/add', route_id=1, vehicle_id=vid, departure='00:10', arrival='00:40')
        sid = self.appmod.query('SELECT MAX(ScheduleID) m FROM Schedule')[0]['m']
        self.assertIn('Ticket booked', self.post('/book', p_id=2, s_id=sid, seat='2D', fare=''))
        edit = dict(vehicle_no='KA-00-SM2', type='Bus', model='', status='Active', driver_id='')
        self.assertIn('would disappear', self.post(f'/vehicles/{vid}/edit', capacity=6, seats_per_row=4, **edit))      # 2D is booked
        self.assertIn('would disappear', self.post(f'/vehicles/{vid}/edit', capacity=8, seats_per_row=2, **edit))      # two across: no D
        self.assertEqual(len(self.seats(vid)), 8)                                        # the whole UPDATE was undone
        self.assertIn('Vehicle updated', self.post(f'/vehicles/{vid}/edit', capacity=12, seats_per_row=4, **edit))
        self.assertEqual(self.seats(vid)[-1], '3D')

    def test_seats_disappear_with_their_vehicle(self):
        vid = self.vehicle('KA-00-SM3', 5)
        self.post(f'/vehicles/{vid}/delete')
        self.assertEqual(self.seats(vid), [])                                            # ON DELETE CASCADE

    def test_seat_position_report_adds_up(self):
        r = next(x for x in self.appmod.REPORTS if x['title'].startswith('Window, aisle'))
        rows = self.appmod.query(r['sql'])
        self.assertEqual({x['Position'] for x in rows} <= {'Window', 'Aisle', 'Middle'}, True)
        self.assertAlmostEqual(sum(float(x['SharePct']) for x in rows), 100, delta=0.3)
        self.assertEqual(sum(x['Tickets'] for x in rows), self.appmod.query('SELECT COUNT(*) n FROM Ticket')[0]['n'])


class ServiceCalendarTests(Base):
    """Departures run on their service days only; some do not run on public holidays."""

    def setUp(self):
        super().setUp()
        self.login('admin')

    def next_day(self, weekday, after=1):
        """The next date with this weekday (0 = Monday) that is not already a public holiday."""
        holidays = {r['HolidayDate'] for r in self.appmod.query('SELECT HolidayDate FROM Holiday')}
        d = date.today() + timedelta(days=after)
        while d.weekday() != weekday or d in holidays:
            d += timedelta(days=1)
        return d

    def book(self, schedule, seat, day):
        return self.post('/book', p_id=4, s_id=schedule, seat=seat, fare='', travel_date=day.isoformat())

    def test_weekday_service_is_not_sold_for_a_weekend(self):
        saturday, monday = self.next_day(5), self.next_day(0)
        self.assertIn('does not run on that date', self.book(11, '1A', saturday))      # 11 runs Monday to Friday
        self.assertIn('Ticket booked', self.book(11, '1A', monday))
        self.assertIn('Ticket booked', self.book(12, '1A', saturday))                  # 12 runs only at weekends
        self.assertIn('does not run on that date', self.book(12, '1B', monday))

    def test_public_holiday_stops_only_the_services_that_skip_holidays(self):
        tuesday = self.next_day(1)
        try:
            self.assertIn('added as a public holiday', self.post('/holidays/add', holiday_date=tuesday.isoformat(), name='Test Day'))
            self.assertIn('does not run on that date', self.book(11, '2A', tuesday))   # weekday service, not on holidays
            self.assertIn('Ticket booked', self.book(2, '11A', tuesday))                # a daily service runs on holidays too
            api = self.client.get(f'/api/seats?s=11&d={tuesday.isoformat()}').get_json()
            self.assertFalse(api['runs'])
        finally:
            self.post(f'/holidays/{tuesday.isoformat()}/delete')
        self.assertIn('Ticket booked', self.book(11, '2A', tuesday))

    def test_runs_on_function_and_view_agree_with_the_calendar(self):
        today = date.today()
        expected = {11: today.weekday() < 5, 12: today.weekday() >= 5}
        holiday = self.appmod.query('SELECT COUNT(*) n FROM Holiday WHERE HolidayDate = CURDATE()')[0]['n'] > 0
        expected[11] = expected[11] and not holiday
        rows = {r['ScheduleID']: bool(r['RunsToday']) for r in self.appmod.query('SELECT ScheduleID, RunsToday FROM ScheduleOccupancy WHERE ScheduleID IN (11, 12)')}
        self.assertEqual(rows, expected)
        self.assertIsNone(self.appmod.query('SELECT RunsOn(999999, CURDATE()) r')[0]['r'])

    def test_schedule_form_stores_service_days(self):
        self.post('/vehicles/add', vehicle_no='KA-00-SVC', type='Bus', capacity=10, status='Active')
        vid = self.appmod.query("SELECT VehicleID v FROM Vehicle WHERE VehicleNo = 'KA-00-SVC'")[0]['v']
        self.assertIn('Schedule added', self.post('/schedules/add', route_id=2, vehicle_id=vid, departure='21:00', arrival='21:40', days=['Sat', 'Sun']))
        sid = self.appmod.query('SELECT ScheduleID, RunsOnHolidays FROM Schedule WHERE VehicleID = %s', (vid,))[0]
        days = [r['Weekday'] for r in self.appmod.query('SELECT Weekday FROM ScheduleDay WHERE ScheduleID = %s ORDER BY Weekday', (sid['ScheduleID'],))]
        self.assertEqual((days, sid['RunsOnHolidays']), ([1, 7], 0))                          # Sunday and Saturday rows
        self.assertEqual(self.appmod.query('SELECT ServiceDays FROM ScheduleOccupancy WHERE ScheduleID = %s', (sid['ScheduleID'],))[0]['ServiceDays'], 'Sat,Sun')
        self.assertIn('Weekends', self.client.get('/routes?q=KA-00-SVC').get_data(as_text=True))

    def test_holidays_are_admin_only_and_validated(self):
        self.assertIn('valid date', self.post('/holidays/add', holiday_date='not-a-date', name='X'))
        self.assertIn('Duplicate', self.post('/holidays/add', holiday_date='2026-12-25', name='Christmas again'))
        self.client = self.appmod.app.test_client()
        self.client.post('/login', data={'username': 'staff', 'password': 'staff123', 'csrf_token': self.token()})
        self.assertIn('not allowed', self.post('/holidays/add', holiday_date='2027-03-01', name='Nope'))


class SearchTests(Base):
    """The search boxes use the FULLTEXT and B-tree indexes instead of scanning with LIKE '%...%'."""

    def setUp(self):
        super().setUp()
        self.login('admin')

    def test_passenger_search_by_word_email_phone_and_number(self):
        for q in ('Thanu', 'thanu@gmail.com', 'sanv', '9000000001'):
            html = self.client.get('/passengers', query_string={'q': q}).get_data(as_text=True)
            self.assertTrue('B M Thanu Sri' in html or 'Sanvi S' in html, q)
        self.assertIn('Akash V', self.client.get('/passengers?q=3').get_data(as_text=True))   # passenger number 3

    def test_booking_search_by_route_seat_vehicle_and_ticket(self):
        def routes(q):                                                                   # the Route column of the result table
            html = self.client.get('/bookings', query_string={'q': q}).get_data(as_text=True)
            return set(re.findall(r'<td class="fw-semibold">[^<]*</td><td>([^<]+)</td>', html))
        self.assertEqual(routes('Purple'), {'Purple Line'})                              # a route word
        self.assertEqual(routes('kempegowda'), {'Route 335E'})                           # a destination word
        self.assertTrue({'Route 201', 'Route 500', 'Purple Line'} <= routes('thanu'))    # a passenger word: her seed tickets
        html = self.client.get('/bookings?q=thanu').get_data(as_text=True)
        self.assertEqual(set(re.findall(r'<td class="fw-semibold">([^<]+)</td>', html)), {'B M Thanu Sri'})
        self.assertIn('KA-51-M-1002', self.client.get('/bookings?q=KA-51-M-1002').get_data(as_text=True))
        seats = re.findall(r'pill pill-blue">([^<]+)<', self.client.get('/bookings?q=1c').get_data(as_text=True))
        self.assertTrue(seats and set(seats) == {'1C'}, seats)

    def test_schedule_search_by_route_word(self):
        html = self.client.get('/routes?q=airport').get_data(as_text=True)
        self.assertIn('Route 335E', html)

    def test_the_plans_use_the_indexes(self):
        def plan(sql, args=()):
            return self.appmod.query('EXPLAIN FORMAT=TRADITIONAL ' + sql, args)
        ft = plan('SELECT * FROM Passenger WHERE MATCH(Name, Email) AGAINST (%s IN BOOLEAN MODE)', ('+thanu*',))[0]
        self.assertEqual((ft['type'], ft['key']), ('fulltext', 'ft_passenger'))
        scan = plan("SELECT * FROM Passenger WHERE Name LIKE '%thanu%'")[0]
        self.assertEqual(scan['type'], 'ALL')                                            # what the FULLTEXT index avoids
        phone = plan("SELECT * FROM Passenger WHERE Phone LIKE '90000%'")[0]
        self.assertIn('idx_passenger_phone', phone['possible_keys'])                     # (a 13-row table is cheaper to scan)

    def test_search_terms_are_built_safely(self):
        from routewise.search import boolean_query, prefix
        self.assertEqual(boolean_query('Ravi  kum'), '+Ravi* +kum*')
        self.assertEqual(boolean_query('a@b.com'), None)                                 # nothing searchable: falls back to LIKE
        self.assertEqual(boolean_query('x" -y +z (w) ~the'), None)                       # operators never reach MATCH
        self.assertEqual(prefix('50%_'), '50\\%\\_%')
        for q in ('"unbalanced', '+++', '*', 'O\'Brien'):
            self.assertEqual(self.client.get('/passengers', query_string={'q': q}).status_code, 200, q)


class EventTests(Base):
    def test_old_sign_in_failures_are_purged_by_a_scheduled_event(self):
        out = mysql(f"SELECT CONCAT_WS('|', EVENT_NAME, INTERVAL_VALUE, INTERVAL_FIELD, STATUS) FROM information_schema.events "
                    f"WHERE EVENT_SCHEMA = '{TEST_DB}'").stdout                         # (the application user cannot even see events)
        for event in ('PurgeLoginAttempts|1|HOUR|ENABLED', 'PurgeSeatHolds|1|MINUTE|ENABLED', 'MaintainAuditPartitions|1|MONTH|ENABLED'):
            self.assertIn(event, out)
        self.appmod.execute("INSERT INTO LoginAttempt VALUES ('old', '1.2.3.4', 3, NOW() - INTERVAL 2 DAY), ('new', '1.2.3.4', 3, NOW())")
        body = mysql(f"SELECT EVENT_DEFINITION FROM information_schema.events WHERE EVENT_SCHEMA = '{TEST_DB}' AND EVENT_NAME = 'PurgeLoginAttempts'").stdout.splitlines()[-1]
        mysql(f'USE {TEST_DB}; {body}')                                                  # run exactly what the event runs every hour
        left = [r['Username'] for r in self.appmod.query("SELECT Username FROM LoginAttempt WHERE Address = '1.2.3.4'")]
        self.assertEqual(left, ['new'])


class SeatHoldTests(Base):
    """Clicking a seat holds it for a few minutes; a colleague cannot sell it meanwhile; booking or expiry releases it."""

    def as_user(self, name):
        self.client = self.appmod.app.test_client()
        self.login(name)

    def hold(self, seat, schedule=2, day=None):
        r = self.client.post('/api/hold', data={'s_id': schedule, 'seat': seat, 'travel_date': (day or date.today()).isoformat(),
                                                'csrf_token': self.token('/')})
        return r.status_code, (r.get_json() or {}).get('message', '')

    def holds(self, seat=None):
        sql = 'SELECT SeatNo, HeldBy FROM SeatHold' + (' WHERE ScheduleID = 2 AND SeatNo = %s' if seat else '')
        return self.appmod.query(sql, (seat,) if seat else ())

    def test_a_held_seat_cannot_be_sold_by_a_colleague_but_by_the_holder(self):
        self.as_user('staff')
        self.assertEqual(self.hold('6A'), (200, 'Seat 6A is held for you for 5 minutes.'))
        self.as_user('admin')
        self.assertIn('held by another clerk', self.post('/book', p_id=3, s_id=2, seat='6A', fare=''))
        self.assertEqual(self.hold('6A')[0], 409)                                       # nor hold it
        seats = self.client.get(f'/api/seats?s=2&d={date.today().isoformat()}').get_json()['seats']
        self.assertTrue(next(x for x in seats if x['no'] == '6A')['held'])
        self.as_user('staff')
        self.assertTrue(next(x for x in self.client.get(f'/api/seats?s=2&d={date.today().isoformat()}').get_json()['seats'] if x['no'] == '6A')['mine'])
        self.assertIn('Ticket booked', self.post('/book', p_id=3, s_id=2, seat='6A', fare=''))
        self.assertEqual(self.holds('6A'), [])                                           # the ReleaseHold trigger cleared it

    def test_one_hold_per_clerk_and_expired_holds_do_not_block(self):
        self.as_user('staff')
        self.hold('6B')
        self.hold('6C')                                                                  # picking another seat releases 6B
        self.assertEqual([(h['SeatNo'], h['HeldBy']) for h in self.holds() if h['HeldBy'] == 'staff'], [('6C', 'staff')])
        mysql(f"UPDATE {TEST_DB}.SeatHold SET ExpiresAt = NOW() - INTERVAL 1 MINUTE WHERE HeldBy = 'staff'")
        self.as_user('admin')
        self.assertIn('Ticket booked', self.post('/book', p_id=4, s_id=2, seat='6C', fare=''))   # an expired hold is no hold
        mysql(f"USE {TEST_DB}; INSERT INTO SeatHold VALUES (2, CURDATE(), '6D', 'nobody', NOW() - INTERVAL 1 MINUTE)")
        mysql(f"USE {TEST_DB}; DELETE FROM SeatHold WHERE ExpiresAt <= NOW()")              # what PurgeSeatHolds runs every minute
        self.assertEqual(self.holds('6D'), [])

    def test_holds_obey_the_same_rules_as_bookings(self):
        self.as_user('staff')
        self.assertIn('already booked', self.hold('1A')[1])                              # a seed booking
        self.assertIn('No such seat', self.hold('13A')[1])
        saturday = date.today() + timedelta(days=(5 - date.today().weekday()) % 7 or 7)
        self.assertIn('does not run', self.hold('1A', schedule=11, day=saturday)[1])
        self.assertEqual(self.client.post('/api/hold', data={'s_id': 2, 'seat': '7A', 'travel_date': 'x', 'csrf_token': self.token('/')}).status_code, 400)
        self.as_user('viewer')
        r = self.client.post('/api/hold', data={'s_id': 2, 'seat': '7A', 'travel_date': date.today().isoformat(), 'csrf_token': self.token('/')})
        self.assertEqual(r.status_code, 302)                                             # viewers may not hold seats
        self.assertEqual(self.holds('7A'), [])


class ETicketTests(Base):
    """A printable e-ticket with a signed QR code, and the check a conductor makes by scanning it."""

    def setUp(self):
        super().setUp()
        self.login('admin')

    def code_for(self, ticket_id):
        html = self.client.get(f'/tickets/{ticket_id}').get_data(as_text=True)
        self.assertIn('<svg', html)                                                      # the QR code
        return re.search(r'/verify/(\d+-[0-9a-f]{16})', html).group(1)

    def test_valid_today_future_tampered_and_cancelled(self):
        code = self.code_for(1)                                                          # a seed ticket for today
        self.assertIn('Valid today', self.client.get(f'/verify/{code}').get_data(as_text=True))
        tampered = code[:-1] + ('0' if code[-1] != '0' else '1')
        self.assertIn('Not a genuine', self.client.get(f'/verify/{tampered}').get_data(as_text=True))
        self.assertIn('Not a genuine', self.client.get('/verify/1-0000000000000000').get_data(as_text=True))
        self.assertEqual(self.client.get('/verify/abc').status_code, 400)
        self.post('/book', p_id=6, s_id=3, seat='9A', fare='', travel_date=(date.today() + timedelta(days=2)).isoformat())
        tid = self.appmod.query("SELECT TicketID FROM Ticket WHERE ScheduleID = 3 AND SeatNo = '9A'")[0]['TicketID']
        later = self.code_for(tid)
        self.assertIn('another day', self.client.get(f'/verify/{later}').get_data(as_text=True))
        self.post(f'/cancel/{tid}')
        r = self.client.get(f'/verify/{later}')
        self.assertEqual(r.status_code, 410)
        self.assertIn('Cancelled ticket', r.get_data(as_text=True))
        self.assertIn('by admin', r.get_data(as_text=True))                              # read from the audit trail

    def test_a_changed_seat_invalidates_the_old_code(self):
        self.post('/book', p_id=7, s_id=3, seat='9B', fare='')
        tid = self.appmod.query("SELECT TicketID FROM Ticket WHERE ScheduleID = 3 AND SeatNo = '9B'")[0]['TicketID']
        code = self.code_for(tid)
        self.post(f'/transfer/{tid}', s_id=3, seat='9C')                                 # a transfer issues a new ticket
        self.assertEqual(self.client.get(f'/verify/{code}').status_code, 410)

    def test_every_role_sees_e_tickets_but_only_when_signed_in(self):
        self.assertEqual(self.client.get('/tickets/999999').status_code, 404)
        anonymous = self.appmod.app.test_client()
        self.assertEqual(anonymous.get('/tickets/1').status_code, 302)
        self.assertEqual(anonymous.get('/verify/1-0000000000000000').status_code, 302)
        self.client = self.appmod.app.test_client()
        self.login('viewer')
        self.assertIn('Scan to verify', self.client.get('/tickets/1').get_data(as_text=True))


class PartitionTests(Base):
    """The audit trail is partitioned by quarter; queries on a period read only its partitions; new quarters are added."""

    def partitions(self):
        out = mysql(f"SELECT PARTITION_NAME FROM information_schema.partitions WHERE TABLE_SCHEMA = '{TEST_DB}' "
                    f"AND TABLE_NAME = 'TicketAudit' ORDER BY PARTITION_ORDINAL_POSITION").stdout.split()[1:]
        return out

    def test_range_partitions_and_pruning(self):
        parts = self.partitions()
        self.assertGreaterEqual(len(parts), 10)
        self.assertEqual((parts[0], parts[-1]), ('p_before_2026', 'pmax'))
        today = date.today()
        quarter_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        plan = self.appmod.query("EXPLAIN FORMAT=TRADITIONAL SELECT COUNT(*) FROM TicketAudit WHERE ActionTime >= %s AND ActionTime < %s + INTERVAL 3 MONTH",
                                 (quarter_start, quarter_start))[0]
        self.assertEqual(plan['partitions'], f'p{today.year}q{(today.month - 1) // 3 + 1}')        # one partition read, not all
        self.assertEqual(self.appmod.query('SELECT COUNT(*) n FROM TicketAudit')[0]['n'],
                         self.appmod.query('SELECT COUNT(*) n FROM TicketAudit WHERE ActionTime >= %s', (quarter_start,))[0]['n']
                         + self.appmod.query('SELECT COUNT(*) n FROM TicketAudit WHERE ActionTime < %s', (quarter_start,))[0]['n'])

    def test_partition_maintenance_adds_future_quarters(self):
        before = self.partitions()
        mysql(f'USE {TEST_DB}; CALL ExtendAuditPartitions(36)')
        after = self.partitions()
        self.assertGreater(len(after), len(before))
        horizon = date.today() + timedelta(days=36 * 31)
        self.assertIn(f'p{horizon.year - 1}q4', after)
        self.assertEqual(after[-1], 'pmax')
        mysql(f'USE {TEST_DB}; CALL ExtendAuditPartitions(36)')
        self.assertEqual(self.partitions(), after)                                       # nothing to do the second time
        self.login('admin')
        self.post('/book', p_id=8, s_id=3, seat='9D', fare='')                          # the audit trail still takes rows
        self.assertEqual(self.appmod.query("SELECT COUNT(*) n FROM TicketAudit WHERE SeatNo = '9D' AND ScheduleID = 3")[0]['n'], 1)


class BoardTests(Base):
    def test_departure_board_lists_todays_departures_with_a_status(self):
        self.login('viewer')
        html = self.client.get('/board').get_data(as_text=True)
        rows = self.appmod.query('SELECT Status FROM DepartureBoard')
        running = self.appmod.query('SELECT COUNT(*) n FROM Schedule WHERE RunsOn(ScheduleID, CURDATE())')[0]['n']
        self.assertEqual(len(rows), running)
        self.assertTrue({r['Status'] for r in rows} <= {'Scheduled', 'Boarding', 'En route', 'Arrived', 'Cancelled'})
        self.assertEqual(html.count('class="board-row status-'), running)
        self.assertIn('Departures now', self.client.get('/').get_data(as_text=True))


class ServiceDayTableTests(Base):
    """Service days are rows of ScheduleDay: a departure keeps at least one, and a day with sold tickets stays."""

    def setUp(self):
        super().setUp()
        self.login('admin')

    def test_new_departures_run_daily_and_add_schedule_is_atomic(self):
        self.appmod.execute("INSERT INTO Schedule (RouteID, VehicleID, DepartureTime, ArrivalTime) VALUES (2, 1, '22:10', '22:40')")
        sid = self.appmod.query('SELECT MAX(ScheduleID) m FROM Schedule')[0]['m']
        self.assertEqual(self.appmod.query('SELECT COUNT(*) n FROM ScheduleDay WHERE ScheduleID = %s', (sid,))[0]['n'], 7)
        before = self.appmod.query('SELECT COUNT(*) n FROM Schedule')[0]['n']
        import mysql.connector
        with self.assertRaises(mysql.connector.Error) as cm:                              # no day at all: everything rolls back
            self.appmod.query_proc('AddSchedule', [2, 1, '23:00', '23:30', None, 'Funday', True])
        self.assertIn('at least one day', cm.exception.msg)
        self.assertEqual(self.appmod.query('SELECT COUNT(*) n FROM Schedule')[0]['n'], before)

    def test_days_with_sold_tickets_cannot_be_removed(self):
        tomorrow = date.today() + timedelta(days=1)
        self.post('/book', p_id=9, s_id=4, seat='9A', fare='', travel_date=tomorrow.isoformat())
        r = subprocess.run(['mysql', '-u', os.environ.get('MYSQL_ADMIN_USER', 'root'), TEST_DB, '-e',
                            'DELETE FROM ScheduleDay WHERE ScheduleID = 4 AND Weekday = DAYOFWEEK(CURDATE() + INTERVAL 1 DAY)'],
                           env=dict(os.environ, MYSQL_PWD=os.environ.get('MYSQL_ADMIN_PASSWORD', '')), capture_output=True, text=True)
        self.assertIn('Tickets are sold for that day', r.stderr)
        self.assertEqual(self.appmod.query('SELECT COUNT(*) n FROM ScheduleDay WHERE ScheduleID = 4')[0]['n'], 7)


class VehicleSwapTests(Base):
    """Another vehicle can run one trip; every seat rule follows the vehicle the trip really uses."""

    def setUp(self):
        super().setUp()
        self.login('admin')
        self.day = date.today() + timedelta(days=2)

    def vehicle(self, number, capacity, per_row=4, vtype='Bus', status='Active'):
        self.post('/vehicles/add', vehicle_no=number, type=vtype, capacity=capacity, seats_per_row=per_row, status=status)
        return self.appmod.query('SELECT VehicleID v FROM Vehicle WHERE VehicleNo = %s', (number,))[0]['v']

    def departure(self, vid, dep, arr):
        self.post('/schedules/add', route_id=1, vehicle_id=vid, departure=dep, arrival=arr)
        return self.appmod.query('SELECT MAX(ScheduleID) m FROM Schedule')[0]['m']

    def swap(self, sid, vid, day=None):
        return self.post('/swaps/add', s_id=sid, vehicle_id=vid, travel_date=(day or self.day).isoformat(), reason='brake repair')

    def test_a_swapped_trip_uses_the_replacement_seat_map(self):
        sid = self.departure(self.vehicle('KA-00-SWA', 8), '00:05', '00:35')
        big = self.vehicle('KA-00-SWB', 12, per_row=6)
        self.assertIn('uses another vehicle', self.swap(sid, big))
        self.assertIn('Ticket booked', self.post('/book', p_id=2, s_id=sid, seat='2F', fare='', travel_date=self.day.isoformat()))   # only the 6-across bus has F
        self.assertIn('No such seat', self.post('/book', p_id=3, s_id=sid, seat='3A', fare='', travel_date=self.day.isoformat()))    # 12 seats: rows 1-2
        api = self.client.get(f'/api/seats?s={sid}&d={self.day.isoformat()}').get_json()
        self.assertEqual((api['capacity'], api['swapped']), (12, 'KA-00-SWB'))
        self.assertEqual(self.appmod.query("SELECT VehicleNo FROM BookingSummary WHERE SeatNo = '2F' AND TravelDate = %s", (self.day,))[0]['VehicleNo'], 'KA-00-SWB')
        other_day = self.client.get(f'/api/seats?s={sid}&d={(self.day + timedelta(days=1)).isoformat()}').get_json()
        self.assertEqual((other_day['capacity'], other_day['swapped']), (8, None))                # only that one date
        self.assertIn('do not exist on the scheduled vehicle', self.post(f'/swaps/{sid}/{self.day.isoformat()}/delete'))   # 2F would vanish

    def test_swaps_that_cannot_work_are_refused(self):
        small = self.vehicle('KA-00-SWC', 8)
        sid = self.departure(small, '00:40', '01:10')
        self.post('/book', p_id=4, s_id=sid, seat='2D', fare='', travel_date=self.day.isoformat())
        three = self.vehicle('KA-00-SWD', 9, per_row=3)
        self.assertIn('do not exist on the replacement', self.swap(sid, three))                   # 2D sold, no D three-across
        idle = self.vehicle('KA-00-SWE', 20, status='Maintenance')
        self.assertIn('active vehicle of the route type', self.swap(sid, idle))
        metro = self.vehicle('KA-00-SWF', 20, vtype='Metro')
        self.assertIn('active vehicle of the route type', self.swap(sid, metro))
        busy = self.vehicle('KA-00-SWG', 20)
        self.departure(busy, '00:50', '01:20')                                                   # busy at that time every day
        self.assertIn('already has a run at that time', self.swap(sid, busy))
        self.assertIn('already the scheduled vehicle', self.swap(sid, small))
        self.assertIn('in the past', self.swap(sid, busy, date.today() - timedelta(days=1)))
        self.client = self.appmod.app.test_client()
        self.login('staff')
        self.assertIn('not allowed', self.swap(sid, three))


class PaymentLedgerTests(Base):
    """Triggers keep a money ledger: a sale per booking, a refund per cancellation, both for a transfer."""

    def setUp(self):
        super().setUp()
        self.login('admin')

    def payments(self, ticket):
        return [(r['Kind'], r['Method'], r['Reference'], float(r['Amount'])) for r in
                self.appmod.query('SELECT * FROM Payment WHERE TicketID = %s ORDER BY PaymentID', (ticket,))]

    def balanced(self):
        return self.appmod.query('SELECT (SELECT SUM(Amount) FROM Payment) = (SELECT SUM(Fare) FROM Ticket) ok')[0]['ok'] == 1

    def test_sale_refund_and_transfer_keep_the_ledger_balanced(self):
        self.assertTrue(self.balanced())
        self.assertIn('Ticket booked', self.post('/book', p_id=10, s_id=3, seat='10A', fare='', pay_method='UPI', pay_ref='TXN-4411'))
        tid = self.appmod.query("SELECT TicketID, Fare FROM Ticket WHERE ScheduleID = 3 AND SeatNo = '10A'")[0]
        fare = float(tid['Fare'])
        self.assertEqual(self.payments(tid['TicketID']), [('Sale', 'UPI', 'TXN-4411', fare)])
        self.post(f"/transfer/{tid['TicketID']}", s_id=1, seat='10A')                          # refund the old fare, sell the new one
        new = self.appmod.query("SELECT TicketID, Fare FROM Ticket WHERE ScheduleID = 1 AND SeatNo = '10A'")[0]
        self.assertEqual(self.payments(tid['TicketID'])[-1][:2], ('Refund', 'UPI'))
        self.assertEqual(self.payments(new['TicketID']), [('Sale', 'UPI', 'TXN-4411', float(new['Fare']))])   # paid the same way
        self.post(f"/cancel/{new['TicketID']}")
        self.assertEqual(self.payments(new['TicketID'])[-1], ('Refund', 'UPI', f"refund of ticket #{new['TicketID']}", -float(new['Fare'])))
        self.assertTrue(self.balanced())
        e = self.client.get(f'/tickets/1').get_data(as_text=True)
        self.assertIn('Paid by', e)

    def test_payment_input_is_validated(self):
        self.assertIn('Choose Cash, UPI or Card', self.post('/book', p_id=10, s_id=3, seat='10B', fare='', pay_method='Bitcoin'))
        self.assertIn('Choose Cash, UPI or Card', self.post('/book', p_id=10, s_id=3, seat='10B', fare='', pay_method='UPI', pay_ref="x'; DROP"))
        self.assertEqual(self.appmod.query("SELECT COUNT(*) n FROM Ticket WHERE ScheduleID = 3 AND SeatNo = '10B'")[0]['n'], 0)

    def test_cash_up_report_rolls_up(self):
        r = next(x for x in self.appmod.REPORTS if x['title'].startswith('Takings'))
        rows = self.appmod.query(r['sql'])
        total = [x for x in rows if x['Method'] == 'All methods']
        self.assertEqual(len(total), 1)
        detail = sum(float(x['Amount']) for x in rows if x['Method'] != 'All methods' and x['Kind'] != 'Net')
        self.assertAlmostEqual(float(total[0]['Amount']), detail, places=2)


class ReplicationDemoTests(unittest.TestCase):
    """Two private MySQL servers: rows written on the primary reach the read-only replica (skipped where mysqld cannot run)."""

    def test_rows_replicate_and_the_replica_refuses_writes(self):
        import json
        import shutil
        if not shutil.which('mysqld'):
            self.skipTest('mysqld is not installed here')
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'replication_demo.py'), '--json'],
                           capture_output=True, text=True, timeout=300)
        if r.returncode == 3:
            self.skipTest('private MySQL servers cannot be started here: ' + r.stderr.strip())
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out['replicated'], {'tickets': 3, 'audit_rows': 3, 'payments': 3})
        self.assertEqual((out['io_running'], out['sql_running']), ('Yes', 'Yes'))
        self.assertTrue(out['replica_write'].startswith('refused'))
        self.assertEqual(out['report_rows'], 3)
        self.assertLess(out['lag_ms'], 5000)


if __name__ == '__main__':
    unittest.main()
