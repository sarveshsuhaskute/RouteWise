"""Rules the database enforces for every account: privileges, update rules, the ledger, regressions."""
import os
import subprocess
import unittest
from datetime import date, timedelta

from support import TEST_DB, Base, build_database, drop_database, mysql


def setUpModule():
    build_database()


def tearDownModule():
    drop_database()


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
                     'UPDATE Payment SET Amount = 1', 'DELETE FROM Payment',                   # the ledger is written by triggers only
                     'UPDATE Ticket SET Fare = 1', 'DELETE FROM Ticket',                       # tickets change only through procedures
                     "INSERT INTO Ticket (PassengerID, ScheduleID, TravelDate, SeatNo, Fare, BookingDate) "
                     "VALUES (1, 1, '2026-01-01', '9C', 0.01, '2025-12-31')",                  # ... and are sold only through BookTicket
                     'UPDATE Schedule SET Frequency = 5'):
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
        cur.execute('SELECT COUNT(*) FROM TicketAudit')
        before = cur.fetchone()[0]
        cur.callproc('BookTicket', [13, 9, '7B', 10, None])
        conn.commit()               # the app has no INSERT right on TicketAudit...
        cur.execute('SELECT COUNT(*) FROM TicketAudit')
        after = cur.fetchone()[0]
        conn.close()
        self.assertEqual(after, before + 1)                                          # ...yet the trigger recorded the booking

    def test_updatable_view_with_check_option(self):
        # a vehicle nobody is booked on, so only the view's own rule can refuse (CheckVehicleChange guards booked ones,
        # and whether a seed vehicle still has trips ahead today depends on the time the tests run)
        mysql(f"INSERT INTO {TEST_DB}.Vehicle (VehicleNo, Type, Capacity, Status) VALUES ('KA-00-VIEW', 'Bus', 8, 'Active')")
        vid = self.appmod.query("SELECT VehicleID v FROM Vehicle WHERE VehicleNo = 'KA-00-VIEW'")[0]['v']
        active = self.appmod.query("SELECT COUNT(*) n FROM Vehicle WHERE Status='Active'")[0]['n']
        self.assertEqual(self.appmod.query("SELECT COUNT(*) n FROM ActiveVehicle")[0]['n'], active)
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            mysql_run = dict(os.environ, MYSQL_PWD=os.environ.get('MYSQL_ADMIN_PASSWORD', ''))
            subprocess.run(['mysql', '-u', os.environ.get('MYSQL_ADMIN_USER', 'root'), TEST_DB, '-e',
                            f"UPDATE ActiveVehicle SET Status='Retired' WHERE VehicleID={vid}"],
                           env=mysql_run, check=True, capture_output=True, text=True)
        self.assertIn('CHECK OPTION failed', cm.exception.stderr)
        self.assertEqual(self.appmod.query("SELECT Status FROM Vehicle WHERE VehicleID=%s", (vid,))[0]['Status'], 'Active')


