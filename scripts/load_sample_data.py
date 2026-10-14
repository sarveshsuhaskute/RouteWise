"""Load a large, realistic demo data set on top of the small seed data in schema.sql.

    .venv/bin/python scripts/load_sample_data.py              # full size (about 3,000 passengers, 25,000 tickets)
    .venv/bin/python scripts/load_sample_data.py --scale 0.1  # a tenth of that

What it adds (at --scale 1): 30 routes, 66 drivers, 66 vehicles, about 600 departures, 3,000 passengers,
about 25,000 tickets spread over the last 45 days, and about 1,500 feedback entries.

Every row goes through the application's own database user (routewise_app), so it passes the same constraints and
triggers as real use: no double seats, no overbooked vehicles, no overlapping runs, an audit row for every ticket.
The data is deterministic (fixed random seed) and the script does nothing if the data is already loaded.
Connection settings: DB_HOST, DB_USER, DB_PASSWORD, DB_NAME (same defaults as the application).
"""
import argparse
import os
import random
import sys
import time
from datetime import datetime, timedelta

import mysql.connector

FIRST = ['Aarav', 'Aditi', 'Akash', 'Ananya', 'Arjun', 'Bhavya', 'Charan', 'Deepa', 'Divya', 'Farhan', 'Gayathri', 'Harish',
         'Ishaan', 'Jyoti', 'Kavya', 'Karthik', 'Lakshmi', 'Manoj', 'Meera', 'Naveen', 'Nisha', 'Pooja', 'Pranav', 'Priya',
         'Rahul', 'Rohan', 'Sanjana', 'Santhosh', 'Shreya', 'Suresh', 'Tanvi', 'Uday', 'Varun', 'Vidya', 'Vikram', 'Yash']
LAST = ['Reddy', 'Nair', 'Gowda', 'Hegde', 'Iyer', 'Kumar', 'Patel', 'Rao', 'Shetty', 'Singh', 'Sharma', 'Joshi', 'Menon',
        'Naik', 'Bhat', 'Kulkarni', 'Pillai', 'Das', 'Verma', 'Desai', 'Murthy', 'Prasad', 'Acharya', 'Khan']
AREAS = ['Majestic', 'Silk Board', 'Whitefield', 'Electronic City', 'Hebbal', 'Yelahanka', 'Jayanagar', 'Banashankari',
         'Marathahalli', 'KR Puram', 'Indiranagar', 'MG Road', 'Koramangala', 'HSR Layout', 'Yeshwanthpur', 'Rajajinagar',
         'Kengeri', 'Bannerghatta', 'Sarjapur', 'Domlur', 'Nagawara', 'Banaswadi', 'Peenya', 'Vijayanagar', 'JP Nagar']
BUS_MODELS = ['Tata Starbus', 'Ashok Leyland Viking', 'Volvo 8400', 'Eicher Skyline', 'Tata Marcopolo']
METRO_MODELS = ['BEML Metro', 'Bombardier Movia']
COMMENTS = {5: ['Clean, on time and comfortable.', 'Excellent service, will use again.', 'Very smooth and fast.', 'Great frequency.'],
            4: ['Good service overall.', 'Mostly on time, a bit crowded at peak.', 'Comfortable ride.', 'Reliable.'],
            3: ['Average. Could be cleaner.', 'Late a few times this week.', 'Seats are cramped.', 'Okay for the price.'],
            2: ['Often delayed.', 'Overcrowded in the morning.', 'Needs better maintenance.'],
            1: ['Very late and crowded.', 'Poor experience.']}


def connect():
    return mysql.connector.connect(
        host=os.environ.get('DB_HOST', 'localhost'), user=os.environ.get('DB_USER', 'routewise_app'),
        password=os.environ.get('DB_PASSWORD', 'routewise_app_pw'), database=os.environ.get('DB_NAME', 'public_transport'))


def weighted(rng, pairs):
    r, acc = rng.random(), 0
    for value, weight in pairs:
        acc += weight
        if r <= acc:
            return value
    return pairs[-1][0]


