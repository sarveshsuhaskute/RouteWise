"""Every page and every visible button, for every role."""
import os
import unittest

from support import ROOT, Base, build_database, drop_database, mysql


def setUpModule():
    build_database()


def tearDownModule():
    drop_database()


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
        def hide(name):
            return any(name in c for _, c in self.stack)
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
                       if 'GET' in r.methods and not r.arguments
                       and r.endpoint not in ('static', 'login', 'metrics')})      # /metrics is for machines (MonitoringTests)

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
                    # deleting users would remove the other roles' accounts
                    if f['action'] and f['action'] != '/logout' and not f['action'].startswith('/users/'):
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
        # emptied by the admin account: the application's own user is (rightly) not allowed to wipe the audit table.
        # The ledgers are append-only for row deletes, so they are emptied the way an archive would be: TRUNCATE (DDL).
        mysql('SET FOREIGN_KEY_CHECKS=0; ' + ' '.join(f'DELETE FROM {self.DB}.{t};' for t in
              ('Ticket', 'Feedback', 'Schedule', 'Vehicle', 'Driver', 'Route', 'Passenger'))
              + f' TRUNCATE TABLE {self.DB}.TicketAudit; TRUNCATE TABLE {self.DB}.Payment;')
        for page in self.pages():
            self.assertEqual(self.client.get(page).status_code, 200, f'empty database: GET {page}')


if __name__ == '__main__':
    unittest.main()
