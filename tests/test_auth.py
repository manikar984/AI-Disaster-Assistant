"""Account security: email confirmation, recovery links and session control.

The SMTP fixture is a real server on loopback, so the links these tests redeem
are the links a user actually receives. Nothing about the flow is simulated:
registration -> email -> link -> confirmation, and forgot -> email -> link ->
new password with every session revoked.
"""

import re
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app import app, limits
from backend.db import connect, now
from backend import mailer
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


def sign_in(client, email='person@example.com', password=PASSWORD):
    """The second call a browser makes: register no longer starts a session."""
    return client.post('/api/auth/login', json={'email': email, 'password': password})


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
        assert response.status_code == 202
        assert response.json()['ok'] is True and 'message' in response.json()
        # Registering no longer signs the caller in: the session starts on login.
        assert client.get('/api/me').status_code == 401
        assert sign_in(client).status_code == 200
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
        sign_in(client)
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
    """No SMTP is configured: the account exists, but delivery is never claimed.

    The response is the same as with a working provider, by design. What an
    operator needs to know is on `/api/ready` (`checks.email`), not in a public
    registration answer.
    """
    response = register(client)
    assert response.status_code == 202
    assert sign_in(client).status_code == 200
    assert client.get('/api/me').json()['email_verified'] == 0
    assert client.get('/api/ready').json()['checks']['email'] == 'not configured'


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
        # Only the real account got mail: the confirmation from registration, then
        # the replacement link; the unknown address got none.
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
    sign_in(client)
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
    sign_in(client)
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


# --- enumeration regression tests -------------------------------------------
# The defect: `POST /api/auth/register` answered 409 for an address that already
# had an account, so anyone could test whether an address was registered, and it
# also created a session for an address the caller had not proven they own.


def probe(client, email, password=PASSWORD):
    """What an attacker can see: status, body, headers and how long it took."""
    started = time.perf_counter()
    response = client.post('/api/auth/register', json={'email': email, 'password': password})
    return response.status_code, response.json(), dict(response.headers), time.perf_counter() - started


def test_registration_never_reveals_whether_an_address_is_known(client):
    """The 409 leak, pinned: a new and a taken address are indistinguishable."""
    fresh_status, fresh_body, fresh_headers, _ = probe(client, 'fresh@example.com')
    with connect() as db:
        before = tuple(db.execute('SELECT password,email_verified FROM users WHERE email=?', ('fresh@example.com',)).fetchone())

    repeated_status, repeated_body, repeated_headers, _ = probe(client, 'fresh@example.com')

    assert fresh_status == repeated_status == 202
    assert fresh_body == repeated_body, 'the answer must not depend on whether the address exists'
    assert 'email_verified' not in fresh_body and 'verification_email' not in fresh_body
    assert set(fresh_headers) == set(repeated_headers)
    with connect() as db:
        after = tuple(db.execute('SELECT password,email_verified FROM users WHERE email=?', ('fresh@example.com',)).fetchone())
        assert db.execute('SELECT count(*) FROM users').fetchone()[0] == 1
    # A duplicate sign-up must not modify the account it collided with...
    assert before == after
    # ...and must not hand the caller a session for an address they have not proven.
    assert client.get('/api/me').status_code == 401
    assert client.get('/api/sessions').status_code == 401


def test_duplicate_registration_sends_the_notice_to_the_address_owner(client, monkeypatch):
    """The information goes to the mailbox owner instead of to the requester."""
    with SmtpFixture() as smtp:
        use_relay(monkeypatch, smtp)
        register(client)
        assert len(smtp.messages) == 1 and 'Confirm your email address' in smtp.parsed()['Subject']

        response = client.post('/api/auth/register', json={'email': 'person@example.com', 'password': PASSWORD})
        assert response.status_code == 202
        assert len(smtp.messages) == 2
        notice = smtp.parsed(1)
        assert notice['To'] == 'person@example.com'
        assert 'existing account' in notice['Subject']
        body = notice.get_content()
        assert 'already exists' in body and 'Nothing changed' in body
        assert 'Forgot your password?' in body
        # A notice, not a replayable link: no extra token was issued for it.
        with connect() as db:
            assert db.execute("SELECT count(*) FROM tokens WHERE kind='verify'").fetchone()[0] == 1

    with connect() as db:
        assert 'register-duplicate' in {row[0] for row in db.execute('SELECT action FROM audit')}


