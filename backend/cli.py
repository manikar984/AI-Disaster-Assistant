"""Explicit operator tasks. Password input is never echoed or passed in argv."""

import argparse
import getpass
import json
from datetime import datetime, timezone
from pathlib import Path

from .db import connect, migrate, now
from .engine import ingest
from .poller import poll_all, source_rows
from .security import password_hash
from .sources import feed_targets


def _show_sources():
    rows = source_rows()
    if not rows:
        print('No sources configured.')
        return
    for row in rows:
        print(
            f"{row['id']:<24} {row['kind']:<9} {row['status']:<9} enabled={int(row['enabled'])} "
            f"interval={row['interval_seconds']}s records={row['records_total']} "
            f"rejected={row['records_rejected']} last_success={row['last_success'] or '-'} "
            f"last_error={row['last_error'] or '-'}"
        )
    print('\nEnable a feed with its documented variable, for example USGS_ENABLED=true or')
    print('NEWS_FEEDS="Publisher|https://example.org/feed.xml" (see .env.example and docs/SOURCES.md).')


def _poll(only):
    targets = [target for target in feed_targets() if not only or target.id == only]
    if not targets:
        print('No matching enabled source. Enable one first (see `python -m backend.cli sources`).')
        return 1
    failed = False
    for result in poll_all(targets=targets):
        print(
            f"{result['source']:<24} status={result['status']:<9} fetched={result.get('fetched', 0)} "
            f"stored={result['inserted']} skipped={result['skipped']} rejected={result['rejected']} "
            f"duration={result['duration_ms']}ms"
        )
        failed = failed or not result['ok']
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description='Disaster Assistant operator tasks')
    parser.add_argument('command', choices=['sample', 'admin', 'reset-password', 'sources', 'poll'])
    parser.add_argument('--email')
    parser.add_argument('--source', default='', help='poll only this configured source id')
    args = parser.parse_args()
    migrate()

    if args.command == 'sources':
        _show_sources()
        return
    if args.command == 'poll':
        raise SystemExit(_poll(args.source))

    with connect() as db:
        if args.command in ('admin', 'reset-password'):
            if not args.email:
                parser.error('--email required')
            password = getpass.getpass('New password (12+ characters): ')
            if len(password) < 12 or len(password) > 128:
                parser.error('Password must contain 12–128 characters')
            email = args.email.lower()
            if args.command == 'admin':
                db.execute(
                    "INSERT INTO users(email,password,role,created_at) VALUES (?,?,'admin',?) "
                    "ON CONFLICT(email) DO UPDATE SET role='admin',password=excluded.password",
                    (email, password_hash(password), now()),
                )
            else:
                if not db.execute('UPDATE users SET password=? WHERE email=?', (password_hash(password), email)).rowcount:
                    parser.error('Unknown account')
            db.execute('DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE email=?)', (email,))
            db.execute('INSERT INTO audit(action,note,created_at) VALUES (?,?,?)', (args.command, 'Local operator credential operation', now()))
        else:
            # Original dataset reused, never silently seeded on startup.
            # City/region coordinates are approximate and labelled as such.
            coords = {'Kerala': (10.8505, 76.2711), 'Japan': (36.2048, 138.2529), 'New York': (40.7128, -74.006)}
            records = json.loads((Path(__file__).resolve().parent.parent / 'disaster_data.json').read_text())
            stored = 0
            for index, record in enumerate(records):
                lat, lon = coords.get(record['location'], (None, None))
                incident = ingest(
                    db,
                    dict(
                        record,
                        source='Legacy dataset',
                        source_name='Legacy dataset (historical sample)',
                        source_kind='community',
                        external_id=str(index),
                        lat=lat,
                        lon=lon,
                        geo_precision='approximate-region' if lat is not None else 'unknown',
                        mode='sample',
                        published_at=datetime(2025, 10, 1, tzinfo=timezone.utc).isoformat(),
                    ),
                )
                stored += 1 if incident is not None else 0
            print(
                f'Historical sample records loaded ({stored} incidents). City/region coordinates are '
                'approximate; the publication date is a fixture placeholder. Samples never notify and '
                'are never treated as live evidence.'
            )


if __name__ == '__main__':
    main()
