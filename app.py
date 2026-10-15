"""RouteWise entry point.

    python -m waitress --listen=127.0.0.1:5000 app:app     # production-style server
    python app.py                                           # development server (FLASK_DEBUG=1 for auto-reload)
"""
import os

from routewise import app

if __name__ == '__main__':
    # Debug mode is off by default (it exposes an interactive debugger).
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1')
