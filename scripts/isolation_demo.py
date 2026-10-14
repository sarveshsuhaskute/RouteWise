"""Transaction isolation and locking, demonstrated with two real connections on a scratch database.

    .venv/bin/python scripts/isolation_demo.py          # prints a table
    .venv/bin/python scripts/isolation_demo.py --json   # machine-readable (used by the tests)

It builds a throw-away database (transitiq_isolation), plays three classic situations between two sessions A and B, and
drops the database again. These are the effects that decide how TransitIQ books a seat:

  1. Non-repeatable read:  A reads a balance, B changes it and commits, A reads again.
  2. Phantom row:          A counts rows, B inserts one and commits, A counts again.
  3. Lost update:          A and B both add 1 to the same balance.
Uses the MySQL administrator account: MYSQL_ADMIN_USER / MYSQL_ADMIN_PASSWORD (default root, empty password).
"""
import json
import os
import sys
import threading
import time

import mysql.connector

DB = 'transitiq_isolation'
ADMIN = dict(user=os.environ.get('MYSQL_ADMIN_USER', 'root'), password=os.environ.get('MYSQL_ADMIN_PASSWORD', ''),
             host=os.environ.get('DB_HOST', 'localhost'))


def connect(level=None):
    conn = mysql.connector.connect(database=DB, **ADMIN)
    if level:
        conn.cursor().execute(f'SET SESSION TRANSACTION ISOLATION LEVEL {level}')
    return conn


def one(conn, sql):
    cur = conn.cursor()
    cur.execute(sql)
    value = cur.fetchone()[0]
    cur.close()
    return int(value)


def run(conn, sql):
    cur = conn.cursor()
    cur.execute(sql)
    cur.close()


def reset():
    c = connect()
    run(c, 'TRUNCATE TABLE Account'); run(c, 'INSERT INTO Account (id, balance) VALUES (1, 10)')
    run(c, 'TRUNCATE TABLE Booking'); run(c, 'INSERT INTO Booking (trip) VALUES (1)')
    c.commit(); c.close()


def non_repeatable_read(level):
    reset()
    a, b = connect(level), connect()
    a.start_transaction()
    first = one(a, 'SELECT balance FROM Account WHERE id = 1')
    run(b, 'UPDATE Account SET balance = 200 WHERE id = 1'); b.commit()
    second = one(a, 'SELECT balance FROM Account WHERE id = 1')
    a.commit(); a.close(); b.close()
    return first, second


def phantom(level):
    reset()
    a, b = connect(level), connect()
    a.start_transaction()
    first = one(a, 'SELECT COUNT(*) FROM Booking WHERE trip = 1')
    run(b, 'INSERT INTO Booking (trip) VALUES (1)'); b.commit()
    second = one(a, 'SELECT COUNT(*) FROM Booking WHERE trip = 1')
    a.commit(); a.close(); b.close()
    return first, second


def lost_update(strategy):
    """Two sessions each add 1 to a balance of 10. The right answer is 12."""
    reset()

    def worker():
        c = connect()
        c.start_transaction()
        if strategy == 'atomic':
            run(c, 'UPDATE Account SET balance = balance + 1 WHERE id = 1')           # one statement: read and write together
        else:
            lock = ' FOR UPDATE' if strategy == 'lock' else ''
            value = one(c, f'SELECT balance FROM Account WHERE id = 1{lock}')
            time.sleep(0.4)                                                             # the other session gets in between
            run(c, f'UPDATE Account SET balance = {value + 1} WHERE id = 1')
        c.commit(); c.close()
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start(); time.sleep(0.05)
    for t in threads:
        t.join()
    c = connect()
    final = one(c, 'SELECT balance FROM Account WHERE id = 1')
    c.close()
    return final


def main():
    admin = mysql.connector.connect(**ADMIN)
    cur = admin.cursor()
    cur.execute(f'DROP DATABASE IF EXISTS {DB}')
    cur.execute(f'CREATE DATABASE {DB}')
    cur.execute(f'CREATE TABLE {DB}.Account (id INT PRIMARY KEY, balance INT NOT NULL) ENGINE=InnoDB')
    cur.execute(f'CREATE TABLE {DB}.Booking (id INT AUTO_INCREMENT PRIMARY KEY, trip INT NOT NULL, KEY (trip)) ENGINE=InnoDB')
    admin.commit()
    try:
        results = {
            'non_repeatable_read': {lvl: non_repeatable_read(lvl) for lvl in ('READ COMMITTED', 'REPEATABLE READ')},
            'phantom': {lvl: phantom(lvl) for lvl in ('READ COMMITTED', 'REPEATABLE READ')},
            'lost_update': {s: lost_update(s) for s in ('unsafe', 'lock', 'atomic')},
        }
    finally:
        cur.execute(f'DROP DATABASE IF EXISTS {DB}')
        admin.commit(); admin.close()
    if '--json' in sys.argv:
        print(json.dumps(results))
        return
    print('1. Non-repeatable read (A reads a balance of 10, B sets it to 200 and commits, A reads again)')
    for lvl, (x, y) in results['non_repeatable_read'].items():
        print(f'   {lvl:<16} A reads {x}, then {y}   ->', 'A sees the change (inconsistent)' if x != y else 'A keeps a consistent snapshot')
    print('2. Phantom row (A counts the rows of a trip, B inserts one and commits, A counts again)')
    for lvl, (x, y) in results['phantom'].items():
        print(f'   {lvl:<16} A counts {x}, then {y}   ->', 'a phantom row appears' if x != y else 'no phantom: the snapshot is stable')
    print('3. Lost update (A and B each add 1 to a balance of 10; the right answer is 12)')
    names = {'unsafe': 'read, then write the value (no lock)', 'lock': 'SELECT ... FOR UPDATE, then write', 'atomic': 'UPDATE balance = balance + 1'}
    for s, final in results['lost_update'].items():
        print(f'   {names[s]:<38} final balance {final}   ->', 'CORRECT' if final == 12 else 'one update was LOST')


if __name__ == '__main__':
    main()
