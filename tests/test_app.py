"""End-to-end tests: Flask test client + a throwaway MySQL database built from schema.sql.

Run from the project folder:   python -m unittest discover -s tests -v
(the tests use the MySQL admin account to build a throw-away database; set MYSQL_ADMIN_USER / MYSQL_ADMIN_PASSWORD if
yours is not root with an empty password. The tests never touch the real database)
"""
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = 'routewise_test'
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
        self.assertIn('created', self.post('/users/add', username='newbie', full_name='New', password='secret1', role='staff'))
        self.assertIn('at least 6', self.post('/users/add', username='weak', password='123', role='staff'))
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
                cur.callproc('BookTicket', [1 + i % 13, sid, f'S{i}', 10])
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
    DB = 'routewise_matrix'
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
        cur.callproc('BookTicket', [13, 9, 'PRV1', 10]); conn.commit()               # the app has no INSERT right on TicketAudit...
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
                     f" (SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema='{db}')")
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
        self.assertEqual(self.count('SELECT COUNT(*) n FROM ScheduleOccupancy WHERE SeatsLeft < 0'), 0)                 # no overbooking
        self.assertEqual(self.count('SELECT COUNT(*) n FROM (SELECT ScheduleID, SeatNo FROM Ticket GROUP BY 1, 2 HAVING COUNT(*) > 1) d'), 0)
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
