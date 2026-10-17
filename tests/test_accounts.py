"""Sign-in, roles, CSRF, the stored sign-in limit, password changes."""
import os
import subprocess
import sys
import time
import unittest

from support import ROOT, TEST_DB, Base, build_database, drop_database, mysql


def setUpModule():
    build_database()


def tearDownModule():
    drop_database()


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


if __name__ == '__main__':
    unittest.main()
