"""Booking a seat on a trip: fares, dates, the seat map, seat holds and e-tickets."""
import re
import unittest
from datetime import date, timedelta

from support import TEST_DB, Base, build_database, drop_database, mysql


def setUpModule():
    build_database()


def tearDownModule():
    drop_database()


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


if __name__ == '__main__':
    unittest.main()