class UpdateRuleTests(Base):
    """Changing a row is checked as strictly as creating it, for every account (these statements run as root)."""

    def setUp(self):
        super().setUp()
        self.login('admin')
        self.day = date.today() + timedelta(days=1)

    def vehicle(self, number, capacity, per_row=4, vtype='Bus'):
        self.post('/vehicles/add', vehicle_no=number, type=vtype, capacity=capacity, seats_per_row=per_row, status='Active')
        return self.appmod.query('SELECT VehicleID v FROM Vehicle WHERE VehicleNo = %s', (number,))[0]['v']

    def departure(self, vid, dep, arr):
        self.post('/schedules/add', route_id=1, vehicle_id=vid, departure=dep, arrival=arr)
        return self.appmod.query('SELECT MAX(ScheduleID) m FROM Schedule')[0]['m']

    def refused(self, sql, message):
        with self.assertRaises(subprocess.CalledProcessError, msg=sql) as cm:
            mysql(f'USE {TEST_DB}; {sql}')
        self.assertIn(message, cm.exception.stderr, sql)

    def test_a_sold_ticket_cannot_be_edited(self):
        import mysql.connector
        tid = self.appmod.query('SELECT MIN(TicketID) t FROM Ticket')[0]['t']
        before = self.appmod.query('SELECT Fare, SeatNo FROM Ticket WHERE TicketID = %s', (tid,))[0]
        self.refused(f'UPDATE Ticket SET Fare = 9999 WHERE TicketID = {tid}', 'cannot be edited')
        self.refused(f"UPDATE Ticket SET SeatNo = '99F' WHERE TicketID = {tid}", 'cannot be edited')
        with self.assertRaises(mysql.connector.Error) as cm:                                    # the app user may not even try
            self.appmod.execute('UPDATE Ticket SET Fare = 1 WHERE TicketID = %s', (tid,))
        self.assertEqual(cm.exception.errno, 1142)
        self.assertEqual(self.appmod.query('SELECT Fare, SeatNo FROM Ticket WHERE TicketID = %s', (tid,))[0], before)
        self.assertEqual(self.appmod.query('SELECT (SELECT SUM(Amount) FROM Payment) = (SELECT SUM(Fare) FROM Ticket) ok')[0]['ok'], 1)

    def test_departure_changes_are_checked_like_new_departures(self):
        eight, four, forty = self.vehicle('KA-00-UPA', 8), self.vehicle('KA-00-UPB', 4), self.vehicle('KA-00-UPC', 40)
        sid = self.departure(eight, '21:00', '21:30')
        self.assertIn('Ticket booked', self.post('/book', p_id=5, s_id=sid, seat='2C', fare='', travel_date=self.day.isoformat()))
        self.refused(f'UPDATE Schedule SET VehicleID = {four} WHERE ScheduleID = {sid}', 'do not exist on the new vehicle')   # no row 2
        self.refused(f'UPDATE Schedule SET RouteID = 3 WHERE ScheduleID = {sid}', 'does not match the route type')         # a metro line
        self.refused(f'UPDATE Schedule SET RouteID = 2 WHERE ScheduleID = {sid}', 'cannot move to another route')          # tickets sold
        mysql(f'USE {TEST_DB}; UPDATE Schedule SET VehicleID = {forty} WHERE ScheduleID = {sid}')                        # 2C exists there
        other = self.departure(forty, '21:40', '21:50')
        self.refused(f"UPDATE Schedule SET DepartureTime = '21:35', ArrivalTime = '21:45' WHERE ScheduleID = {sid}", 'already has a run')
        mysql(f"USE {TEST_DB}; UPDATE Schedule SET DepartureTime = '21:05', ArrivalTime = '21:35', Frequency = 20 WHERE ScheduleID = {sid}")
        mysql(f"USE {TEST_DB}; INSERT INTO VehicleSwap VALUES ({sid}, '{self.day}', {eight}, 'tyre change')")
        self.refused(f"UPDATE Schedule SET ArrivalTime = '21:38' WHERE ScheduleID = {sid}", 'Remove the vehicle swaps')
        self.refused(f"UPDATE VehicleSwap SET VehicleID = {four} WHERE ScheduleID = {sid}", 'Remove the swap and add a new one')
        mysql(f"USE {TEST_DB}; UPDATE VehicleSwap SET Reason = 'tyre change (rear)' WHERE ScheduleID = {sid}")         # wording may change
        self.assertEqual(self.appmod.query('SELECT VehicleID, DepartureTime FROM Schedule WHERE ScheduleID = %s', (sid,))[0]['VehicleID'], forty)
        self.assertNotEqual(other, sid)

    def test_vehicle_seat_and_service_day_changes(self):
        vid = self.vehicle('KA-00-UPD', 8)
        self.departure(vid, '21:00', '21:30')
        out = self.post(f'/vehicles/{vid}/edit', vehicle_no='KA-00-UPD', type='Metro', capacity=8, seats_per_row=4, status='Active')
        self.assertIn('cannot change type', out)                                                    # shown to the admin in the app
        spare = self.vehicle('KA-00-UPE', 8)
        mysql(f"USE {TEST_DB}; UPDATE Vehicle SET Type = 'Metro' WHERE VehicleID = {spare}")       # runs nothing yet: allowed
        self.refused(f"UPDATE Seat SET SeatNo = '9Z' WHERE VehicleID = {vid} AND SeatNo = '1A'", 'Seat labels follow')
        mysql(f"USE {TEST_DB}; UPDATE Seat SET Position = 'Aisle' WHERE VehicleID = {vid} AND SeatNo = '1A'")
        self.refused('UPDATE ScheduleDay SET Weekday = 1 WHERE ScheduleID = 11 LIMIT 1', 'Add or remove a service day')

    def test_ledger_and_audit_trail_are_append_only_even_for_root(self):
        counts = 'SELECT (SELECT COUNT(*) FROM Payment) p, (SELECT COUNT(*) FROM TicketAudit) a'
        before = self.appmod.query(counts)[0]
        self.refused('UPDATE Payment SET Amount = 1', 'ledger is append-only')
        self.refused('DELETE FROM Payment', 'ledger is append-only')
        self.refused("UPDATE TicketAudit SET Action = 'FORGED'", 'audit trail is append-only')
        self.refused('DELETE FROM TicketAudit', 'audit trail is append-only')
        self.assertEqual(self.appmod.query(counts)[0], before)

    def test_tickets_are_sold_only_through_book_ticket(self):
        import mysql.connector
        with self.assertRaises(mysql.connector.Error) as cm:                                     # no back-dated, self-priced ticket
            self.appmod.execute("INSERT INTO Ticket (PassengerID, ScheduleID, TravelDate, SeatNo, Fare, BookingDate) "
                                "VALUES (1, 1, CURDATE() - INTERVAL 40 DAY, '9C', 0.01, NOW() - INTERVAL 41 DAY)")
        self.assertEqual(cm.exception.errno, 1142)
        self.assertIn('in the past', self.post('/book', p_id=1, s_id=1, seat='9C', fare='',
                                               travel_date=(date.today() - timedelta(days=1)).isoformat()))

    def test_a_route_with_departures_keeps_its_type(self):
        self.refused("UPDATE Route SET Type = 'Metro' WHERE RouteID = 1", 'cannot change type')
        self.assertIn('Route added', self.post('/routes/add', route_name='Empty Line', origin='A', destination='B', type='Bus', distance='5'))
        mysql(f"USE {TEST_DB}; UPDATE Route SET Type = 'Metro' WHERE RouteName = 'Empty Line'")      # no departures yet: allowed

    def test_holidays_never_strand_sold_tickets(self):
        weekday_only, daily = self.vehicle('KA-00-UPH', 8), self.vehicle('KA-00-UPI', 8)
        mysql(f"USE {TEST_DB}; CALL AddSchedule(1, {weekday_only}, '21:00', '21:30', NULL, 'Mon,Tue,Wed,Thu,Fri,Sat,Sun', FALSE)")
        no_holidays = self.appmod.query('SELECT MAX(ScheduleID) m FROM Schedule')[0]['m']
        holiday_ok = self.departure(daily, '21:00', '21:30')                                     # runs on holidays
        d1, d2 = date.today() + timedelta(days=5), date.today() + timedelta(days=6)
        try:
            self.assertIn('Ticket booked', self.post('/book', p_id=6, s_id=no_holidays, seat='1A', fare='', travel_date=d1.isoformat()))
            self.assertIn('do not run on holidays', self.post('/holidays/add', holiday_date=d1.isoformat(), name='Probe Day'))
            self.assertEqual(self.appmod.query('SELECT COUNT(*) n FROM Holiday WHERE HolidayDate = %s', (d1,))[0]['n'], 0)
            mysql(f"USE {TEST_DB}; INSERT INTO Holiday VALUES ('{d2}', 'Second Probe Day')")
            self.refused(f"UPDATE Holiday SET HolidayDate = '{d1}' WHERE HolidayDate = '{d2}'", 'do not run on holidays')
            self.assertIn('Ticket booked', self.post('/book', p_id=7, s_id=holiday_ok, seat='1B', fare='', travel_date=d2.isoformat()))
            self.refused(f'UPDATE Schedule SET RunsOnHolidays = FALSE WHERE ScheduleID = {holiday_ok}', 'upcoming public holiday')
        finally:
            mysql(f"USE {TEST_DB}; DELETE FROM Holiday WHERE Name IN ('Probe Day', 'Second Probe Day')")

    def test_a_vehicle_leaves_service_only_without_booked_passengers(self):
        busy, idle = self.vehicle('KA-00-UPJ', 8), self.vehicle('KA-00-UPK', 8)
        sid = self.departure(busy, '21:00', '21:30')
        self.assertIn('Ticket booked', self.post('/book', p_id=8, s_id=sid, seat='1C', fare='', travel_date=self.day.isoformat()))
        out = self.post(f'/vehicles/{busy}/edit', vehicle_no='KA-00-UPJ', type='Bus', capacity=8, seats_per_row=4, status='Maintenance')
        self.assertIn('swap in another vehicle', out)                                          # the admin sees why
        self.assertIn('uses another vehicle', self.post('/swaps/add', s_id=sid, vehicle_id=idle, travel_date=self.day.isoformat(), reason='brakes'))
        out = self.post(f'/vehicles/{busy}/edit', vehicle_no='KA-00-UPJ', type='Bus', capacity=8, seats_per_row=4, status='Maintenance')
        self.assertIn('Vehicle updated', out)                                                  # the trip is covered now
        self.assertEqual(self.appmod.query('SELECT Status FROM Vehicle WHERE VehicleID = %s', (busy,))[0]['Status'], 'Maintenance')


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
        e = self.client.get('/tickets/1').get_data(as_text=True)
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


if __name__ == '__main__':
    unittest.main()
