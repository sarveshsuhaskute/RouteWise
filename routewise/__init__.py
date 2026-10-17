"""RouteWise: a Flask interface on top of a MySQL database that enforces the business rules itself.

Modules: db (connections and helpers), web (shared request helpers, filters, error pages), auth (sign-in, roles, CSRF,
users), search (FULLTEXT search terms) and views/* (one module per page).
"""
import logging
import os
import secrets

from flask import Flask


def _load_env_file():
    """Read KEY=VALUE lines from the git-ignored .env file in the project folder, without overriding the real environment."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"\''))


_load_env_file()
app = Flask(__name__)
# Set SECRET_KEY to keep logins valid across restarts; otherwise a random key is generated per run.
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    REPORT_CACHE_SECONDS=int(os.environ.get('REPORT_CACHE_SECONDS', '30')),   # 0 switches the report cache off
    LOGIN_MAX_FAILURES=5,                                                       # failed sign-ins allowed ...
    LOGIN_LOCK_SECONDS=600,                                                     # ... within this window
    DEMO_USERS=os.environ.get('DEMO_USERS', '1') != '0',                        # create the demo accounts on first start
    METRICS_TOKEN=os.environ.get('METRICS_TOKEN') or None,                      # /metrics stays off (404) without it
)
# one line per request on stderr (waitress and docker compose logs collect it), see monitoring.py
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')

if os.environ.get('BEHIND_PROXY') == '1':          # behind Caddy (docker-compose.yml): trust its X-Forwarded-* headers
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
if os.environ.get('COOKIE_SECURE') == '1':           # session cookie only over HTTPS
    app.config['SESSION_COOKIE_SECURE'] = True

from . import auth, monitoring, web  # noqa: E402,F401  (filters, error pages, sign-in, users, access log and metrics)
from .views import bookings, dashboard, feedback, fleet, network, passengers, reports, tickets  # noqa: E402,F401
