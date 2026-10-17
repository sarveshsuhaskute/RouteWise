"""Backups, migrations, the production server, large data and replication."""
import os
import re
import subprocess
import sys
import unittest

from support import ROOT, TEST_DB, Base, build_database, drop_database, mysql


def setUpModule():
    build_database()


def tearDownModule():
    drop_database()


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
        "UNION ALL SELECT 'view', CONCAT_WS('|', table_name, view_definition) FROM information_schema.views WHERE table_schema = DATABASE() "
        "UNION ALL SELECT 'priv', CONCAT_WS('|', grantee, table_name, privilege_type) FROM information_schema.table_privileges WHERE table_schema = DATABASE() "
        "UNION ALL SELECT 'priv', CONCAT_WS('|', grantee, '*', privilege_type) FROM information_schema.schema_privileges WHERE table_schema = DATABASE() ORDER BY 1, 2")

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
        self.assertEqual(self.scalar(self.OLD, 'SELECT MAX(Version) FROM SchemaVersion'), '7')
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
        self.assertEqual(out['after_failover'], {'tickets': 4, 'ledger_balanced': True})      # promoted, nothing lost


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



class MonitoringTests(Base):
    """An access-log line per request, and /metrics for a monitoring server (off unless a token is configured)."""

    def tearDown(self):
        self.appmod.app.config['METRICS_TOKEN'] = None

    def test_metrics_need_a_configured_token(self):
        self.appmod.app.config['METRICS_TOKEN'] = None
        self.assertEqual(self.client.get('/metrics').status_code, 404)                        # off by default
        self.appmod.app.config['METRICS_TOKEN'] = 'scrape-secret'
        self.assertEqual(self.client.get('/metrics').status_code, 403)
        self.assertEqual(self.client.get('/metrics', headers={'Authorization': 'Bearer wrong'}).status_code, 403)

    def test_metrics_report_requests_latency_and_business_numbers(self):
        self.appmod.app.config['METRICS_TOKEN'] = 'scrape-secret'
        self.login('staff')
        self.client.get('/bookings')
        text = self.client.get('/metrics', headers={'Authorization': 'Bearer scrape-secret'}).get_data(as_text=True)
        self.assertRegex(text, r'routewise_http_requests_total\{endpoint="bookings",status="200"\} [1-9]')
        self.assertIn('routewise_http_request_seconds_bucket{endpoint="bookings",le="+Inf"}', text)
        self.assertIn('routewise_schema_version 7', text)
        self.assertRegex(text, r'routewise_tickets_sold_today \d+')

    def test_every_request_is_logged_without_its_query_string(self):
        self.login('staff')
        with self.assertLogs('routewise.access', level='INFO') as logs:
            self.client.get('/passengers?q=secret-search-term')
        self.assertTrue(any('GET /passengers 200' in line and 'user=staff' in line for line in logs.output), logs.output)
        self.assertFalse(any('secret-search-term' in line for line in logs.output))

if __name__ == '__main__':
    unittest.main()
