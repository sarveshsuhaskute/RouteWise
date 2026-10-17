"""Routes, departures, service days, holidays, vehicle swaps and the departure board."""
import os
import subprocess
import unittest
from datetime import date, timedelta

from support import TEST_DB, Base, build_database, drop_database


def setUpModule():
    build_database()


def tearDownModule():
    drop_database()


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
        self.assertEqual(self.appmod.query('SELECT MAX(Version) v FROM SchemaVersion')[0]['v'], 7)


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
        found = self.appmod.query('SELECT ScheduleID, RunsToday FROM ScheduleOccupancy WHERE ScheduleID IN (11, 12)')
        rows = {r['ScheduleID']: bool(r['RunsToday']) for r in found}
        self.assertEqual(rows, expected)
        self.assertIsNone(self.appmod.query('SELECT RunsOn(999999, CURDATE()) r')[0]['r'])

    def test_schedule_form_stores_service_days(self):
        self.post('/vehicles/add', vehicle_no='KA-00-SVC', type='Bus', capacity=10, status='Active')
        vid = self.appmod.query("SELECT VehicleID v FROM Vehicle WHERE VehicleNo = 'KA-00-SVC'")[0]['v']
        self.assertIn('Schedule added', self.post('/schedules/add', route_id=2, vehicle_id=vid, departure='21:00', arrival='21:40', days=['Sat', 'Sun']))
        sid = self.appmod.query('SELECT ScheduleID, RunsOnHolidays FROM Schedule WHERE VehicleID = %s', (vid,))[0]
        days = [r['Weekday'] for r in self.appmod.query('SELECT Weekday FROM ScheduleDay WHERE ScheduleID = %s ORDER BY Weekday', (sid['ScheduleID'],))]
        self.assertEqual((days, sid['RunsOnHolidays']), ([1, 7], 0))                          # Sunday and Saturday rows
        days = self.appmod.query('SELECT ServiceDays FROM ScheduleOccupancy WHERE ScheduleID = %s', (sid['ScheduleID'],))[0]['ServiceDays']
        self.assertEqual(days, 'Sat,Sun')
        self.assertIn('Weekends', self.client.get('/routes?q=KA-00-SVC').get_data(as_text=True))

    def test_holidays_are_admin_only_and_validated(self):
        self.assertIn('valid date', self.post('/holidays/add', holiday_date='not-a-date', name='X'))
        self.assertIn('Duplicate', self.post('/holidays/add', holiday_date='2026-12-25', name='Christmas again'))
        self.client = self.appmod.app.test_client()
        self.client.post('/login', data={'username': 'staff', 'password': 'staff123', 'csrf_token': self.token()})
        self.assertIn('not allowed', self.post('/holidays/add', holiday_date='2027-03-01', name='Nope'))


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
        # only the 6-across bus has an F seat
        self.assertIn('Ticket booked', self.post('/book', p_id=2, s_id=sid, seat='2F', fare='', travel_date=self.day.isoformat()))
        self.assertIn('No such seat', self.post('/book', p_id=3, s_id=sid, seat='3A', fare='', travel_date=self.day.isoformat()))    # 12 seats: rows 1-2
        api = self.client.get(f'/api/seats?s={sid}&d={self.day.isoformat()}').get_json()
        self.assertEqual((api['capacity'], api['swapped']), (12, 'KA-00-SWB'))
        booked = self.appmod.query("SELECT VehicleNo FROM BookingSummary WHERE SeatNo = '2F' AND TravelDate = %s", (self.day,))
        self.assertEqual(booked[0]['VehicleNo'], 'KA-00-SWB')
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


if __name__ == '__main__':
    unittest.main()
