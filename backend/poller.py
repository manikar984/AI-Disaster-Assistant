"""Generic, idempotent polling of one configured feed.

One code path handles every adapter: fetch → normalize → ingest, with error
isolation per record, honest counters, and no fallback data of any kind. A
failed feed leaves the previous records untouched and only changes source
health, so a user can always see that a source is failing rather than stale.
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from .db import connect, now
from .engine import ingest
from .sources import adapter_for, feed_targets
from .sources.base import http_client

log = logging.getLogger('poller')

#: Per-record failures that are counted and skipped, never fatal.
RECORD_ERRORS = (ValueError, KeyError, TypeError, IndexError, OverflowError, AttributeError)


def upsert_source(db, target):
    db.execute(
        'INSERT INTO sources(id,name,enabled,status,kind,description,attribution,scope,interval_seconds) '
        "VALUES (?,?,1,'pending',?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, enabled=1, "
        'kind=excluded.kind, description=excluded.description, attribution=excluded.attribution, '
        'scope=excluded.scope, interval_seconds=excluded.interval_seconds',
        (
            target.id,
            target.name,
            target.kind,
            target.description,
            target.attribution,
            target.scope,
            target.interval,
        ),
    )


def _finish_run(db, run_id, counters, error, duration_ms):
    db.execute(
        'UPDATE ingestion_runs SET finished_at=?,records=?,skipped=?,errors=?,error=?,duration_ms=? WHERE id=?',
        (now(), counters['inserted'], counters['skipped'], counters['rejected'], error, duration_ms, run_id),
    )


def _mark_source(db, target, status, last_error, counters, success_stamp):
    next_poll = (datetime.now(timezone.utc) + timedelta(seconds=target.interval)).isoformat()
    db.execute(
        'UPDATE sources SET status=?, last_attempt=?, last_success=COALESCE(?,last_success), last_error=?, '
        'records_total=records_total+?, records_rejected=records_rejected+?, next_poll=?, '
        'interval_seconds=? WHERE id=?',
        (
            status,
            now(),
            success_stamp,
            last_error,
            counters['inserted'],
            counters['rejected'],
            next_poll,
            target.interval,
            target.id,
        ),
    )


def poll_target(target, client):
    """Poll one feed. Never raises; returns a result dict for logs and callers."""
    started = time.monotonic()
    counters = {'inserted': 0, 'skipped': 0, 'rejected': 0}
    with connect() as db:
        upsert_source(db, target)
        run_id = db.execute(
            'INSERT INTO ingestion_runs(source_id,started_at) VALUES (?,?)', (target.id, now())
        ).lastrowid
    try:
        module = adapter_for(target)
        raw_records = module.fetch_raw(client, target.url)
        if not isinstance(raw_records, list):
            raise ValueError('adapter did not return a record list')
        with connect() as db:
            for raw in raw_records:
                try:
                    db.execute('SAVEPOINT record')
                    record = module.normalize(raw, target)
                    incident_id = ingest(db, record)
                    db.execute('RELEASE record')
                    counters['inserted' if incident_id is not None else 'skipped'] += 1
                except RECORD_ERRORS:
                    db.execute('ROLLBACK TO record')
                    db.execute('RELEASE record')
                    counters['rejected'] += 1
                except sqlite3.Error:
                    db.execute('ROLLBACK TO record')
                    db.execute('RELEASE record')
                    counters['rejected'] += 1
                    log.warning(json.dumps({'event': 'record_rejected', 'source': target.id, 'error': 'database'}))
        duration_ms = int((time.monotonic() - started) * 1000)
        status = 'degraded' if counters['rejected'] else 'healthy'
        detail = f"{counters['rejected']} malformed records" if counters['rejected'] else None
        with connect() as db:
            _finish_run(db, run_id, counters, detail, duration_ms)
            _mark_source(db, target, status, detail, counters, now())
        log.info(
            json.dumps(
                {
                    'event': 'poll_complete',
                    'source': target.id,
                    'fetched': len(raw_records),
                    'inserted': counters['inserted'],
                    'skipped': counters['skipped'],
                    'rejected': counters['rejected'],
                    'duration_ms': duration_ms,
                    'status': status,
                }
            )
        )
        return dict(ok=True, status=status, source=target.id, fetched=len(raw_records),
                    duration_ms=duration_ms, **counters)
    except Exception as exc:  # provider failure, malformed feed, timeout
        duration_ms = int((time.monotonic() - started) * 1000)
        error = type(exc).__name__
        with connect() as db:
            _finish_run(db, run_id, counters, error, duration_ms)
            _mark_source(db, target, 'error', error, counters, None)
        # Provider response bodies and credentials are never logged.
        log.warning(json.dumps({'event': 'ingestion_failed', 'source': target.id, 'error': error}))
        return dict(ok=False, status='error', source=target.id, error=error, duration_ms=duration_ms, **counters)


def poll_all(client=None, targets=None):
    """Poll every configured feed once (used by the CLI and by tests)."""
    configured = feed_targets() if targets is None else targets
    results = []
    if client is not None:
        for target in configured:
            results.append(poll_target(target, client))
        return results
    with http_client() as owned:
        for target in configured:
            results.append(poll_target(target, owned))
    return results


def source_health(row, current=None):
    """Effective status for one source row, accounting for feed staleness."""
    if not row['enabled']:
        return 'disabled'
    status = row['status'] or 'pending'
    if status in ('healthy', 'degraded') and row['last_success']:
        age = (current or datetime.now(timezone.utc)) - datetime.fromisoformat(row['last_success'])
        if age > timedelta(seconds=max(600, 3 * (row['interval_seconds'] or 300))):
            return 'stale'
    return status


SOURCE_COLUMNS = (
    'id,name,kind,description,attribution,scope,status,enabled,interval_seconds,last_attempt,'
    'last_success,last_error,records_total,records_rejected,next_poll'
)


def source_rows(current=None):
    """Every configured source with its effective health, for the API and CLI."""
    with connect() as db:
        rows = db.execute(f'SELECT {SOURCE_COLUMNS} FROM sources ORDER BY id').fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['status'] = source_health(row, current)
        item['enabled'] = bool(row['enabled'])
        result.append(item)
    return result


def source_state_summary(current=None):
    """Health summary used by the readiness endpoint and the dashboard."""
    current = current or datetime.now(timezone.utc)
    summary = {'total': 0, 'enabled': 0, 'healthy': 0, 'degraded': 0, 'error': 0, 'stale': 0, 'pending': 0, 'disabled': 0}
    for row in source_rows(current):
        summary['total'] += 1
        if row['enabled']:
            summary['enabled'] += 1
        status = row['status']
        summary[status] = summary.get(status, 0) + 1
    return summary
