"""Database access: connection settings, small query helpers and readable error messages."""
import os
import time

import mysql.connector
from flask import has_request_context, session

# The app connects as a least-privilege user created by schema.sql (never as root). Override for your setup.
db_config = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'user': os.environ.get('DB_USER', 'routewise_app'),
    'password': os.environ.get('DB_PASSWORD', 'routewise_app_pw'),
    'database': os.environ.get('DB_NAME', 'public_transport'),
}


def connect():
    """Open a connection and tell MySQL which application user is acting (the audit triggers record it as the Actor)."""
    conn = mysql.connector.connect(**db_config)
    user = session.get('user') if has_request_context() else None
    if user:
        cur = conn.cursor()
        cur.execute('SET @actor = %s', (user['username'],))
        cur.close()
    return conn


def query(sql, args=()):
    """Run a SELECT and return all rows as dicts."""
    conn = connect()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, args)
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()


def execute(sql, args=()):
    """Run an INSERT/UPDATE/DELETE and commit."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        conn.commit()
        cur.close()
    finally:
        conn.close()


def call(proc, args, retries=3):
    """Call a stored procedure and commit. Retries when MySQL reports a deadlock (1213) or lock timeout (1205)."""
    for attempt in range(retries):
        conn = connect()
        try:
            cur = conn.cursor()
            cur.callproc(proc, args)
            conn.commit()
            cur.close()
            return
        except mysql.connector.Error as err:
            if err.errno in (1213, 1205) and attempt < retries - 1:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise
        finally:
            conn.close()


def query_proc(proc, args=()):
    """Call a stored procedure that returns a result set (for example RouteScorecard) and return its rows."""
    conn = connect()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(f"CALL {proc}({', '.join(['%s'] * len(args))})", args)    # proc is a fixed name from the code, never user input
        rows = cur.fetchall()
        while cur.nextset():                                                   # the CALL's own status result
            pass
        cur.close()
        return rows
    finally:
        conn.close()


def friendly_error(err):
    msg = err.msg or str(err)
    if err.errno == 1451:
        return 'Cannot delete: other records still depend on this one.'
    if err.errno == 1062:
        return 'Duplicate value: that email / vehicle number / licence / date already exists.'
    if err.errno == 3819:
        return 'A value violates a database rule (check ratings, capacity, seat label, times, fare...).'
    if err.errno == 1406:
        return 'A value is too long for its field (seat numbers: at most 10 characters).'
    if err.errno in (1264, 1366, 1292, 1265):
        return 'A value is out of range or not valid for its field (for example a fare above 9999.99).'
    if err.errno in (1213, 1205):
        return 'The system is busy with another booking for this departure. Please try again.'
    if err.errno == 1048:
        return 'A required value is missing (for example a fare could not be calculated).'
    if err.errno == 1452:
        return 'Invalid reference: the selected passenger / schedule / route does not exist.'
    return msg
