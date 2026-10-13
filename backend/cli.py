"""Explicit operator tasks. Password input is never echoed or passed in argv."""
import argparse
import getpass
import json
from datetime import datetime, timezone
from pathlib import Path
from .db import connect, migrate, now
from .engine import ingest
from .security import password_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['sample', 'admin', 'reset-password'])
    parser.add_argument('--email')
    args = parser.parse_args()
    migrate()
    with connect() as db:
        if args.command in ('admin', 'reset-password'):
            if not args.email:
                parser.error('--email required')
            password = getpass.getpass('New password (12+ characters): ')
            if len(password) < 12 or len(password) > 128:
                parser.error('Password must contain 12–128 characters')
            email = args.email.lower()
            if args.command == 'admin':
                db.execute("INSERT INTO users(email,password,role,created_at) VALUES (?,?,'admin',?) ON CONFLICT(email) DO UPDATE SET role='admin',password=excluded.password", (email, password_hash(password), now()))
            else:
                if not db.execute('UPDATE users SET password=? WHERE email=?', (password_hash(password), email)).rowcount:
                    parser.error('Unknown account')
            db.execute('DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE email=?)', (email,))
            db.execute('INSERT INTO audit(action,note,created_at) VALUES (?,?,?)', (args.command, 'Local operator credential operation', now()))
        else:
            # Original dataset reused, never silently seeded on startup.
            coords = {'Kerala': (10.8505, 76.2711), 'Japan': (36.2048, 138.2529), 'New York': (40.7128, -74.006)}
            records = json.loads((Path(__file__).resolve().parent.parent/'disaster_data.json').read_text())
            for index, record in enumerate(records):
                lat, lon = coords.get(record['location'], (None, None))
                ingest(db, dict(record, source='Legacy dataset', external_id=str(index), lat=lat, lon=lon, mode='sample', published_at=datetime(2025, 10, 1, tzinfo=timezone.utc).isoformat()))
            print('Historical sample records loaded. City/region coordinates are approximate; publication date is a fixture placeholder. No notifications will be sent.')


if __name__ == '__main__':
    main()
