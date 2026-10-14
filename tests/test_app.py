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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = 'transitiq_test'
os.environ['DB_NAME'] = TEST_DB          # must be set before app.py is imported
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
        import app as appmod
        cls.appmod = appmod
        appmod.app.config['TESTING'] = True
        appmod.REPORT_CACHE_SECONDS = 0                              # tests always see fresh report data

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
        self.assertIn('Ticket booked', self.post('/book', p_id=5, s_id=8, seat='ST1', fare=40))
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
        self.assertIn('Ticket booked', self.post('/book', p_id=1, s_id=2, seat='t1', fare=45))
        self.assertIn('Seat already booked', self.post('/book', p_id=2, s_id=2, seat='T1', fare=45))

    def test_auto_fare_uses_card_discount(self):
        # passenger 1 = Monthly card (30% off); schedule 9 = route 335E, 35 km bus -> 35 * 1.5 = 52.50 -> 36.75
        self.assertIn('Ticket booked', self.post('/book', p_id=1, s_id=9, seat='AF1'))
        rows = self.appmod.query("SELECT Fare FROM Ticket WHERE ScheduleID=9 AND SeatNo='AF1'")
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
        self.assertIn('Ticket booked', self.post('/book', p_id=1, s_id=sid, seat='F1', fare=10))
        self.assertIn('Vehicle is full', self.post('/book', p_id=2, s_id=sid, seat='F2', fare=10))

    def test_vehicle_in_maintenance_cannot_be_booked(self):
        self.post('/vehicles/add', vehicle_no='KA-00-M-3', type='Bus', capacity=10, status='Maintenance')
        vid = self.appmod.query("SELECT VehicleID FROM Vehicle WHERE VehicleNo='KA-00-M-3'")[0]['VehicleID']
        # schedules can only be created for active vehicles in the UI, so insert directly
        self.appmod.execute("INSERT INTO Schedule (RouteID, VehicleID, DepartureTime, ArrivalTime) VALUES (1, %s, '02:00', '03:00')", (vid,))
        sid = self.appmod.query("SELECT MAX(ScheduleID) m FROM Schedule")[0]['m']
        self.assertIn('not in service', self.post('/book', p_id=1, s_id=sid, seat='M1', fare=10))

    def test_cancel_writes_audit_log(self):
        self.post('/book', p_id=3, s_id=3, seat='AU1', fare=30)
        tid = self.appmod.query("SELECT TicketID FROM Ticket WHERE SeatNo='AU1'")[0]['TicketID']
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
        barrier, errors = threading.Barrier(20), []

        def book(i):
            conn = mysql.connector.connect(**self.appmod.db_config)
            barrier.wait()
            try:
                cur = conn.cursor()
                cur.callproc('BookTicket', [1 + i % 13, sid, f'S{i}', 10, None])
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
        self.assertTrue(all('Vehicle is full' in e for e in errors), errors)     # no deadlock errors

    def test_vehicle_cannot_have_overlapping_runs(self):
        # vehicle 1 already runs 06:00-07:30
        self.assertIn('already has a run', self.post('/schedules/add', route_id=2, vehicle_id=1, departure='06:30', arrival='07:00'))
        self.assertIn('Schedule added', self.post('/schedules/add', route_id=2, vehicle_id=1, departure='07:30', arrival='08:00'))   # back-to-back is fine

    def test_csv_export_neutralises_formulas(self):
        self.post('/passengers/add', name='=HYPERLINK("http://evil","x")', email='csvinj@x.com')
        pid = self.appmod.query("SELECT PassengerID p FROM Passenger WHERE Email='csvinj@x.com'")[0]['p']
        self.post('/book', p_id=pid, s_id=3, seat='CSV9', fare=5)
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
    DB = 'transitiq_matrix'
    PW = {'admin': 'admin123', 'staff': 'staff123', 'viewer': 'viewer123'}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        schema = open(os.path.join(ROOT, 'schema.sql')).read().replace('public_transport', cls.DB)
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
            self.assertIn('TransitIQ', r.get_data(as_text=True))
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
        self.post('/book', p_id=5, s_id=8, seat='TR1', fare=40)                    # passenger 5 has no card (no discount)
        old = self.ticket(8, 'TR1')
        self.assertIn('moved to schedule #2', self.post(f"/transfer/{old['TicketID']}", s_id=2, seat='tr9'))
        self.assertIsNone(self.ticket(8, 'TR1'))                                   # old seat freed
        new = self.ticket(2, 'TR9')                                                # seat upper-cased
        self.assertEqual(new['PassengerID'], 5)
        self.assertEqual(float(new['Fare']), 38.25)                                # CalcFare for the new route (25.5 km x 1.5)
        actions = [r['Action'] for r in self.appmod.query(
            "SELECT Action FROM TicketAudit WHERE SeatNo IN ('TR1','TR9') ORDER BY AuditID")]
        self.assertEqual(actions, ['BOOKED', 'CANCELLED', 'BOOKED'])

    def test_failed_transfer_rolls_everything_back(self):
        self.post('/book', p_id=5, s_id=8, seat='TR2', fare=40)
        self.post('/book', p_id=6, s_id=2, seat='TR3', fare=40)                    # the seat the first passenger wants
        before = self.counts()
        mine = self.ticket(8, 'TR2')
        self.assertIn('Seat already booked', self.post(f"/transfer/{mine['TicketID']}", s_id=2, seat='TR3'))
        self.assertEqual(self.ticket(8, 'TR2')['TicketID'], mine['TicketID'])      # original ticket untouched
        self.assertEqual(self.counts(), before)                                    # no ticket and no audit row appeared or vanished

    def test_transfer_to_a_vehicle_under_maintenance_rolls_back(self):
        self.post('/vehicles/add', vehicle_no='KA-00-TRM', type='Bus', capacity=10, status='Maintenance')
        vid = self.appmod.query("SELECT VehicleID v FROM Vehicle WHERE VehicleNo='KA-00-TRM'")[0]['v']
        self.post('/schedules/add', route_id=1, vehicle_id=vid, departure='23:00', arrival='23:30')
        sid = self.appmod.query("SELECT MAX(ScheduleID) m FROM Schedule")[0]['m']
        self.post('/book', p_id=7, s_id=8, seat='TR4', fare=40)
        before, mine = self.counts(), self.ticket(8, 'TR4')
        self.assertIn('not in service', self.post(f"/transfer/{mine['TicketID']}", s_id=sid, seat='A1'))
        self.assertIsNotNone(self.ticket(8, 'TR4'))
        self.assertEqual(self.counts(), before)

    def test_unknown_ticket_and_missing_fields(self):
        self.assertIn('Ticket not found', self.post('/transfer/999999', s_id=2, seat='Z1'))
        self.assertIn('Choose the new departure', self.post('/transfer/1', s_id='', seat=''))

    def test_only_staff_and_admin_can_transfer(self):
        self.client = self.appmod.app.test_client()
        self.client.post('/login', data={'username': 'viewer', 'password': 'viewer123', 'csrf_token': self.token()})
        self.assertIn('not allowed', self.post('/transfer/1', s_id=2, seat='Z1'))
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
                     "CREATE USER 'x'@'localhost'", 'TRUNCATE TABLE Ticket'):
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
        cur.callproc('BookTicket', [13, 9, 'PRV1', 10, None]); conn.commit()               # the app has no INSERT right on TicketAudit...
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
        target = 'transitiq_restore_test'
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
                     f" (SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema='{db}')")
                env2 = dict(os.environ, MYSQL_PWD=os.environ.get('MYSQL_ADMIN_PASSWORD', ''))
                return subprocess.run(['mysql', '-N', '-u', os.environ.get('MYSQL_ADMIN_USER', 'root'), '-e', q],
                                      env=env2, check=True, capture_output=True, text=True).stdout
            self.assertEqual(snapshot(TEST_DB), snapshot(target))
        finally:
            mysql(f'DROP DATABASE IF EXISTS {target}')


