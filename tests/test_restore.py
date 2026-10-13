"""Backup and restore drill: a snapshot must come back as a *usable* database.

The unit test in `tests/test_deploy.py` proves the backup script produces a
consistent, pruned snapshot. That is only half the promise — a backup nobody has
ever restored is a hypothesis. This module performs the drill the runbook asks
for: build a database with real relationships in it (accounts, sessions,
locations, sources, ingestion runs, reports, incidents, notification and delivery
rows, audit entries, tokens, runtime state), back it up with `deploy/backup.sh`,
restore that snapshot with `deploy/restore.sh`, and then use the restored file as
if it were the deployment's:

* the API runs against it (sign-in with the password from the snapshot, incident
  and notification reads, a write),
* the ingestion engine runs against it and a new qualifying incident produces a
  new notification,
* relationships and row counts match the source, `PRAGMA integrity_check` is
  clean and `PRAGMA foreign_key_check` finds nothing broken,
* the source database is untouched — same bytes, same mtime — because a drill
  that damages the live database is worse than no drill.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app import app, limits
from backend.db import connect, migrate, now, set_runtime_state
from backend.engine import ingest, qualify_notifications
from backend.security import password_hash

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(REPO, 'deploy')
HEADERS = {'X-Requested-With': 'DisasterAssistant'}
PASSWORD = 'Restore-drill-password-47'


def fingerprint(path):
    """Everything a drill must not change: length, bytes and modification time."""
    data = open(path, 'rb').read()
    stat = os.stat(path)
    return {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'mtime': stat.st_mtime_ns, 'mode': stat.st_mode & 0o777}


def seed_live_database(path):
    """A small but complete deployment state, built through the real code paths."""
    os.environ['DATABASE_PATH'] = str(path)
    migrate()
    with connect() as db:
        user_id = db.execute(
            "INSERT INTO users(email,password,role,created_at,email_verified,email_verified_at) VALUES (?,?,?,?,1,?)",
            ('restore-drill@example.com', password_hash(PASSWORD), 'user', now(), now()),
        ).lastrowid
        db.execute(
            "INSERT INTO sessions(token,user_id,expires_at,created_at,last_seen_at) VALUES ('drill-session-hash',?,?,?,?)",
            (user_id, now(), now(), now()),
        )
        db.execute(
            'INSERT INTO locations(user_id,name,lat,lon,radius,min_severity,email_enabled) VALUES (?,?,?,?,?,?,1)',
            (user_id, 'Drill home', 10.0, 76.0, 50, 'High'),
        )
        # An unrelated account with its own location: the drill must not merge or
        # lose per-user rows.
        other = db.execute(
            "INSERT INTO users(email,password,role,created_at,email_verified) VALUES (?,?,?,?,1)",
            ('other@example.com', password_hash('Other-password-47'), 'user', now()),
        ).lastrowid
        db.execute(
            'INSERT INTO locations(user_id,name,lat,lon,radius,min_severity,email_enabled) VALUES (?,?,?,?,?,?,0)',
            (other, 'Other home', 35.0, 139.0, 100, 'Critical'),
        )
        # A live incident with an evidence report link and a timeline entry.
        incident_id = ingest(db, {
            'source': 'USGS', 'external_id': 'drill-event-1', 'text': 'Magnitude 6.2 earthquake',
            'location': 'Drill region', 'lat': 10.0, 'lon': 76.0, 'published_at': now(),
            'mode': 'live', 'official': True, 'magnitude': 6.2,
            'url': 'https://earthquake.usgs.gov/earthquakes/eventpage/drill-event-1',
        })
        db.execute(
            "INSERT INTO reports(source,external_id,url,text,location,lat,lon,published_at,collected_at,mode,official,raw) "
            "VALUES ('news','drill-news-1','https://news.example.org/drill','Flooding reported nearby','Drill region',"
            "10.1,76.1,?,?,'live',0,'{\"payload\":true}')",
            (now(), now()),
        )
        news_report = db.execute("SELECT id FROM reports WHERE external_id='drill-news-1'").fetchone()['id']
        db.execute('INSERT INTO incident_reports(incident_id,report_id) VALUES (?,?)', (incident_id, news_report))
        db.execute('INSERT INTO timeline(incident_id,created_at,status,note) VALUES (?,?,?,?)',
                   (incident_id, now(), 'detected', 'Drill: incident detected from a structured source'))
        qualify_notifications(db)
        db.execute("INSERT INTO audit(actor_id,action,note,created_at) VALUES (?,?,?,?)",
                   (user_id, 'register', 'Drill: account created', now()))
        set_runtime_state(db, 'worker_heartbeat', {'pid': 4242, 'status': 'alive'})
    return {'user_id': user_id, 'incident_id': incident_id, 'report_id': news_report, 'other_id': other}


def run_script(script, args, **environment):
    env = dict(os.environ, PYTHON=sys.executable, **environment)
    return subprocess.run(['bash', os.path.join(DEPLOY, script), *args],
                          capture_output=True, text=True, env=env)


@pytest.fixture
def drill(tmp_path):
    """A live database, a snapshot of it and a restored copy — all in tmp_path."""
    live = tmp_path / 'live' / 'assistant.db'
    live.parent.mkdir()
    ids = seed_live_database(live)
    before = fingerprint(live)
    backups = tmp_path / 'backups'
    backup = run_script('backup.sh', [], DATABASE_PATH=str(live), BACKUP_DIR=str(backups),
                        BACKUP_RETENTION_DAYS='30')
    assert backup.returncode == 0, backup.stderr
    snapshot = sorted(backups.glob('assistant-*.db'))[-1]
    restored = tmp_path / 'restored' / 'assistant.db'
    restore = run_script('restore.sh', [str(snapshot), '--to', str(restored)], DATABASE_PATH=str(live))
    assert restore.returncode == 0, restore.stderr
    return {'live': live, 'before': before, 'snapshot': snapshot, 'restored': restored,
            'ids': ids, 'backup_output': backup.stdout, 'restore_output': restore.stdout}


def table_counts(path, tables):
    with sqlite3.connect(path) as db:
        return {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in tables}


TRACKED = ('users', 'sessions', 'locations', 'sources', 'reports', 'incidents',
           'incident_reports', 'timeline', 'notifications', 'deliveries', 'audit', 'tokens')


def test_the_restored_database_is_intact_and_matches_the_source(drill):
    assert 'Snapshot verified' in drill['restore_output']
    assert 'row counts match the snapshot' in drill['restore_output']
    with sqlite3.connect(drill['restored']) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert table_counts(drill['restored'], TRACKED) == table_counts(drill['live'], TRACKED)
    # The seeded content is really there, not just the tables.
    assert table_counts(drill['restored'], ('users', 'incidents', 'reports', 'notifications'))['notifications'] >= 1
    assert open(drill['restored'], 'rb').read(16) == b'SQLite format 3\x00'


def test_relationships_survive_the_round_trip(drill):
    ids = drill['ids']
    with sqlite3.connect(drill['restored']) as db:
        db.row_factory = sqlite3.Row
        # Every reference still resolves, in both directions.
        assert db.execute('SELECT count(*) FROM locations WHERE user_id NOT IN (SELECT id FROM users)').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM sessions WHERE user_id NOT IN (SELECT id FROM users)').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM incident_reports WHERE incident_id NOT IN (SELECT id FROM incidents) OR report_id NOT IN (SELECT id FROM reports)').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM timeline WHERE incident_id NOT IN (SELECT id FROM incidents)').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM notifications WHERE incident_id NOT IN (SELECT id FROM incidents) OR user_id NOT IN (SELECT id FROM users)').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM deliveries WHERE notification_id NOT IN (SELECT id FROM notifications)').fetchone()[0] == 0
        linked = db.execute('SELECT incident_id, report_id FROM incident_reports').fetchall()
        assert (ids['incident_id'], ids['report_id']) in {(row[0], row[1]) for row in linked}
        # Per-user rows stayed apart.
        emails = {row['email'] for row in db.execute('SELECT email FROM users')}
        assert emails == {'restore-drill@example.com', 'other@example.com'}
        heart = db.execute("SELECT value FROM runtime_state WHERE key='worker_heartbeat'").fetchone()
        assert json.loads(heart['value'])['pid'] == 4242
        # Retention markers and account state are part of the state, not of a
        # rebuild: the restored copy is a real database, migrations included.
        assert db.execute('SELECT count(*) FROM migrations').fetchone()[0] >= 4


def test_the_restored_database_serves_the_api(drill, monkeypatch):
    """Sign-in, reads and a write, all against the restored file."""
    monkeypatch.setenv('DATABASE_PATH', str(drill['restored']))
    monkeypatch.setenv('USGS_ENABLED', 'false')
    limits.clear()
    with TestClient(app, headers=HEADERS) as client:
        # No re-migration was needed, and the API started on the restored file.
        login = client.post('/api/auth/login', json={'email': 'restore-drill@example.com', 'password': PASSWORD})
        assert login.status_code == 200, 'the restored password hash no longer verifies'
        incidents = client.get('/api/incidents').json()
        assert len(incidents) == 1 and incidents[0]['id'] == drill['ids']['incident_id']
        assert incidents[0]['verification'] in ('Corroborated', 'Officially Confirmed')
        detail = client.get(f"/api/incidents/{drill['ids']['incident_id']}").json()
        assert detail['timeline'] and detail['evidence']
        assert client.get('/api/notifications').json(), 'the queued alert did not survive'
        assert client.get('/api/locations').json()[0]['name'] == 'Drill home'
        added = client.post('/api/locations', json={
            'name': 'Added after restore', 'lat': 12.0, 'lon': 77.0, 'radius': 25,
            'min_severity': 'Medium', 'email_enabled': False,
        })
        assert added.status_code in (200, 201), 'the restored database rejected a write'
        assert client.get('/api/locations').json()[1]['name'] == 'Added after restore'


def test_the_restored_database_accepts_new_ingestion(drill, monkeypatch):
    """A restore that cannot take new work is not a restore."""
    monkeypatch.setenv('DATABASE_PATH', str(drill['restored']))
    monkeypatch.setenv('USGS_ENABLED', 'false')
    with connect() as db:
        before = db.execute('SELECT count(*) FROM notifications').fetchone()[0]
        ingest(db, {
            'source': 'USGS', 'external_id': 'drill-event-2', 'text': 'Magnitude 6.8 earthquake',
            'location': 'Drill region', 'lat': 10.0, 'lon': 76.0, 'published_at': now(),
            'mode': 'live', 'official': True, 'magnitude': 6.8,
            'url': 'https://earthquake.usgs.gov/earthquakes/eventpage/drill-event-2',
        })
        qualify_notifications(db)
        after = db.execute('SELECT count(*) FROM notifications').fetchone()[0]
        assert after > before, 'the restored database produced no notification for a new incident'
        assert db.execute("SELECT count(*) FROM notifications WHERE incident_id=(SELECT id FROM incidents WHERE summary LIKE '%6.8%')").fetchone()[0] >= 1
    migrate()  # idempotent on a restored database: nothing pending, nothing lost


def test_the_drill_never_touches_the_source_database(drill):
    assert fingerprint(drill['live']) == drill['before'], 'the drill modified the database it backed up'
    assert drill['restored'] != drill['live']
    assert not os.path.exists(str(drill['live']) + '-journal'), 'the drill left a rollback journal behind'


def test_restore_refuses_to_guess_a_destination(drill):
    result = run_script('restore.sh', [str(drill['snapshot'])])
    assert result.returncode == 2
    assert 'Refusing to guess a destination' in result.stderr


def test_restore_refuses_an_existing_target_without_force(drill, tmp_path):
    target = tmp_path / 'occupied.db'
    shutil.copy(drill['snapshot'], target)
    before = fingerprint(target)
    result = run_script('restore.sh', [str(drill['snapshot']), '--to', str(target)])
    assert result.returncode == 1
    assert 'pass --force to replace it' in result.stderr
    assert fingerprint(target) == before, 'a refused restore still modified the target'


def test_restore_refuses_the_live_database_without_force(tmp_path):
    live = tmp_path / 'live.db'
    live.write_bytes(b'not-really-a-database')
    snapshot = tmp_path / 'snapshot.db'
    shutil.copy(live, snapshot)
    result = run_script('restore.sh', [str(snapshot), '--to', str(live)], DATABASE_PATH=str(live))
    assert result.returncode == 1
    assert 'live database' in result.stderr
    assert live.read_bytes() == b'not-really-a-database'


def test_a_corrupt_snapshot_is_refused_before_anything_is_written(tmp_path):
    corrupt = tmp_path / 'corrupt.db'
    corrupt.write_bytes(b'SQLite format 3\x00' + b'\x00' * 4096)  # header, no pages
    target = tmp_path / 'never-created.db'
    result = run_script('restore.sh', [str(corrupt), '--to', str(target)])
    assert result.returncode != 0
    assert 'not a usable SQLite database' in result.stderr
    assert not target.exists(), 'a failed restore left a file where the API could open it'
    assert not list(tmp_path.glob('.restore-*')), 'the temporary file was not cleaned up'


def test_a_snapshot_with_broken_relationships_is_refused(tmp_path):
    """Verification is not only an integrity check: references must resolve."""
    broken = tmp_path / 'broken.db'
    with sqlite3.connect(broken) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('CREATE TABLE notifications(id INTEGER PRIMARY KEY)')
        db.execute('CREATE TABLE deliveries(id INTEGER PRIMARY KEY, notification_id INTEGER REFERENCES notifications(id))')
        for table in ('users', 'sessions', 'locations', 'sources', 'ingestion_runs', 'reports',
                      'incidents', 'incident_reports', 'timeline', 'audit', 'tokens', 'runtime_state'):
            db.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY)')
        db.execute('CREATE TABLE migrations(name TEXT PRIMARY KEY)')
        # A delivery pointing at a notification that was never there: the kind of
        # damage a partial restore or a bad merge would leave behind.
        db.execute('INSERT INTO deliveries(id,notification_id) VALUES (1,99)')
    target = tmp_path / 'never-created.db'
    result = run_script('restore.sh', [str(broken), '--to', str(target)])
    assert result.returncode != 0
    assert 'broken foreign-key reference' in result.stderr
    assert not target.exists(), 'a failed restore left a file where the API could open it'
