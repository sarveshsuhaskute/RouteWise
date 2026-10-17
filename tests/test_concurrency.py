"""Transactions, rollback, concurrent sessions and isolation levels."""
import os
import subprocess
import sys
import unittest

from support import ROOT, Base, build_database, drop_database


def setUpModule():
    build_database()


def tearDownModule():
    drop_database()


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
        self.post('/book', p_id=5, s_id=8, seat='6B', fare=40)                     # passenger 5 has no card (no discount)
        old = self.ticket(8, '6B')
        self.assertIn('moved to schedule #2', self.post(f"/transfer/{old['TicketID']}", s_id=2, seat='9b'))
        self.assertIsNone(self.ticket(8, '6B'))                                    # old seat freed
        new = self.ticket(2, '9B')                                                 # seat upper-cased
        self.assertEqual(new['PassengerID'], 5)
        self.assertEqual(float(new['Fare']), 38.25)                                # CalcFare for the new route (25.5 km x 1.5)
        actions = [r['Action'] for r in self.appmod.query(
            "SELECT Action FROM TicketAudit WHERE TicketID IN (%s, %s) ORDER BY AuditID", (old['TicketID'], new['TicketID']))]
        self.assertEqual(actions, ['BOOKED', 'CANCELLED', 'BOOKED'])

    def test_failed_transfer_rolls_everything_back(self):
        self.post('/book', p_id=5, s_id=8, seat='6C', fare=40)
        self.post('/book', p_id=6, s_id=2, seat='9C', fare=40)                     # the seat the first passenger wants
        before = self.counts()
        mine = self.ticket(8, '6C')
        self.assertIn('Seat already booked', self.post(f"/transfer/{mine['TicketID']}", s_id=2, seat='9C'))
        self.assertEqual(self.ticket(8, '6C')['TicketID'], mine['TicketID'])       # original ticket untouched
        self.assertEqual(self.counts(), before)                                    # no ticket and no audit row appeared or vanished

    def test_transfer_to_a_vehicle_under_maintenance_rolls_back(self):
        self.post('/vehicles/add', vehicle_no='KA-00-TRM', type='Bus', capacity=10, status='Maintenance')
        vid = self.appmod.query("SELECT VehicleID v FROM Vehicle WHERE VehicleNo='KA-00-TRM'")[0]['v']
        self.post('/schedules/add', route_id=1, vehicle_id=vid, departure='23:00', arrival='23:30')
        sid = self.appmod.query("SELECT MAX(ScheduleID) m FROM Schedule")[0]['m']
        self.post('/book', p_id=7, s_id=8, seat='7A', fare=40)
        before, mine = self.counts(), self.ticket(8, '7A')
        self.assertIn('not in service', self.post(f"/transfer/{mine['TicketID']}", s_id=sid, seat='1A'))
        self.assertIsNotNone(self.ticket(8, '7A'))
        self.assertEqual(self.counts(), before)

    def test_unknown_ticket_and_missing_fields(self):
        self.assertIn('Ticket not found', self.post('/transfer/999999', s_id=2, seat='11A'))
        self.assertIn('Choose the new departure', self.post('/transfer/1', s_id='', seat=''))

    def test_only_staff_and_admin_can_transfer(self):
        self.client = self.appmod.app.test_client()
        self.client.post('/login', data={'username': 'viewer', 'password': 'viewer123', 'csrf_token': self.token()})
        self.assertIn('not allowed', self.post('/transfer/1', s_id=2, seat='11A'))
        self.assertNotIn('transferModal', self.client.get('/bookings').get_data(as_text=True))


class ConcurrencyTests(Base):
    """Several sessions at once: transfers in opposite directions neither lose nor duplicate a ticket."""

    def test_opposite_transfers_never_lose_or_duplicate_tickets(self):
        import threading
        self.login('admin')
        group_a, group_b = range(1, 7), range(7, 13)
        seats_a, seats_b = ['8A', '8B', '8C', '9A', '9B', '9C'], ['8A', '8B', '8C', '8D', '9A', '9B']   # on schedules 8 and 9
        new_a, new_b = ['10A', '10B', '10C', '10D', '6A', '6B'], ['1A', '1B', '1C', '2A', '2B', '2C']   # free seats on 9 and 8
        for i, seat in zip(group_a, seats_a):
            self.post('/book', p_id=i, s_id=8, seat=seat, fare='')
        for i, seat in zip(group_b, seats_b):
            self.post('/book', p_id=i, s_id=9, seat=seat, fare='')
        ids = {r['PassengerID']: r['TicketID'] for r in self.appmod.query(
            "SELECT PassengerID, TicketID FROM Ticket WHERE TravelDate = CURDATE() AND "
            "((ScheduleID = 8 AND SeatNo IN ('8A','8B','8C','9A','9B','9C')) OR (ScheduleID = 9 AND SeatNo IN ('8A','8B','8C','8D','9A','9B')))")}
        self.assertEqual(len(ids), 12)
        barrier, errors = threading.Barrier(12), []

        def move(passenger, target, seat):
            barrier.wait()
            try:
                self.appmod.call('TransferTicket', [ids[passenger], target, seat, None])     # call() retries a deadlock victim
            except Exception as err:                                                         # noqa: BLE001
                errors.append(repr(err))
        threads = [threading.Thread(target=move, args=(i, 9, seat)) for i, seat in zip(group_a, new_a)] + \
                  [threading.Thread(target=move, args=(i, 8, seat)) for i, seat in zip(group_b, new_b)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        rows = self.appmod.query("SELECT PassengerID, ScheduleID FROM Ticket WHERE TravelDate = CURDATE() AND "
                                 "((ScheduleID = 9 AND SeatNo IN ('10A','10B','10C','10D','6A','6B')) "
                                 "OR (ScheduleID = 8 AND SeatNo IN ('1A','1B','1C','2A','2B','2C')))")
        self.assertEqual(sorted(r['PassengerID'] for r in rows), list(range(1, 13)))         # exactly one ticket each ...
        self.assertTrue(all((r['ScheduleID'] == 9) == (r['PassengerID'] <= 6) for r in rows))  # ... on the other departure
        self.assertEqual(self.appmod.query('SELECT COUNT(*) n FROM Ticket WHERE TicketID IN (%s)' % ','.join(map(str, ids.values())))[0]['n'], 0)


class IsolationDemoTests(unittest.TestCase):
    def test_demo_script_shows_the_documented_effects(self):
        import json
        out = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'isolation_demo.py'), '--json'], capture_output=True, text=True,
                             env=dict(os.environ, MYSQL_PWD=os.environ.get('MYSQL_ADMIN_PASSWORD', '')), check=True).stdout
        r = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(r['non_repeatable_read'], {'READ COMMITTED': [10, 200], 'REPEATABLE READ': [10, 10]})
        self.assertEqual(r['phantom'], {'READ COMMITTED': [1, 2], 'REPEATABLE READ': [1, 1]})
        self.assertEqual(r['lost_update'], {'unsafe': 11, 'lock': 12, 'atomic': 12})        # the lost update, and the two cures


if __name__ == '__main__':
    unittest.main()
