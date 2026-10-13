"""Retention enforcement: the documented policy, actually applied.

`docs/OPERATIONS.md` states the retention policy. Until now it was only prose:
nothing deleted anything. This module is the part that deletes, on a schedule the
worker drives and an operator can preview.

| Data | Kept | Why this window |
|---|---|---|
| Raw source payloads (`reports.raw`) | 30 days | the largest privacy surface; the parsed record and its evidence link stay |
| Ingestion runs | 30 days | operational history, not evidence |
| Delivery and notification history | 90 days | long enough to investigate a delivery complaint |
| Inactive, never-corroborated incidents | 90 days | a rumour that was never confirmed and never updated |
| Session rows and single-use tokens | until expiry | already invalid once expired |
| Security and admin audit entries | 365 days | the accountability window |

Never purged, whatever the clock says: sample rows (operator-loaded fixtures that
are labelled as samples everywhere), incidents that are `Corroborated`,
`Officially Confirmed`, `Resolved` or `Disputed`, incidents updated recently,
account rows, and audit entries inside the window. Deleting an incident cascades
through `incident_reports`, `timeline` and `notifications` (foreign keys are
enabled in `db.connect()`), which is the intended behaviour: an incident that
aged out takes its evidence links with it.

Run it as an operator with `python -m backend.cli retention [--dry-run]`. The
worker calls `purge_if_due()` once a day. Purged rows still exist in any backup
taken before the purge, until that backup expires — the deployed privacy policy
must say so.
"""

from datetime import datetime, timedelta, timezone

from .db import connect, get_runtime_state, now, set_runtime_state

#: Days each class of data is kept. Change here and the docs must change with it.
POLICY = {
    'raw_payloads': 30,
    'ingestion_runs': 30,
    'delivery_history': 90,
    'inactive_incidents': 90,
    'audit': 365,
}

#: Runtime-state key that stops the worker from purging on every cycle.
STATE_KEY = 'last_purge'

#: Verifications that mean "nobody confirmed this": only these age out.
UNCONFIRMED = ('Unverified', 'Developing')


def _cutoff(days, now=None):
    moment = now or datetime.now(timezone.utc)
    return (moment - timedelta(days=days)).isoformat()


def plan(db, moment=None):
    """Count what the policy would remove right now, changing nothing."""
    stamp_now = (moment or datetime.now(timezone.utc)).isoformat()
    return {
        'raw_payloads': db.execute(
            "SELECT COUNT(*) FROM reports WHERE raw<>'{}' AND collected_at<?",
            (_cutoff(POLICY['raw_payloads'], moment),),
        ).fetchone()[0],
        'ingestion_runs': db.execute(
            'SELECT COUNT(*) FROM ingestion_runs WHERE started_at<?',
            (_cutoff(POLICY['ingestion_runs'], moment),),
        ).fetchone()[0],
        'notifications': db.execute(
            'SELECT COUNT(*) FROM notifications WHERE created_at<?',
            (_cutoff(POLICY['delivery_history'], moment),),
        ).fetchone()[0],
        'inactive_incidents': db.execute(
            'SELECT COUNT(*) FROM incidents WHERE mode<>? AND verification IN (?,?) AND last_updated<?',
            ('sample', *UNCONFIRMED, _cutoff(POLICY['inactive_incidents'], moment)),
        ).fetchone()[0],
        'expired_sessions': db.execute('SELECT COUNT(*) FROM sessions WHERE expires_at<?', (stamp_now,)).fetchone()[0],
        'expired_tokens': db.execute('SELECT COUNT(*) FROM tokens WHERE expires_at<?', (stamp_now,)).fetchone()[0],
        'audit': db.execute('SELECT COUNT(*) FROM audit WHERE created_at<?', (_cutoff(POLICY['audit'], moment),)).fetchone()[0],
    }


def purge(db, moment=None, dry_run=False):
    """Apply the policy. Returns per-table row counts (actual deletes, or the plan)."""
    counted = plan(db, moment)
    if dry_run:
        return counted
    deleted = {}
    deleted['raw_payloads'] = db.execute(
        "UPDATE reports SET raw='{}' WHERE raw<>'{}' AND collected_at<?",
        (_cutoff(POLICY['raw_payloads'], moment),),
    ).rowcount
    deleted['ingestion_runs'] = db.execute(
        'DELETE FROM ingestion_runs WHERE started_at<?', (_cutoff(POLICY['ingestion_runs'], moment),)
    ).rowcount
    # Deliveries first: the cascade would take them anyway, but explicit counts
    # make the operation auditable in the CLI output.
    deleted['deliveries'] = db.execute(
        'DELETE FROM deliveries WHERE notification_id IN (SELECT id FROM notifications WHERE created_at<?)',
        (_cutoff(POLICY['delivery_history'], moment),),
    ).rowcount
    deleted['notifications'] = db.execute(
        'DELETE FROM notifications WHERE created_at<?', (_cutoff(POLICY['delivery_history'], moment),)
    ).rowcount
    deleted['inactive_incidents'] = db.execute(
        'DELETE FROM incidents WHERE mode<>? AND verification IN (?,?) AND last_updated<?',
        ('sample', *UNCONFIRMED, _cutoff(POLICY['inactive_incidents'], moment)),
    ).rowcount
    stamp_now = (moment or datetime.now(timezone.utc)).isoformat()
    deleted['expired_sessions'] = db.execute('DELETE FROM sessions WHERE expires_at<?', (stamp_now,)).rowcount
    deleted['expired_tokens'] = db.execute('DELETE FROM tokens WHERE expires_at<?', (stamp_now,)).rowcount
    deleted['audit'] = db.execute('DELETE FROM audit WHERE created_at<?', (_cutoff(POLICY['audit'], moment),)).rowcount
    # Counted rows that were already gone (a cascade, or a race with the API)
    # are dropped from the report instead of being reported as deletions.
    return {key: value for key, value in deleted.items() if value}


def purge_if_due(hours=24, moment=None):
    """Purge at most once per window. Returns the counts, or None when not due.

    The worker calls this every cycle; the marker lives in `runtime_state`, so a
    restarted worker neither skips a due purge nor loops on a failing one.
    """
    with connect() as db:
        state = get_runtime_state(STATE_KEY, db=db)
        if state:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(state[1])).total_seconds()
            except ValueError:
                age = hours * 3600
            if age < hours * 3600:
                return None
        counts = purge(db, moment)
        set_runtime_state(db, STATE_KEY, {'purged': counts, 'policy_days': POLICY, 'at': now()})
        return counts
