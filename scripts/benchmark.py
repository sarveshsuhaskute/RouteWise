"""Index benchmark on 200,000 tickets, in a separate database (the demo data is never touched).

    .venv/bin/python scripts/benchmark.py [rows]

It builds database `routewise_bench` from schema.sql, loads `rows` synthetic tickets (default 200000) into
TicketBench (a copy of Ticket's structure and indexes, without triggers), and times three typical queries
with and without the relevant index. Results are printed and saved to benchmarks/results.json.
Uses the MySQL admin account: MYSQL_ADMIN_USER / MYSQL_ADMIN_PASSWORD (default root, empty password).
"""
import json
import os
import statistics
import subprocess
import sys
import time

import mysql.connector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 200000
DB = 'routewise_bench'
ADMIN = dict(user=os.environ.get('MYSQL_ADMIN_USER', 'root'), password=os.environ.get('MYSQL_ADMIN_PASSWORD', ''),
             host=os.environ.get('DB_HOST', 'localhost'))


def sh_mysql(sql=None, stdin=None):
    env = dict(os.environ, MYSQL_PWD=ADMIN['password'])
    cmd = ['mysql', '-u', ADMIN['user'], '-h', ADMIN['host']] + (['-e', sql] if sql else [])
    subprocess.run(cmd, input=stdin, text=True, env=env, check=True, capture_output=True)


def timed(cur, sql, runs=15):
    cur.execute(sql); cur.fetchall()                      # warm-up
    samples = []
    for _ in range(runs):
        t = time.perf_counter(); cur.execute(sql); cur.fetchall(); samples.append((time.perf_counter() - t) * 1000)
    return round(statistics.mean(samples), 2), round(min(samples), 2)


def plan(cur, sql):
    """Access plan of the query: one entry per table (access type, index used, estimated rows)."""
    cur.execute('EXPLAIN FORMAT=TRADITIONAL ' + sql)
    return [{k: row[k] for k in ('table', 'type', 'key', 'rows')} for row in cur.fetchall()]


def main():
    with open(os.path.join(ROOT, 'schema.sql')) as f:
        schema = f.read().replace('public_transport', DB)
    sh_mysql(f'DROP DATABASE IF EXISTS {DB}')
    sh_mysql(stdin=schema)
    conn = mysql.connector.connect(database=DB, **ADMIN)
    cur = conn.cursor(dictionary=True)
    cur.execute('CREATE TABLE TicketBench LIKE Ticket')
    cur.execute(f'SET SESSION cte_max_recursion_depth = {ROWS + 10}')
    t = time.perf_counter()
    cur.execute(f"""INSERT INTO TicketBench (PassengerID, ScheduleID, SeatNo, Fare)
        WITH RECURSIVE seq (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < {ROWS})
        SELECT 1 + (n * 7919) % 5000, 1 + n % 10, CONCAT('S', n), 10 + n % 90 FROM seq""")
    conn.commit()
    load_s = round(time.perf_counter() - t, 1)
    cur.execute('ANALYZE TABLE TicketBench'); cur.fetchall()
    cur.execute('SELECT COUNT(*) AS n FROM TicketBench'); n = cur.fetchone()['n']
    print(f'Loaded {n:,} tickets in {load_s}s')

    q_passenger = 'SELECT COUNT(*) AS trips, SUM(Fare) AS spent FROM TicketBench WHERE PassengerID = 4242'
    q_seat = "SELECT TicketID FROM TicketBench WHERE ScheduleID = 7 AND SeatNo = 'S150006'"
    q_join = ('SELECT R.RouteName, COUNT(*) AS trips, SUM(T.Fare) AS spent FROM TicketBench T '
              'JOIN Schedule S ON S.ScheduleID = T.ScheduleID JOIN Route R ON R.RouteID = S.RouteID '
              'WHERE T.PassengerID = 4242 GROUP BY R.RouteName')
    experiments = [
        ('Passenger history: WHERE PassengerID = ?', q_passenger, 'idx_ticket_passenger'),
        ('Seat lookup: WHERE ScheduleID = ? AND SeatNo = ?', q_seat, 'uq_schedule_seat'),
        ('Passenger spend per route (3-table join)', q_join, 'idx_ticket_passenger'),
    ]
    results = []
    for title, sql, index in experiments:
        cur.execute(f'SHOW INDEX FROM TicketBench WHERE Key_name = %s', (index,)); cols = [r['Column_name'] for r in cur.fetchall()]
        with_plan, (with_mean, with_min) = plan(cur, sql), timed(cur, sql)
        cur.execute(f'ALTER TABLE TicketBench DROP INDEX {index}')
        without_plan, (without_mean, without_min) = plan(cur, sql), timed(cur, sql)
        cur.execute(f'ALTER TABLE TicketBench ADD {"UNIQUE " if index.startswith("uq") else ""}INDEX {index} ({", ".join(cols)})')
        results.append({'query': title, 'index': index, 'with_ms': with_mean, 'without_ms': without_mean,
                        'speedup': round(without_mean / with_mean, 1), 'with_plan': with_plan, 'without_plan': without_plan})
    conn.close()
    out = {'rows': n, 'load_seconds': load_s, 'results': results}
    os.makedirs(os.path.join(ROOT, 'benchmarks'), exist_ok=True)
    with open(os.path.join(ROOT, 'benchmarks', 'results.json'), 'w') as f:
        json.dump(out, f, indent=2)
    print(f"\n{'Query':<52}{'with index':>12}{'without':>12}{'speed-up':>10}")
    for r in results:
        print(f"{r['query']:<52}{r['with_ms']:>10} ms{r['without_ms']:>10} ms{r['speedup']:>8}x")
    sh_mysql(f'DROP DATABASE IF EXISTS {DB}')


if __name__ == '__main__':
    main()
