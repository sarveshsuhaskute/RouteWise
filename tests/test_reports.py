"""Reports, analytics, FULLTEXT search, partitioning and the scheduled events."""
import re
import unittest
from datetime import date, timedelta

from support import TEST_DB, Base, build_database, drop_database, mysql


def setUpModule():
    build_database()


def tearDownModule():
    drop_database()


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


class EventTests(Base):
    def test_old_sign_in_failures_are_purged_by_a_scheduled_event(self):
        out = mysql(f"SELECT CONCAT_WS('|', EVENT_NAME, INTERVAL_VALUE, INTERVAL_FIELD, STATUS) FROM information_schema.events "
                    f"WHERE EVENT_SCHEMA = '{TEST_DB}'").stdout                         # (the application user cannot even see events)
        for event in ('PurgeLoginAttempts|1|HOUR|ENABLED', 'PurgeSeatHolds|1|MINUTE|ENABLED', 'MaintainAuditPartitions|1|MONTH|ENABLED'):
            self.assertIn(event, out)
        self.appmod.execute("INSERT INTO LoginAttempt VALUES ('old', '1.2.3.4', 3, NOW() - INTERVAL 2 DAY), ('new', '1.2.3.4', 3, NOW())")
        body = mysql(f"SELECT EVENT_DEFINITION FROM information_schema.events "
                     f"WHERE EVENT_SCHEMA = '{TEST_DB}' AND EVENT_NAME = 'PurgeLoginAttempts'").stdout.splitlines()[-1]
        mysql(f'USE {TEST_DB}; {body}')                                                  # run exactly what the event runs every hour
        left = [r['Username'] for r in self.appmod.query("SELECT Username FROM LoginAttempt WHERE Address = '1.2.3.4'")]
        self.assertEqual(left, ['new'])


if __name__ == '__main__':
    unittest.main()
