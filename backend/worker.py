"""Background worker: scheduled ingestion plus notification dispatch.

Run exactly one worker per database. Every enabled feed from the source
registry is polled on its own schedule with exponential backoff after failures;
a failing feed never causes sample data to be loaded, and a malformed record
never aborts a poll.

The worker publishes a heartbeat (``runtime_state``) so the API and operators can
tell "no new incidents" apart from "the worker died".
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from .db import CYCLE_KEY, HEARTBEAT_KEY, connect, migrate, now, set_runtime_state
from . import mailer
from .engine import qualify_notifications
from .poller import poll_target
from .sources import feed_targets, usgs
from .sources.base import http_client

log = logging.getLogger('worker')

MAX_CYCLE_SLEEP = 5


# --- Backwards-compatible helpers (used by tests and the CLI) ---------------


def normalize_usgs(feature):
    """Normalize one USGS feature using the adapter's own source identity."""
    return usgs.normalize(feature, usgs.target())


def poll_usgs():
    """Poll USGS once; ``True`` when the feed was collected successfully."""
    with http_client() as client:
        return poll_target(usgs.target(), client)['ok']


# ---------------------------------------------------------------------------


def deliver_email():
    with connect() as db:
        items = db.execute(
            "SELECT d.*,n.message,n.user_id,n.version AS alert_version,i.version AS current_version,"
            'i.verification,i.published_at,u.email FROM deliveries d '
            'JOIN notifications n ON n.id=d.notification_id JOIN users u ON u.id=n.user_id '
            "JOIN incidents i ON i.id=n.incident_id WHERE d.channel='email' AND d.status IN ('pending','retry') "
            'AND d.next_attempt<=? ORDER BY CASE i.severity WHEN \'Critical\' THEN 0 ELSE 1 END,d.id LIMIT 50',
            (now(),),
        ).fetchall()
    for item in items:
        with connect() as db:
            consent = db.execute('SELECT 1 FROM locations WHERE user_id=? AND email_enabled=1', (item['user_id'],)).fetchone()
            stale = (
                datetime.now(timezone.utc) - datetime.fromisoformat(item['published_at']) > timedelta(hours=6)
                and item['verification'] not in ('Resolved', 'Disputed')
            )
            if not consent or stale or item['alert_version'] != item['current_version']:
                db.execute("UPDATE deliveries SET status='cancelled' WHERE id=?", (item['id'],))
                continue
        try:
            mailer.send_alert(item['email'], item['message'], item['notification_id'])
            status, error, sent = 'accepted', None, now()
        except Exception as exc:
            # Failure text is redacted by the mailer and truncated here: it is
            # shown to operators and must never carry a credential.
            status = 'failed' if item['attempts'] >= 4 else 'retry'
            error, sent = f'{type(exc).__name__}: {exc}'[:300], None
        with connect() as db:
            db.execute(
                'UPDATE deliveries SET status=?,attempts=attempts+1,last_error=?,sent_at=?,next_attempt=? WHERE id=?',
                (status, error, sent, (datetime.now(timezone.utc) + timedelta(seconds=min(3600, 60 * 2 ** item['attempts']))).isoformat(), item['id']),
            )


def poll_cycle():
    """Poll every due feed once. Returns a small summary for logging/tests."""
    results = []
    with http_client() as client:
        for target in feed_targets():
            results.append(poll_target(target, client))
    return results


def main():
    logging.basicConfig(level=logging.INFO)
    migrate()
    schedule = {}
    with connect() as db:
        set_runtime_state(db, 'worker_started', {'pid': os.getpid(), 'version': '0.2.0'})
    while True:
        try:
            with connect() as db:
                set_runtime_state(db, HEARTBEAT_KEY, {'pid': os.getpid(), 'status': 'alive'})
            due = []
            stamp = time.monotonic()
            for target in feed_targets():
                state = schedule.setdefault(target.id, {'next': 0.0, 'failures': 0})
                if stamp >= state['next']:
                    due.append(target)
            if due:
                with http_client() as client:
                    for target in due:
                        result = poll_target(target, client)
                        state = schedule[target.id]
                        state['failures'] = 0 if result['ok'] else state['failures'] + 1
                        # Exponential backoff on failure, capped at one hour.
                        delay = min(3600, target.interval * 2 ** min(state['failures'], 5))
                        state['next'] = time.monotonic() + delay
                        with connect() as db:
                            set_runtime_state(
                                db,
                                CYCLE_KEY,
                                {
                                    'source': target.id,
                                    'status': result['status'],
                                    'inserted': result['inserted'],
                                    'skipped': result['skipped'],
                                    'rejected': result['rejected'],
                                    'fetched': result.get('fetched', 0),
                                    'duration_ms': result['duration_ms'],
                                },
                            )
            with connect() as db:
                qualify_notifications(db)
            deliver_email()
        except Exception as exc:
            log.error(json.dumps({'event': 'worker_cycle_failed', 'error': type(exc).__name__}))
        time.sleep(MAX_CYCLE_SLEEP)


if __name__ == '__main__':
    main()
