"""Shared test helpers: a throw-away MySQL database built from schema.sql, and a base class that drives the app.

Every test module builds its own fresh database (setUpModule) and drops it afterwards, so no module depends on what
another one left behind. The tests use the MySQL admin account (MYSQL_ADMIN_USER / MYSQL_ADMIN_PASSWORD, default root
with an empty password) and never touch the real database.
"""
import logging
import os
import re
import subprocess
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = 'routewise_test'
os.environ['DB_NAME'] = TEST_DB          # must be set before the application is imported
sys.path.insert(0, ROOT)
logging.getLogger('routewise.access').setLevel(logging.WARNING)   # quiet test output; assertLogs still sees it


def mysql(sql=None, stdin=None):
    env = dict(os.environ, MYSQL_PWD=os.environ.get('MYSQL_ADMIN_PASSWORD', ''))
    cmd = ['mysql', '-u', os.environ.get('MYSQL_ADMIN_USER', 'root'), '-h', os.environ.get('DB_HOST', 'localhost')]
    if sql:
        cmd += ['-e', sql]
    return subprocess.run(cmd, input=stdin, text=True, env=env, capture_output=True, check=True)


def build_database():
    with open(os.path.join(ROOT, 'schema.sql')) as f:
        schema = f.read().replace('public_transport', TEST_DB)
    mysql(f'DROP DATABASE IF EXISTS {TEST_DB}')
    mysql(stdin=schema)


def drop_database():
    mysql(f'DROP DATABASE IF EXISTS {TEST_DB}')


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as entry  # the same import the production server uses
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
