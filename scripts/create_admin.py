"""Create (or reset) an administrator account, for installations that run without the demo accounts (DEMO_USERS=0).

    .venv/bin/python scripts/create_admin.py USERNAME ["Full name"]     # asks for the password twice

The password is stored as a salted hash, like every other account. Connection settings: DB_HOST, DB_USER, DB_PASSWORD,
DB_NAME (the same as the application, so it runs as the application's own database user).
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from werkzeug.security import generate_password_hash  # noqa: E402

from routewise.auth import DEMO_PASSWORDS  # noqa: E402
from routewise.db import execute  # noqa: E402


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    username = sys.argv[1].strip().lower()
    full_name = sys.argv[2] if len(sys.argv) > 2 else username
    password = os.environ.get('NEW_ADMIN_PASSWORD') or getpass.getpass('New password: ')
    if not os.environ.get('NEW_ADMIN_PASSWORD') and password != getpass.getpass('Repeat it: '):
        sys.exit('The passwords do not match.')
    if len(password) < 8 or password in DEMO_PASSWORDS.values():
        sys.exit('Choose a password of at least 8 characters that is not one of the demo passwords.')
    execute("""INSERT INTO AppUser (Username, FullName, PasswordHash, Role) VALUES (%s, %s, %s, 'admin')
               ON DUPLICATE KEY UPDATE FullName = VALUES(FullName), PasswordHash = VALUES(PasswordHash), Role = 'admin'""",
            (username, full_name, generate_password_hash(password)))
    print(f'Administrator {username} is ready. Sign in at http://127.0.0.1:5000')


if __name__ == '__main__':
    main()