class LargeDataTests(Base):
    """The sample-data loader fills a database with thousands of rows that obey every rule, and the app still copes."""
    DB = 'transitiq_large'

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
        self.assertIn('Ticket booked', self.book(1, 2, 'TD1'))
        self.assertIn('Ticket booked', self.book(2, 2, 'TD1', self.today + timedelta(days=1)))      # same seat, tomorrow
        self.assertIn('Seat already booked', self.book(3, 2, 'TD1'))                                # same seat, same day
        self.assertIn('Seat already booked', self.book(3, 2, 'TD1', self.today + timedelta(days=1)))

    def test_past_and_too_distant_dates_are_refused(self):
        self.assertIn('in the past', self.book(1, 2, 'TD2', self.today - timedelta(days=1)))
        self.assertIn('at most 30 days ahead', self.book(1, 2, 'TD2', self.today + timedelta(days=31)))
        self.assertIn('Ticket booked', self.book(1, 2, 'TD2', self.today + timedelta(days=30)))
        self.assertIn('not a valid date', self.post('/book', p_id=1, s_id=2, seat='TD3', fare='', travel_date='2026-13-45'))

    def test_capacity_is_counted_per_trip(self):
        sid = self.new_schedule('KA-00-TRIP', 2, '02:10', '02:40')
        tomorrow = self.today + timedelta(days=1)
        self.assertIn('Ticket booked', self.book(1, sid, 'C1'))
        self.assertIn('Ticket booked', self.book(2, sid, 'C2'))
        self.assertIn('Vehicle is full', self.book(3, sid, 'C3'))                                   # today's trip is full ...
        self.assertIn('Ticket booked', self.book(3, sid, 'C3', tomorrow))                           # ... tomorrow's is empty
        rows = self.appmod.query('SELECT TravelDate, Booked, SeatsLeft FROM TripOccupancy WHERE ScheduleID = %s ORDER BY TravelDate', (sid,))
        self.assertEqual([(r['Booked'], r['SeatsLeft']) for r in rows], [(2, 0), (1, 1)])

    def test_seats_left_api_and_function_use_the_chosen_date(self):
        sid = self.new_schedule('KA-00-API', 3, '02:50', '03:20')
        self.book(1, sid, 'A1')
        today = self.client.get(f'/api/seats?s={sid}&d={self.today.isoformat()}').get_json()
        tomorrow = self.client.get(f'/api/seats?s={sid}&d={(self.today + timedelta(days=1)).isoformat()}').get_json()
        self.assertEqual((today['left'], today['capacity']), (2, 3))
        self.assertEqual(tomorrow['left'], 3)
        self.assertIsNone(self.client.get('/api/seats?s=x&d=nonsense').get_json()['left'])

    def test_booking_form_and_list_show_travel_dates(self):
        html = self.client.get('/bookings').get_data(as_text=True)
        self.assertIn(f'value="{self.today.isoformat()}"', html)
        self.assertIn('name="travel_date"', html)
        self.assertIn('<th>Travels</th>', html)
        self.assertIn('TravelDate', self.client.get('/bookings/export.csv').get_data(as_text=True).splitlines()[0])

    def test_transfer_can_move_the_ticket_to_another_day(self):
        self.book(5, 8, 'TD7')
        tid = self.appmod.query("SELECT TicketID FROM Ticket WHERE ScheduleID = 8 AND SeatNo = 'TD7' AND TravelDate = CURDATE()")[0]['TicketID']
        tomorrow = self.today + timedelta(days=1)
        self.assertIn('moved to schedule', self.post(f'/transfer/{tid}', s_id=2, seat='TD7', travel_date=tomorrow.isoformat()))
        row = self.appmod.query("SELECT TravelDate, ScheduleID FROM Ticket WHERE PassengerID = 5 AND SeatNo = 'TD7'")[0]
        self.assertEqual((row['TravelDate'], row['ScheduleID']), (tomorrow, 2))
        tid2 = self.appmod.query("SELECT TicketID FROM Ticket WHERE ScheduleID = 2 AND SeatNo = 'TD7'")[0]['TicketID']
        self.assertIn('in the past', self.post(f'/transfer/{tid2}', s_id=2, seat='TD8', travel_date=(self.today - timedelta(days=2)).isoformat()))
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
        self.post('/book', p_id=7, s_id=8, seat='AC1', fare='')
        tid = self.appmod.query("SELECT TicketID FROM Ticket WHERE ScheduleID = 8 AND SeatNo = 'AC1'")[0]['TicketID']
        self.client = self.appmod.app.test_client()
        self.client.post('/login', data={'username': 'admin', 'password': 'admin123', 'csrf_token': self.token()})
        self.post(f'/cancel/{tid}')
        rows = self.appmod.query('SELECT Action, Actor, TravelDate FROM TicketAudit WHERE TicketID = %s ORDER BY AuditID', (tid,))
        self.assertEqual([(r['Action'], r['Actor']) for r in rows], [('BOOKED', 'staff'), ('CANCELLED', 'admin')])
        self.assertEqual(rows[0]['TravelDate'], date.today())

    def test_schema_version_is_recorded(self):
        self.assertEqual(self.appmod.query('SELECT MAX(Version) v FROM SchemaVersion')[0]['v'], 2)


