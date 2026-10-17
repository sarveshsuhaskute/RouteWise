"""Operations: one access-log line per request, and /metrics in the Prometheus text format.

/metrics is for a monitoring server (Prometheus, Grafana Agent ...), not for people. It is switched off (404) until
METRICS_TOKEN is set; the scraper then sends "Authorization: Bearer <token>". It reports request counts and latencies
per page and status, plus a few business numbers read from the database (tickets sold today, seats held, the
schema version). The access log records the method, path (never the query string, which can hold search terms),
status, duration and the signed-in user.
"""
import hmac
import logging
import threading
import time

from flask import Response, abort, g, request, session

from . import app
from .db import query

log = logging.getLogger('routewise.access')
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)        # seconds
_lock = threading.Lock()
_requests = {}            # (endpoint, status) -> count
_latency = {}             # endpoint -> [bucket counts..., +Inf count, sum of seconds]


@app.before_request
def _start_clock():
    g.started = time.perf_counter()


def _record(status):
    seconds = time.perf_counter() - g.get('started', time.perf_counter())
    endpoint = request.endpoint or 'unknown'                             # a fixed set of names, never a raw URL
    with _lock:
        _requests[(endpoint, status)] = _requests.get((endpoint, status), 0) + 1
        h = _latency.setdefault(endpoint, [0] * (len(BUCKETS) + 2))
        for i, bound in enumerate(BUCKETS):
            if seconds <= bound:
                h[i] += 1
        h[-2] += 1
        h[-1] += seconds
    user = (session.get('user') or {}).get('username', '-')
    log.info('%s %s %s %.1fms user=%s', request.method, request.path, status, seconds * 1000, user)


@app.after_request
def _log_response(resp):
    _record(resp.status_code)
    return resp


@app.teardown_request
def _log_crash(exc):
    if exc is not None:                                                  # an unhandled error: after_request did not run
        _record(500)


def _metric_lines():
    out = ['# HELP routewise_http_requests_total HTTP requests by page (Flask endpoint) and status.',
           '# TYPE routewise_http_requests_total counter']
    with _lock:
        requests = sorted(_requests.items())
        latency = sorted((k, list(v)) for k, v in _latency.items())
    out += [f'routewise_http_requests_total{{endpoint="{e}",status="{s}"}} {n}' for (e, s), n in requests]
    out += ['# HELP routewise_http_request_seconds Time to answer a request, by page.',
            '# TYPE routewise_http_request_seconds histogram']
    for endpoint, h in latency:
        out += [f'routewise_http_request_seconds_bucket{{endpoint="{endpoint}",le="{b}"}} {h[i]}' for i, b in enumerate(BUCKETS)]
        out += [f'routewise_http_request_seconds_bucket{{endpoint="{endpoint}",le="+Inf"}} {h[-2]}',
                f'routewise_http_request_seconds_count{{endpoint="{endpoint}"}} {h[-2]}',
                f'routewise_http_request_seconds_sum{{endpoint="{endpoint}"}} {h[-1]:.6f}']
    row = query("""SELECT (SELECT COUNT(*) FROM Ticket WHERE BookingDate >= CURDATE()) AS sold_today,
                          (SELECT COUNT(*) FROM SeatHold WHERE ExpiresAt > NOW()) AS seats_held,
                          (SELECT MAX(Version) FROM SchemaVersion) AS schema_version""")[0]
    out += ['# HELP routewise_tickets_sold_today Tickets booked since midnight.', '# TYPE routewise_tickets_sold_today gauge',
            f'routewise_tickets_sold_today {row["sold_today"]}',
            '# HELP routewise_seats_held Seats currently held by a clerk.', '# TYPE routewise_seats_held gauge',
            f'routewise_seats_held {row["seats_held"]}',
            '# HELP routewise_schema_version Version recorded in SchemaVersion.', '# TYPE routewise_schema_version gauge',
            f'routewise_schema_version {row["schema_version"]}']
    return out


@app.route('/metrics')
def metrics():
    token = app.config.get('METRICS_TOKEN')
    if not token:
        abort(404)
    if not hmac.compare_digest(request.headers.get('Authorization', ''), f'Bearer {token}'):
        abort(403)
    return Response('\n'.join(_metric_lines()) + '\n', mimetype='text/plain; version=0.0.4')
