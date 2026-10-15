"""RouteWise: a Flask interface on top of a MySQL database that enforces the business rules itself.

Modules: db (connections and helpers), web (shared request helpers, filters, error pages), auth (sign-in, roles, CSRF,
users), search (FULLTEXT search terms) and views/* (one module per page).
"""
import os
import secrets

from flask import Flask


def _load_env_file():
    """Read KEY=VALUE lines from the git-ignored .env file in the project folder, without overriding the real environment."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    if not os.path.exists(path):
        return
    for line in open(path):
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
)

from . import web, auth  # noqa: E402,F401  (registers filters, error pages, sign-in and user management)
from .views import dashboard, bookings, passengers, fleet, network, feedback, reports  # noqa: E402,F401
