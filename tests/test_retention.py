"""Retention tests: the policy must delete exactly what it documents.

Every test seeds one expired and one fresh row per class of data, so a purge
that is too eager fails just as loudly as one that never deletes anything.
"""

import sys
from datetime import datetime, timedelta, timezone

import pytest

from backend import cli, retention
from backend.db import connect, get_runtime_state, migrate, now


def ago(**kwargs):
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'retention.db'))
    monkeypatch.setenv('USGS_ENABLED', 'false')
    migrate()
    return tmp_path


def seed():
    """One expired and one fresh row for every class the policy covers."""
    with connect() as db:
        db.execute("INSERT INTO users(id,email,password,role,created_at) VALUES (1,'person@example.com','x','user',?)", (now(),))
        # Raw source payloads: the expired one still carries a payload.
        db.execute(
            "INSERT INTO reports(id,source,external_id,text,location,published_at,collected_at,mode,official,raw) "
            "VALUES (1,'USGS','old','old text','Region A',?,?,'live',1,'{\"payload\":1}')",
            (ago(days=40), ago(days=40)),
        )
        db.execute(
            "INSERT INTO reports(id,source,external_id,text,location,published_at,collected_at,mode,official,raw) "
            "VALUES (2,'USGS','new','new text','Region A',?,?,'live',1,'{}')",
            (ago(days=2), ago(days=2)),
        )
        db.execute("INSERT INTO ingestion_runs(id,source_id,started_at,records) VALUES (1,'usgs',?,1)", (ago(days=31),))
        db.execute("INSERT INTO ingestion_runs(id,source_id,started_at,records) VALUES (2,'usgs',?,1)", (ago(days=1),))
        for row in (
            # id, mode, verification, age in days
            (1, 'live', 'Unverified', 100),    # purged: never confirmed, long idle
            (2, 'live', 'Corroborated', 100),  # kept: corroborated is evidence
            (3, 'live', 'Unverified', 2),      # kept: still fresh
            (4, 'sample', 'Unverified', 400),  # kept: labelled fixture
        ):
            db.execute(
                'INSERT INTO incidents(id,type,location,lat,lon,severity,verification,confidence,mode,summary,'
                'first_detected,last_updated,published_at,version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)',
                (row[0], 'Earthquake', f'Region {row[0]}', 10.0, 76.0, 'Medium', row[2], 'Limited evidence',
                 row[1], f'incident {row[0]}', ago(days=row[3]), ago(days=row[3]), ago(days=row[3])),
            )
        # Delivery history: an expired notification with its delivery, and a fresh
        # pair. They point at different incidents because a notification is unique
        # per (user, incident, version).
        for notification_id, incident_id, age in ((1, 1, 100), (2, 3, 3)):
            db.execute(
                'INSERT INTO notifications(id,user_id,incident_id,version,message,created_at) VALUES (?,1,?,1,?,?)',
                (notification_id, incident_id, f'message {notification_id}', ago(days=age)),
            )
            db.execute(
                "INSERT INTO deliveries(id,notification_id,channel,status,attempts,next_attempt,sent_at) "
                "VALUES (?,?,'email','accepted',1,?,?)",
                (notification_id, notification_id, ago(days=age), ago(days=age)),
            )
        db.execute("INSERT INTO audit(id,action,note,created_at) VALUES (1,'review','old',?)", (ago(days=400),))
        db.execute("INSERT INTO audit(id,action,note,created_at) VALUES (2,'review','fresh',?)", (ago(days=5),))
        db.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES ('expired',1,?)", (ago(days=1),))
        db.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES ('live',1,?)", (ago(days=-20),))
        db.execute(
            "INSERT INTO tokens(user_id,kind,token_hash,created_at,expires_at) VALUES (1,'verify','h1',?,?)",
            (ago(days=2), ago(days=1)),
        )
        db.execute(
            "INSERT INTO tokens(user_id,kind,token_hash,created_at,expires_at) VALUES (1,'verify','h2',?,?)",
            (now(), ago(days=-1)),
        )