class AccountSecurityTests(Base):
    """Own-password change and the sign-in attempt limit."""

    def make_user(self, name, password='initial-pass1', role='staff'):
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
            for _ in range(self.appmod.LOGIN_MAX_FAILURES):
                self.assertEqual(self.sign_in('lockme', 'wrong-password').status_code, 200)
            locked = self.sign_in('lockme', 'initial-pass1')                                       # even the CORRECT password is refused now
            self.assertEqual(locked.status_code, 429)
            self.assertIn('Too many failed sign-in attempts', locked.get_data(as_text=True))
            self.assertEqual(self.sign_in('admin', 'admin123').status_code, 302)                   # other users are not affected
        finally:
            self.appmod._failed_logins.clear()
        self.assertEqual(self.sign_in('lockme', 'initial-pass1').status_code, 302)                 # lock expires / is cleared

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
            self.assertIn('TransitIQ', body)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


class MigrationTests(unittest.TestCase):
    """Upgrading a version-1 database with scripts/migrate.py gives exactly a fresh install and keeps the data."""
    OLD, NEW = 'transitiq_mig_old', 'transitiq_mig_new'
    SIGNATURE = (
        "SELECT 'col', CONCAT_WS('|', table_name, ordinal_position, column_name, column_type, is_nullable, IFNULL(column_default,'-'), extra) FROM information_schema.columns WHERE table_schema = DATABASE() "
        "UNION ALL SELECT 'idx', CONCAT_WS('|', table_name, index_name, non_unique, GROUP_CONCAT(column_name ORDER BY seq_in_index)) FROM information_schema.statistics WHERE table_schema = DATABASE() GROUP BY table_name, index_name, non_unique "
        "UNION ALL SELECT 'chk', CONCAT_WS('|', tc.table_name, tc.constraint_name, cc.check_clause) FROM information_schema.table_constraints tc JOIN information_schema.check_constraints cc ON cc.constraint_schema = tc.constraint_schema AND cc.constraint_name = tc.constraint_name WHERE tc.table_schema = DATABASE() AND tc.constraint_type = 'CHECK' "
        "UNION ALL SELECT 'fk', CONCAT_WS('|', table_name, constraint_name, referenced_table_name, delete_rule) FROM information_schema.referential_constraints WHERE constraint_schema = DATABASE() "
        "UNION ALL SELECT 'trg', CONCAT_WS('|', trigger_name, event_object_table, action_timing, event_manipulation, action_order, action_statement) FROM information_schema.triggers WHERE trigger_schema = DATABASE() "
        "UNION ALL SELECT 'rtn', CONCAT_WS('|', routine_name, routine_type, routine_definition) FROM information_schema.routines WHERE routine_schema = DATABASE() "
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
        self.assertEqual(self.scalar(self.OLD, 'SELECT MAX(Version) FROM SchemaVersion'), '2')
        with open(os.path.join(ROOT, 'schema.sql')) as f:
            mysql(stdin=f.read().replace('public_transport', self.NEW))
        upgraded = self.scalar(self.OLD, self.SIGNATURE).replace(self.OLD, 'DB')
        fresh = self.scalar(self.NEW, self.SIGNATURE).replace(self.NEW, 'DB')
        self.assertGreater(len(fresh.splitlines()), 150)
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
        for i in group_a:
            self.post('/book', p_id=i, s_id=8, seat=f'CA{i}', fare='')
        for i in group_b:
            self.post('/book', p_id=i, s_id=9, seat=f'CB{i}', fare='')
        ids = {r['PassengerID']: r['TicketID'] for r in self.appmod.query("SELECT PassengerID, TicketID FROM Ticket WHERE SeatNo LIKE 'C%'")}
        self.assertEqual(len(ids), 12)
        barrier, errors = threading.Barrier(12), []

        def move(passenger, target, seat):
            barrier.wait()
            try:
                self.appmod.call('TransferTicket', [ids[passenger], target, seat, None])     # call() retries a deadlock victim
            except Exception as err:                                                         # noqa: BLE001
                errors.append(repr(err))
        threads = [threading.Thread(target=move, args=(i, 9, f'CV{i}')) for i in group_a] + \
                  [threading.Thread(target=move, args=(i, 8, f'CW{i}')) for i in group_b]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        rows = self.appmod.query("SELECT PassengerID, ScheduleID, SeatNo FROM Ticket WHERE SeatNo LIKE 'CV%' OR SeatNo LIKE 'CW%'")
        self.assertEqual(sorted(r['PassengerID'] for r in rows), list(range(1, 13)))         # exactly one ticket each
        self.assertTrue(all((r['ScheduleID'] == 9) == r['SeatNo'].startswith('CV') for r in rows))
        self.assertEqual(self.appmod.query("SELECT COUNT(*) n FROM Ticket WHERE SeatNo LIKE 'CA%' OR SeatNo LIKE 'CB%'")[0]['n'], 0)


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
        self.appmod.REPORT_CACHE_SECONDS = 60
        try:
            self.appmod._report_cache.update(results=None)
            first = self.client.get('/reports').get_data(as_text=True)
            self.assertIn('kept for 60 s', first)
            cached_at = self.appmod._report_cache['at']
            self.client.get('/reports')
            self.assertEqual(self.appmod._report_cache['at'], cached_at)                  # second view reused the results
            self.client.get('/reports?refresh=1')
            self.assertGreater(self.appmod._report_cache['at'], cached_at)               # Refresh recalculated them
        finally:
            self.appmod.REPORT_CACHE_SECONDS = 0

    def test_report_timing_script_runs(self):
        out = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'report_timings.py'), '--runs', '1'], capture_output=True,
                             text=True, env=dict(os.environ, DB_NAME=TEST_DB), check=True).stdout
        self.assertIn('All 12 reports together', out)
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


if __name__ == '__main__':
    unittest.main()
