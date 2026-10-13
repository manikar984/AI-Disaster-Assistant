"""Account security: email confirmation, recovery links and session control.

The SMTP fixture is a real server on loopback, so the links these tests redeem
are the links a user actually receives. Nothing about the flow is simulated:
registration -> email -> link -> confirmation, and forgot -> email -> link ->
new password with every session revoked.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app import app, limits
from backend.db import connect, now
from backend.engine import ingest, qualify_notifications
from smtp_fixture import SmtpFixture
from test_platform import record

HEADERS = {'X-Requested-With': 'DisasterAssistant'}
PASSWORD = 'Strong-test-password-47'
NEW_PASSWORD = 'An-even-stronger-password-51'
PUBLIC_ORIGIN = 'https://alerts.example.org'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'auth.db'))
    monkeypatch.setenv('USGS_ENABLED', 'false')
    monkeypatch.setenv('APP_ORIGIN', PUBLIC_ORIGIN)
    monkeypatch.setenv('PUBLIC_BASE_URL', PUBLIC_ORIGIN)
    for variable in ('SMTP_HOST', 'SMTP_FROM', 'SMTP_USER', 'SMTP_PASSWORD', 'SMTP_SECURITY', 'SSL_CERT_FILE'):
        monkeypatch.delenv(variable, raising=False)
    limits.clear()
    with TestClient(app, headers=HEADERS) as c:
        yield c


def use_relay(monkeypatch, smtp):
    """Point the application at the loopback relay (no TLS needed here)."""
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    monkeypatch.setenv('SMTP_PORT', str(smtp.port))
    monkeypatch.setenv('SMTP_SECURITY', 'plain')
    monkeypatch.setenv('SMTP_FROM', 'alerts@example.org')


def register(client, email='person@example.com'):
    return client.post('/api/auth/register', json={'email': email, 'password': PASSWORD})


def link_token(smtp, kind, index=0):
    """Pull the one-time token out of the message the user received."""
    body = smtp.parsed(index).get_content()
    match = re.search(rf'{re.escape(PUBLIC_ORIGIN)}/\?{kind}=([A-Za-z0-9_\-]+)', body)
    assert match, f'no {kind} link in:\n{body}'
    return match.group(1)


def test_registration_emails_a_link_that_confirms_the_account(client, monkeypatch):
    with SmtpFixture() as smtp:
        use_relay(monkeypatch, smtp)
        response = register(client)
        assert response.status_code == 201
        assert response.json() == {'ok': True, 'email_verified': False, 'verification_email': 'sent'}
        assert client.get('/api/me').json()['email_verified'] == 0
        assert len(smtp.messages) == 1
        assert smtp.messages[0]['to'] == ['person@example.com']
        assert 'Confirm your email address' in smtp.parsed()['Subject']
        token = link_token(smtp, 'verify')

        confirmed = client.post('/api/auth/verify', json={'token': token})
        assert confirmed.status_code == 200
        assert confirmed.json()['email_verified'] is True
        assert client.get('/api/me').json()['email_verified'] == 1
        # Single use: the same link cannot confirm twice or be replayed.
        assert client.post('/api/auth/verify', json={'token': token}).status_code == 400
    with connect() as db:
        actions = {row[0] for row in db.execute('SELECT action FROM audit')}
    assert {'register', 'verify-email'} <= actions


def test_unconfirmed_addresses_get_in_app_alerts_but_no_email(client, monkeypatch):
    with SmtpFixture() as smtp:
        use_relay(monkeypatch, smtp)
        register(client)
        client.post('/api/locations', json={'name': 'Home', 'lat': 10.0, 'lon': 76.0, 'radius': 50, 'min_severity': 'High', 'email_enabled': True})
        with connect() as db:
            ingest(db, record(external_id='evt-unconfirmed'))
            qualify_notifications(db)
            channels = {row[0] for row in db.execute('SELECT channel FROM deliveries')}
        assert channels == {'in-app'}, 'an unconfirmed address must not be emailed'

        # Confirming changes the rule, not the pipeline: the next incident emails.
        client.post('/api/auth/verify', json={'token': link_token(smtp, 'verify')})
        with connect() as db:
            ingest(db, record(external_id='evt-confirmed'))
            qualify_notifications(db)
            confirmed_channels = {row[0] for row in db.execute('SELECT channel FROM deliveries')}
        assert confirmed_channels == {'in-app', 'email'}


def test_registration_survives_a_missing_mail_provider(client):
    """No SMTP is configured: the account exists, but delivery is never claimed."""
    response = register(client)
    assert response.status_code == 201
    assert response.json()['verification_email'] == 'unavailable'
    assert client.get('/api/me').json()['email_verified'] == 0


def test_expired_confirmation_tokens_are_refused(client, monkeypatch):
    with SmtpFixture() as smtp:
        use_relay(monkeypatch, smtp)
        register(client)
        token = link_token(smtp, 'verify')
        with connect() as db:
            db.execute('UPDATE tokens SET expires_at=? WHERE kind=?', ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), 'verify'))
        response = client.post('/api/auth/verify', json={'token': token})
        assert response.status_code == 400
        assert 'invalid or has expired' in response.json()['detail']


def test_resend_issues_a_new_link_and_invalidates_the_old_one(client, monkeypatch):
    with SmtpFixture() as smtp:
        use_relay(monkeypatch, smtp)
        register(client)
        first = link_token(smtp, 'verify')
        assert client.post('/api/auth/resend-verification', json={'email': 'person@example.com'}).status_code == 202
        assert len(smtp.messages) == 2
        second = link_token(smtp, 'verify', index=1)
        assert first != second
        assert client.post('/api/auth/verify', json={'token': first}).status_code == 400
        assert client.post('/api/auth/verify', json={'token': second}).status_code == 200


def test_verification_resend_never_reveals_whether_an_account_exists(client, monkeypatch):
    with SmtpFixture() as smtp:
        use_relay(monkeypatch, smtp)
        unknown = client.post('/api/auth/resend-verification', json={'email': 'nobody@example.com'})
        register(client, email='someone@example.com')
        known = client.post('/api/auth/resend-verification', json={'email': 'someone@example.com'})
        assert unknown.status_code == known.status_code == 202
        assert unknown.json() == known.json()
        # Only the real account got mail: registration plus the resend.
        assert [message['to'] for message in smtp.messages] == [['someone@example.com'], ['someone@example.com']]


def test_forgot_password_is_generic_and_the_recovery_link_works(client, monkeypatch):
    with SmtpFixture() as smtp:
        use_relay(monkeypatch, smtp)
        register(client)
        unknown = client.post('/api/auth/forgot', json={'email': 'nobody@example.com'})
        known = client.post('/api/auth/forgot', json={'email': 'person@example.com'})
        assert unknown.status_code == known.status_code == 202
        assert unknown.json() == known.json()
        assert len(smtp.messages) == 2  # the confirmation email, then the recovery email
        token = link_token(smtp, 'reset', index=1)

        reset = client.post('/api/auth/reset', json={'token': token, 'password': NEW_PASSWORD})
        assert reset.status_code == 200
        # Every device is signed out, including the one that asked for the reset.
        assert client.get('/api/me').status_code == 401
        assert client.post('/api/auth/login', json={'email': 'person@example.com', 'password': PASSWORD}).status_code == 401
        assert client.post('/api/auth/login', json={'email': 'person@example.com', 'password': NEW_PASSWORD}).status_code == 200
        # Single use.
        assert client.post('/api/auth/reset', json={'token': token, 'password': PASSWORD}).status_code == 400
    with connect() as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM audit WHERE action='reset-password'").fetchone()[0] == 1


def test_recovery_requires_a_valid_token_and_a_long_enough_password(client, monkeypatch):
    with SmtpFixture() as smtp:
        use_relay(monkeypatch, smtp)
        register(client)
        short = client.post('/api/auth/reset', json={'token': 'x' * 40, 'password': 'short'})
        assert short.status_code == 422
        unknown = client.post('/api/auth/reset', json={'token': 'x' * 40, 'password': NEW_PASSWORD})
        assert unknown.status_code == 400


def test_old_recovery_links_stop_working_when_a_new_one_is_issued(client, monkeypatch):
    with SmtpFixture() as smtp:
        use_relay(monkeypatch, smtp)
        register(client)
        client.post('/api/auth/forgot', json={'email': 'person@example.com'})
        first = link_token(smtp, 'reset', index=1)
        client.post('/api/auth/forgot', json={'email': 'person@example.com'})
        second = link_token(smtp, 'reset', index=2)
        assert client.post('/api/auth/reset', json={'token': first, 'password': NEW_PASSWORD}).status_code == 400
        assert client.post('/api/auth/reset', json={'token': second, 'password': NEW_PASSWORD}).status_code == 200


def test_sessions_are_listed_without_tokens_and_can_all_be_revoked(client):
    register(client)
    first = client.get('/api/sessions').json()
    assert len(first) == 1 and first[0]['current'] is True
    assert len(first[0]['id']) == 12
    assert 'token' not in first[0]
    assert first[0]['created_at'] and first[0]['expires_at']

    client.post('/api/auth/login', json={'email': 'person@example.com', 'password': PASSWORD})
    assert len(client.get('/api/sessions').json()) == 2

    revoked = client.post('/api/auth/logout-all')
    assert revoked.status_code == 200 and revoked.json()['sessions_revoked'] == 2
    assert client.get('/api/me').status_code == 401
    assert client.get('/api/sessions').status_code == 401


def test_idle_and_absolute_expiry_end_a_session_and_delete_the_row(client):
    register(client)
    with connect() as db:
        db.execute('UPDATE sessions SET last_seen_at=?', ((datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),))
    assert client.get('/api/me').status_code == 401
    with connect() as db:
        assert db.execute('SELECT count(*) FROM sessions').fetchone()[0] == 0

    client.post('/api/auth/login', json={'email': 'person@example.com', 'password': PASSWORD})
    with connect() as db:
        db.execute('UPDATE sessions SET expires_at=?', ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
    assert client.get('/api/me').status_code == 401
    with connect() as db:
        assert db.execute('SELECT count(*) FROM sessions').fetchone()[0] == 0


def test_existing_accounts_are_grandfathered_as_confirmed(tmp_path, monkeypatch):
    """Accounts created before verification existed keep receiving alerts."""
    from backend.db import migrate as run_migrations
    from pathlib import Path as PathType

    database = tmp_path / 'legacy-users.db'
    monkeypatch.setenv('DATABASE_PATH', str(database))
    migrations = PathType(__import__('backend.db', fromlist=['db']).__file__).with_name('migrations')
    with connect() as db:
        db.executescript((migrations / '001_initial.sql').read_text())
        db.execute('CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY)')
        db.execute("INSERT INTO migrations VALUES ('001_initial.sql')")
        db.execute("INSERT INTO users(email,password,role,created_at) VALUES ('old@example.com','x','user',?)", (now(),))
        db.execute("INSERT INTO locations(user_id,name,lat,lon,radius,min_severity,email_enabled) VALUES (1,'Home',10,76,50,'High',1)")
    run_migrations()
    with connect() as db:
        row = db.execute('SELECT email_verified,email_verified_at FROM users').fetchone()
        assert row['email_verified'] == 1 and row['email_verified_at']
        assert db.execute("SELECT count(*) FROM audit WHERE note LIKE '004:%'").fetchone()[0] == 1
        ingest(db, record(external_id='evt-grandfathered'))
        qualify_notifications(db)
        assert 'email' in {r[0] for r in db.execute('SELECT channel FROM deliveries')}
