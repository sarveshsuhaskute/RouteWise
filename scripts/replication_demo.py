"""Primary/replica replication, demonstrated on two throw-away MySQL servers (your own server is never touched).

    .venv/bin/python scripts/replication_demo.py          # prints what happens
    .venv/bin/python scripts/replication_demo.py --json   # machine-readable (used by the tests)

It initialises two empty MySQL data directories in a temporary folder and starts them on free ports:
  primary  - binary log on, GTIDs on; schema.sql is imported here
  replica  - read-only (super_read_only), replicating from the primary with GTID auto-positioning
Then it books tickets on the primary through BookTicket (the triggers fire there; row-based replication ships the
resulting rows, so triggers do not run twice), waits until the replica has them, measures the delay, shows that the
replica refuses writes, runs a report query on the replica, and finally stops both servers and deletes the folder.

Needs the `mysqld` and `mysql` programs of a MySQL 8.0.23+ installation on the PATH.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import mysql.connector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, base, name, server_id, replica=False):
        self.dir = os.path.join(base, name)
        self.port = free_port()
        self.server_id = server_id
        self.replica = replica
        self.proc = None

    def start(self):
        data = os.path.join(self.dir, 'data')
        os.makedirs(self.dir)
        subprocess.run(['mysqld', '--no-defaults', '--initialize-insecure', f'--datadir={data}'],
                       check=True, capture_output=True)
        args = ['mysqld', '--no-defaults', f'--datadir={data}', f'--port={self.port}', '--bind-address=127.0.0.1',
                f'--socket={os.path.join(self.dir, "mysql.sock")}', f'--pid-file={os.path.join(self.dir, "mysqld.pid")}',
                f'--log-error={os.path.join(self.dir, "error.log")}', f'--server-id={self.server_id}', '--mysqlx=OFF',
                '--gtid-mode=ON', '--enforce-gtid-consistency=ON', '--log-bin=binlog', '--binlog-format=ROW']
        if self.replica:
            args += ['--super-read-only=ON', '--skip-replica-start']
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(120):
            try:
                self.connect().close()
                return self
            except mysql.connector.Error:
                time.sleep(0.25)
        raise RuntimeError(f'{self.dir} did not start; see error.log there')

    def connect(self, user='root', password='', database=None):
        return mysql.connector.connect(host='127.0.0.1', port=self.port, user=user, password=password, database=database,
                                       autocommit=True)

    def sql(self, statement, database=None, user='root', password=''):
        conn = self.connect(user, password, database)
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(statement)
            rows = cur.fetchall() if cur.with_rows else []
            cur.close()
            return rows
        finally:
            conn.close()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.sql('SHUTDOWN')
                self.proc.wait(timeout=30)
            except Exception:                                   # noqa: BLE001
                self.proc.kill()


def main():
    as_json = '--json' in sys.argv
    say = (lambda *a: None) if as_json else print
    base = tempfile.mkdtemp(prefix='routewise-replication-')
    primary, replica = Server(base, 'primary', 1), Server(base, 'replica', 2, replica=True)
    result = {}
    try:
        say('Starting two throw-away MySQL servers in', base, '...')
        try:
            primary.start()
            replica.start()
        except (subprocess.CalledProcessError, RuntimeError, OSError) as err:
            print(f'Could not start private MySQL servers here ({err}); nothing was changed.', file=sys.stderr)
            sys.exit(3)
        say(f'  primary on port {primary.port}, replica on port {replica.port} (read-only)')

        # the primary gets the full RouteWise schema and a replication account
        with open(os.path.join(ROOT, 'schema.sql')) as f:
            subprocess.run(['mysql', '-h', '127.0.0.1', '-P', str(primary.port), '-u', 'root'], input=f.read(), text=True,
                           check=True, capture_output=True)
        primary.sql("CREATE USER 'repl'@'%' IDENTIFIED BY 'repl-demo-pw'")
        primary.sql("GRANT REPLICATION SLAVE ON *.* TO 'repl'@'%'")

        replica.sql(f"CHANGE REPLICATION SOURCE TO SOURCE_HOST = '127.0.0.1', SOURCE_PORT = {primary.port}, "
                    "SOURCE_USER = 'repl', SOURCE_PASSWORD = 'repl-demo-pw', SOURCE_AUTO_POSITION = 1, GET_SOURCE_PUBLIC_KEY = 1")
        replica.sql('START REPLICA')

        def caught_up(timeout=60):
            target = primary.sql('SELECT @@GLOBAL.gtid_executed AS g')[0]['g']
            started = time.perf_counter()
            while time.perf_counter() - started < timeout:
                if replica.sql(f"SELECT GTID_SUBSET('{target}', @@GLOBAL.gtid_executed) AS ok")[0]['ok'] == 1:
                    return time.perf_counter() - started
                time.sleep(0.01)
            raise RuntimeError('the replica did not catch up')

        result['initial_copy_seconds'] = round(caught_up(), 2)
        tables = replica.sql("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema = 'public_transport' "
                             "AND table_type = 'BASE TABLE'")[0]['n']
        result['replica_tables'] = tables
        say(f'Replica copied the schema and seed data: {tables} tables ({result["initial_copy_seconds"]} s)')

        # book on the primary: procedure + triggers run there, the replica receives the resulting rows
        conn = primary.connect(database='public_transport')
        cur = conn.cursor()
        for seat in ('9A', '9B', '9C'):
            cur.execute("SET @actor = 'replication-demo', @pay_method = 'UPI'")
            cur.callproc('BookTicket', [3, 2, seat, None, None])
        conn.close()
        lag = caught_up()
        result['lag_ms'] = round(lag * 1000, 1)
        rows = replica.sql("SELECT COUNT(*) AS t, (SELECT COUNT(*) FROM TicketAudit WHERE Actor = 'replication-demo') AS a, "
                           "(SELECT COUNT(*) FROM Payment WHERE Actor = 'replication-demo') AS p "
                           "FROM Ticket WHERE ScheduleID = 2 AND SeatNo IN ('9A', '9B', '9C')", 'public_transport')[0]
        result['replicated'] = {'tickets': rows['t'], 'audit_rows': rows['a'], 'payments': rows['p']}
        say(f'3 tickets booked on the primary appear on the replica after {result["lag_ms"]} ms, with their '
            f'{rows["a"]} audit rows and {rows["p"]} payments (written by triggers on the primary, shipped as rows)')

        status = replica.sql('SHOW REPLICA STATUS')[0]
        result['io_running'], result['sql_running'] = status['Replica_IO_Running'], status['Replica_SQL_Running']
        say(f'Replica threads: IO {status["Replica_IO_Running"]}, SQL {status["Replica_SQL_Running"]}; '
            f'seconds behind source: {status["Seconds_Behind_Source"]}')

        try:
            replica.sql("CALL BookTicket(4, 2, '9D', NULL, NULL)", 'public_transport')
            result['replica_write'] = 'accepted'
        except mysql.connector.Error as err:
            result['replica_write'] = f'refused ({err.errno})'
        say(f'A booking sent to the replica: {result["replica_write"]} - it is read-only, all writes go to the primary')

        report = replica.sql('SELECT RouteName, TicketsSold, Revenue FROM RouteRevenue ORDER BY Revenue DESC LIMIT 3',
                             'public_transport', user='routewise_app', password='routewise_app_pw')
        result['report_rows'] = len(report)
        say('Reports can run on the replica (as the application user, which replicated too):')
        for r in report:
            say(f'  {r["RouteName"]:<14} {r["TicketsSold"]:>4} tickets  {r["Revenue"]:>9}')
    finally:
        replica.stop()
        primary.stop()
        shutil.rmtree(base, ignore_errors=True)
        say('Both servers stopped and removed.')
    if as_json:
        print(json.dumps(result))


if __name__ == '__main__':
    main()