def test_repeated_duplicate_signups_cannot_be_used_to_bomb_a_mailbox(client, monkeypatch):
    """The identical answer must not become a way to mail a third party repeatedly."""
    with SmtpFixture() as smtp:
        use_relay(monkeypatch, smtp)
        register(client)
        for _ in range(5):
            assert client.post('/api/auth/register', json={'email': 'person@example.com', 'password': PASSWORD}).status_code == 202
        # One confirmation at registration, one notice for the first duplicate.
        assert len(smtp.messages) == 2
        with connect() as db:
            assert db.execute("SELECT count(*) FROM audit WHERE action='register-duplicate'").fetchone()[0] == 5
            assert db.execute("SELECT count(*) FROM audit WHERE note LIKE '%notice already sent%'").fetchone()[0] == 4


def test_recovery_endpoints_tell_the_caller_nothing_without_a_provider(client):
    """Without SMTP the caller still gets the generic answer, never 'no such account'."""
    unknown = client.post('/api/auth/forgot', json={'email': 'nobody@example.com'})
    register(client)
    known = client.post('/api/auth/forgot', json={'email': 'person@example.com'})
    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert client.post('/api/auth/resend-verification', json={'email': 'nobody@example.com'}).json() == known.json()


def test_the_answer_completes_before_the_message_is_sent(monkeypatch, tmp_path):
    """Ordering, not just wording: no address can be probed through latency.

    The mail is queued as a background task, so the response body is complete
    before the SMTP conversation starts. A synchronous send would make a
    registered address measurably slower than an unregistered one — the leak this
    test exists to prevent. `mailer.send` is wrapped (and still called for real),
    so the ordering below is observed, not assumed.
    """
    events = []

    async def recording_app(scope, receive, send):
        async def recording_send(message):
            if message['type'] == 'http.response.body' and not message.get('more_body'):
                events.append('response-complete')
            await send(message)

        await app(scope, receive, recording_send)

    real_send = mailer.send

    def recording_send_mail(*args, **kwargs):
        events.append('smtp-started')
        return real_send(*args, **kwargs)

    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'order.db'))
    monkeypatch.setenv('USGS_ENABLED', 'false')
    monkeypatch.setenv('APP_ORIGIN', PUBLIC_ORIGIN)
    monkeypatch.setenv('PUBLIC_BASE_URL', PUBLIC_ORIGIN)
    limits.clear()
    with SmtpFixture() as smtp:
        use_relay(monkeypatch, smtp)
        monkeypatch.setattr(mailer, 'send', recording_send_mail)
        with TestClient(recording_app, headers=HEADERS) as client:
            assert client.post('/api/auth/register', json={'email': 'order-probe@example.com', 'password': PASSWORD}).status_code == 202
    assert events == ['response-complete', 'smtp-started'], events
    assert len(smtp.messages) == 1


def test_registration_does_the_same_expensive_work_either_way(client, monkeypatch):
    """scrypt runs on both branches, so the answer cannot be timed.

    A branch that skips the password hash answers ~50 ms faster than one that pays
    for it, which is by itself a test for "is this address registered?". This was
    measured against a real server before the branch was equalised.
    """
    import backend.app as app_module

    real_hash = app_module.password_hash
    calls = []

    def counting_hash(password):
        calls.append(password)
        return real_hash(password)

    monkeypatch.setattr(app_module, 'password_hash', counting_hash)
    assert probe(client, 'timing-probe@example.com')[0] == 202      # nothing to hash for
    assert probe(client, 'timing-probe@example.com')[0] == 202      # duplicate sign-up
    assert len(calls) == 2, 'both branches must pay the same password-hashing cost'
