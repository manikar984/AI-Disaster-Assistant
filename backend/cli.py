"""Explicit operator tasks. Password input is never echoed or passed in argv."""

import argparse
import getpass
import json
from datetime import datetime, timezone
from pathlib import Path

from . import mailer, retention
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


def _retention(dry_run):
    """Show or apply the retention policy from docs/OPERATIONS.md.

    Prints per-class row counts. Nothing is deleted unless the operator leaves
    --dry-run off, and no class of data outside the policy is touched.
    """
    with connect() as db:
        counts = retention.purge(db, dry_run=dry_run)
    counts = {key: value for key, value in counts.items() if value}
    print('Retention window (days): ' + ', '.join(f'{key}={value}' for key, value in retention.POLICY.items()))
    kept = 'Sample rows, corroborated/official incidents, user accounts and recent records are never purged.'
    if not counts:
        print(('Would remove: nothing. ' if dry_run else 'Removed: nothing. ') + kept)
        return 0
    print('Would remove:' if dry_run else 'Removed:')
    for key, value in sorted(counts.items()):
        print(f'  {key:<20} {value}')
    print(kept)
    print('Purged rows still exist in any backup taken before now, until that backup expires.')
    if dry_run:
        print('Dry run only — nothing was deleted. Re-run without --dry-run to apply.')
    return 0


def _email_check(recipient):
    """Prove the configured mailbox end-to-end, exactly as the worker sends it.

    Prints the configuration summary and the provider's reply code. Never prints
    a credential; the mailer redacts the password from every error it raises.
    """
    try:
        config = mailer.settings()
    except mailer.MailError as exc:
        print(f'Configuration problem: {exc}')
        return 1
    print(f'SMTP configuration: {mailer.describe(config)}')
    if not config['configured']:
        print('Email is disabled: set SMTP_HOST and SMTP_FROM (see .env.example).')
        return 1
    if not recipient or '@' not in recipient:
        print('Name the mailbox that should receive the test: email-check --to you@example.org')
        return 1
    try:
        code = mailer.send_probe(recipient, detail='Requested by the operator email-check command.')
    except mailer.MailError as exc:
        print(f'Delivery failed: {exc}')
        print('The message was not sent. Nothing in this output contains the SMTP password.')
        return 1
    print(f'Provider accepted the message (final SMTP reply {code}).')
    print(f'Confirm that it arrived at {recipient} (check the spam folder), then record it in '
          'docs/VERIFICATION.md. Until that message arrives, the email channel is configured '
          'but not proven.')
    return 0


def main():
    parser = argparse.ArgumentParser(description='Disaster Assistant operator tasks')
    parser.add_argument('command', choices=['sample', 'admin', 'reset-password', 'sources', 'poll', 'email-check', 'retention'])
    parser.add_argument('--email')
    parser.add_argument('--source', default='', help='poll only this configured source id')
    parser.add_argument('--to', default='', help='email-check: mailbox that should receive the test message')
    parser.add_argument('--dry-run', action='store_true', help='retention: report what would be removed without removing it')
    args = parser.parse_args()
    migrate()

    if args.command == 'sources':
        _show_sources()
        return
    if args.command == 'poll':
        raise SystemExit(_poll(args.source))
    if args.command == 'email-check':
        raise SystemExit(_email_check(args.to))
    if args.command == 'retention':
        raise SystemExit(_retention(args.dry_run))

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