def seat_labels(capacity):
    return [f'S{i}' for i in range(1, capacity + 1)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--scale', type=float, default=1.0, help='1.0 = about 3,000 passengers and 25,000 tickets')
    args = ap.parse_args()
    s = max(args.scale, 0.01)
    rng = random.Random(42)
    conn = connect()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM Passenger WHERE Email LIKE '%@demo.example'")
    if cur.fetchone()[0] > 0:
        print('Sample data is already loaded (passengers @demo.example exist). Nothing to do.')
        return
    started = time.time()

    # ---- routes ---------------------------------------------------------------------------------
    n_routes = max(int(25 * s), 5)          # at least one metro route (every fifth route is metro)
    routes = []                                           # (RouteID, Type, Distance)
    used_pairs, bus_no = set(), 601
    for i in range(n_routes):
        while True:
            a, b = rng.sample(AREAS, 2)
            if (a, b) not in used_pairs:
                used_pairs.add((a, b))
                break
        rtype = 'Metro' if i % 5 == 4 else 'Bus'
        dist = round(rng.uniform(12, 44) if rtype == 'Metro' else rng.uniform(7, 38), 2)
        name = f'{a.split()[0]} Line' if rtype == 'Metro' else f'Route {bus_no}'
        bus_no += rtype == 'Bus'
        if rtype == 'Metro':
            name = f'{name} {i}'
        cur.execute('INSERT INTO Route (RouteName, Origin, Destination, Distance, Type) VALUES (%s,%s,%s,%s,%s)', (name, a, b, dist, rtype))
        routes.append((cur.lastrowid, rtype, dist))

    # ---- drivers and vehicles -------------------------------------------------------------------
    n_vehicles = max(int(60 * s), 6)
    driver_ids = []
    for i in range(n_vehicles):
        name = f'{rng.choice(FIRST)} {rng.choice(LAST)}'
        cur.execute('INSERT INTO Driver (Name, LicenseNo, ShiftHours, Phone) VALUES (%s,%s,%s,%s)',
                    (name, f'KA-01-D{i + 1:05d}', rng.randint(6, 10), f'98{rng.randint(10000000, 99999999)}'))
        driver_ids.append(cur.lastrowid)
    vehicles = []                                         # (VehicleID, Type, Capacity, Status)
    for i in range(n_vehicles):
        vtype = 'Metro' if i % 4 == 3 else 'Bus'
        cap = rng.randint(90, 140) if vtype == 'Metro' else rng.randint(35, 50)
        status = weighted(rng, [('Active', 0.92), ('Maintenance', 0.05), ('Retired', 0.03)])
        model = rng.choice(METRO_MODELS if vtype == 'Metro' else BUS_MODELS)
        cur.execute('INSERT INTO Vehicle (VehicleNo, Type, Capacity, Model, Status, DriverID) VALUES (%s,%s,%s,%s,%s,%s)',
                    (f'KA-02-{"M" if vtype == "Metro" else "F"}-{2000 + i}', vtype, cap, model, status, driver_ids[i]))
        vehicles.append((cur.lastrowid, vtype, cap, status))
    conn.commit()

    # ---- schedules: back-to-back runs for every active vehicle, 05:00 to about 22:30 -------------
    schedules = []                                        # (ScheduleID, RouteID, VehicleID, Departure(min), Capacity)
    for vid, vtype, cap, status in vehicles:
        if status != 'Active':
            continue
        my_routes = [r for r in routes if r[1] == vtype]
        clock = 5 * 60 + rng.randint(0, 40)
        while True:
            rid, _, dist = rng.choice(my_routes)
            minutes = int(dist / (35 if vtype == 'Metro' else 22) * 60) + 5
            if clock + minutes > 22 * 60 + 30:
                break
            dep, arr = clock, clock + minutes
            cur.execute('INSERT INTO Schedule (RouteID, VehicleID, DepartureTime, ArrivalTime, Frequency) VALUES (%s,%s,%s,%s,%s)',
                        (rid, vid, f'{dep // 60:02d}:{dep % 60:02d}:00', f'{arr // 60:02d}:{arr % 60:02d}:00', rng.choice([10, 15, 20, 30, 60])))
            schedules.append((cur.lastrowid, rid, vid, dep, cap))
            clock = arr + rng.randint(15, 35)
    conn.commit()

    # ---- passengers -----------------------------------------------------------------------------
    n_pass = max(int(3000 * s), 40)
    passengers = []
    for i in range(n_pass):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        card = weighted(rng, [('None', 0.45), ('Daily', 0.15), ('Weekly', 0.20), ('Monthly', 0.20)])
        dob = datetime(1960, 1, 1) + timedelta(days=rng.randint(0, 17000))
        cur.execute('INSERT INTO Passenger (Name, Email, Phone, DOB, CardType) VALUES (%s,%s,%s,%s,%s)',
                    (f'{first} {last}', f'{first}.{last}{i + 1}@demo.example'.lower(), f'9{rng.randint(100000000, 999999999)}', dob.date(), card))
        passengers.append(cur.lastrowid)
    conn.commit()

    # ---- tickets: peak hours fuller, spread over the last 45 days; every row passes the triggers ----
    now = datetime.now()
    tickets = failed = 0
    for sid, rid, vid, dep, cap in schedules:
        hour = dep // 60
        peak = 0.85 if 7 <= hour <= 10 or 17 <= hour <= 20 else 0.55
        fill = min(max(rng.gauss(peak, 0.15), 0.1), 0.98)
        n = max(int(cap * fill), 1)
        riders = rng.sample(passengers, min(n, len(passengers)))
        seats = rng.sample(seat_labels(cap), len(riders))
        rows = [(p, sid, seat, p, sid, now - timedelta(days=rng.random() * 45, minutes=rng.randint(0, 1440))) for p, seat in zip(riders, seats)]
        try:
            cur.executemany('INSERT INTO Ticket (PassengerID, ScheduleID, SeatNo, Fare, BookingDate) '
                            'SELECT %s, %s, %s, CalcFare(%s, %s), %s', rows)
            conn.commit()
            tickets += len(rows)
        except mysql.connector.Error as err:        # a rule rejected the batch: nothing half-written
            conn.rollback()
            failed += 1
            print(f'  schedule {sid}: {err.msg}', file=sys.stderr)

    # ---- feedback ---------------------------------------------------------------------------------
    n_fb = max(int(1500 * s), 20)
    for _ in range(n_fb):
        rating = weighted(rng, [(5, 0.30), (4, 0.35), (3, 0.20), (2, 0.10), (1, 0.05)])
        cur.execute('INSERT INTO Feedback (PassengerID, RouteID, Rating, Comments, FeedbackDate) VALUES (%s,%s,%s,%s,%s)',
                    (rng.choice(passengers), rng.choice(routes)[0], rating, rng.choice(COMMENTS[rating]),
                     now - timedelta(days=rng.random() * 45)))
    conn.commit()

    cur.execute('SELECT (SELECT COUNT(*) FROM Passenger), (SELECT COUNT(*) FROM Route), (SELECT COUNT(*) FROM Vehicle), '
                '(SELECT COUNT(*) FROM Driver), (SELECT COUNT(*) FROM Schedule), (SELECT COUNT(*) FROM Ticket), '
                '(SELECT COUNT(*) FROM Feedback), (SELECT COUNT(*) FROM TicketAudit)')
    p, r, v, d, sc, t, f, a = cur.fetchone()
    print(f'Loaded in {time.time() - started:.1f}s (rejected batches: {failed}).')
    print(f'Database now holds: {p:,} passengers, {r} routes, {v} vehicles, {d} drivers, {sc} departures, '
          f'{t:,} tickets, {f:,} feedback entries, {a:,} audit rows.')
    conn.close()


if __name__ == '__main__':
    main()
