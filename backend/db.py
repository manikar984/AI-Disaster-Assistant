import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone


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
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY)')
        for path in sorted(Path(__file__).with_name('migrations').glob('*.sql')):
            if not db.execute('SELECT 1 FROM migrations WHERE name=?', (path.name,)).fetchone():
                # Execute migration and marker atomically.
                db.executescript('BEGIN IMMEDIATE;\n' + path.read_text() + "\nINSERT INTO migrations VALUES ('" + path.name + "');\nCOMMIT;")
        enabled = os.getenv('USGS_ENABLED', 'false').lower() == 'true'
        db.execute("INSERT INTO sources(id,name,enabled,status) VALUES ('usgs','USGS Earthquake Hazards',?,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled", (int(enabled), 'pending' if enabled else 'disabled'))
