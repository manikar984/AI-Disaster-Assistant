"""Persistence entry points: connection policy, migrations and runtime state.

SQLite in WAL mode is a deliberate single-node choice (one API process plus one
worker on one host with a persistent volume). It is not horizontally scalable:
see docs/ARCHITECTURE.md for the PostgreSQL/PostGIS migration boundary.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


#: Runtime-state keys shared by the worker (writer) and the API (reader).
HEARTBEAT_KEY = 'worker_heartbeat'
CYCLE_KEY = 'worker_last_cycle'


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    path = os.getenv('DATABASE_PATH', './data/assistant.db')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def migrate():
    """Apply pending migrations and refresh the configured source registry.

    Configuration (environment) is the source of truth for which feeds are
    enabled right now; existing health history is preserved.
    """
    from .sources import source_rows  # imported here to avoid an import cycle

    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY)')
        for path in sorted(Path(__file__).with_name('migrations').glob('*.sql')):
            if not db.execute('SELECT 1 FROM migrations WHERE name=?', (path.name,)).fetchone():
                # Execute migration and marker atomically.
                db.executescript(
                    'BEGIN IMMEDIATE;\n' + path.read_text() + "\nINSERT INTO migrations VALUES ('"
                    + path.name + "');\nCOMMIT;"
                )
        configured = source_rows()
        ids = [row['id'] for row in configured]
        if ids:
            placeholders = ','.join('?' for _ in ids)
            db.execute(
                f"UPDATE sources SET enabled=0, status='disabled' WHERE id NOT IN ({placeholders})",
                ids,
            )
        else:
            db.execute("UPDATE sources SET enabled=0, status='disabled'")
        for row in configured:
            db.execute(
                "INSERT INTO sources(id,name,enabled,status,kind,description,attribution,scope,interval_seconds) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                'enabled=excluded.enabled, kind=excluded.kind, description=excluded.description, '
                'attribution=excluded.attribution, scope=excluded.scope, interval_seconds=excluded.interval_seconds, '
                "status=CASE WHEN excluded.enabled=1 AND sources.status='disabled' THEN 'pending' "
                "WHEN excluded.enabled=0 THEN 'disabled' ELSE sources.status END",
                (
                    row['id'],
                    row['name'],
                    row['enabled'],
                    'pending' if row['enabled'] else 'disabled',
                    row['kind'],
                    row['description'],
                    row['attribution'],
                    row['scope'],
                    row['interval_seconds'],
                ),
            )


def set_runtime_state(db, key, value):
    """Record small operational facts (worker heartbeat, last cycle result)."""
    db.execute(
        'INSERT INTO runtime_state(key,value,updated_at) VALUES (?,?,?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
        (key, json.dumps(value), now()),
    )


def get_runtime_state(key, db=None):
    """(value, updated_at) for a runtime key, or None when it was never set."""
    query = 'SELECT value,updated_at FROM runtime_state WHERE key=?'
    try:
        if db is not None:
            row = db.execute(query, (key,)).fetchone()
        else:
            with connect() as connection:
                row = connection.execute(query, (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        return json.loads(row['value']), row['updated_at']
    except (ValueError, TypeError):
        return row['value'], row['updated_at']