def ids(db, table):
    return [row[0] for row in db.execute(f'SELECT id FROM {table} ORDER BY id')]


def test_dry_run_counts_every_expired_class_without_deleting(database):
    seed()
    with connect() as db:
        counted = {key: value for key, value in retention.purge(db, dry_run=True).items() if value}
        assert counted == {
            'raw_payloads': 1,
            'ingestion_runs': 1,
            'notifications': 1,
            'inactive_incidents': 1,
            'expired_sessions': 1,
            'expired_tokens': 1,
            'audit': 1,
        }
        # Nothing moved: the payload is still there and every row still exists.
        assert db.execute('SELECT raw FROM reports WHERE id=1').fetchone()['raw'] == '{"payload":1}'
        assert ids(db, 'reports') == [1, 2]
        assert ids(db, 'incidents') == [1, 2, 3, 4]
        assert ids(db, 'notifications') == [1, 2]
        assert ids(db, 'audit') == [1, 2]


def test_purge_removes_only_the_expired_rows(database):
    seed()
    with connect() as db:
        removed = retention.purge(db)
    assert removed == {
        'raw_payloads': 1,
        'ingestion_runs': 1,
        'deliveries': 1,
        'notifications': 1,
        'inactive_incidents': 1,
        'expired_sessions': 1,
        'expired_tokens': 1,
        'audit': 1,
    }
    with connect() as db:
        # The report row and its evidence link stay; only the raw payload goes.
        assert ids(db, 'reports') == [1, 2]
        assert db.execute('SELECT raw FROM reports WHERE id=1').fetchone()['raw'] == '{}'
        assert db.execute('SELECT raw FROM reports WHERE id=2').fetchone()['raw'] == '{}'
        assert ids(db, 'ingestion_runs') == [2]
        # Corroborated, fresh and sample incidents survive a purge.
        assert ids(db, 'incidents') == [2, 3, 4]
        assert ids(db, 'notifications') == [2]
        assert ids(db, 'deliveries') == [2]
        assert ids(db, 'audit') == [2]
        assert [row[0] for row in db.execute('SELECT token FROM sessions')] == ['live']
        assert [row[0] for row in db.execute('SELECT token_hash FROM tokens')] == ['h2']
        # Account rows are never retention's business.
        assert db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1


def test_purge_is_idempotent_and_reports_nothing_the_second_time(database):
    seed()
    with connect() as db:
        retention.purge(db)
        assert retention.purge(db) == {}


def test_purge_if_due_runs_once_per_window(database):
    seed()
    first = retention.purge_if_due()
    assert first and first['inactive_incidents'] == 1
    # The marker is what stops a five-second worker loop from purging continuously.
    assert retention.purge_if_due() is None
    with connect() as db:
        marker = get_runtime_state(retention.STATE_KEY, db=db)
        assert marker and marker[0]['policy_days'] == retention.POLICY
        db.execute('UPDATE runtime_state SET updated_at=? WHERE key=?', (ago(days=2), retention.STATE_KEY))
    # Once the marker is older than the window it runs again; there is simply
    # nothing left to remove, which is the honest result to report.
    assert retention.purge_if_due() == {}


def test_cli_reports_the_policy_and_deletes_only_without_dry_run(database, monkeypatch, capsys):
    seed()
    monkeypatch.setattr(sys, 'argv', ['cli', 'retention', '--dry-run'])
    with pytest.raises(SystemExit) as first:
        cli.main()
    assert first.value.code == 0
    dry = capsys.readouterr().out
    assert 'Retention window (days)' in dry
    assert 'inactive_incidents' in dry and 'Dry run only' in dry
    with connect() as db:
        assert ids(db, 'incidents') == [1, 2, 3, 4]

    monkeypatch.setattr(sys, 'argv', ['cli', 'retention'])
    with pytest.raises(SystemExit) as second:
        cli.main()
    assert second.value.code == 0
    applied = capsys.readouterr().out
    assert 'Removed:' in applied and 'never purged' in applied
    with connect() as db:
        assert ids(db, 'incidents') == [2, 3, 4]
