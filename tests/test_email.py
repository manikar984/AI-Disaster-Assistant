"""Email channel tests over a real SMTP conversation on a loopback socket.

Nothing in the delivery path is mocked: the real worker selects the real queued
delivery, the real mailer builds a real MIME message and the real `smtplib`
client talks to a real SMTP server (the fixture below) over TCP. The fixture
stands in for the mail provider until the operator supplies credentials, so it
covers everything except the provider's own delivery to a human inbox — that
last step is what `python -m backend.cli email-check` proves on the deployment.
"""

import os
import socket
import sys
from datetime import datetime, timedelta, timezone

import pytest

from backend import cli, mailer
from backend.db import connect, migrate, now
from backend.engine import ingest, qualify_notifications
from backend.worker import deliver_email


from smtp_fixture import SmtpFixture


@pytest.fixture
def mailbox(tmp_path, monkeypatch):
    """A migrated database with no SMTP configuration at all."""
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'mail.db'))
    monkeypatch.setenv('USGS_ENABLED', 'false')
    for variable in ('SMTP_HOST', 'SMTP_FROM', 'SMTP_USER', 'SMTP_PASSWORD', 'SMTP_SECURITY', 'SSL_CERT_FILE'):
        monkeypatch.delenv(variable, raising=False)
    migrate()
    return tmp_path


def queue_alert(email='person@example.com', magnitude=6.1):
    """Create a subscriber, a live official incident and the queued email delivery."""
    with connect() as db:
        db.execute(
            "INSERT INTO users(email,password,role,created_at,email_verified,email_verified_at) VALUES (?,'x','user',?,1,?)",
            (email, now(), now()),
        )
        user_id = db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()['id']
        db.execute(
            'INSERT INTO locations(user_id,name,lat,lon,radius,min_severity,email_enabled) VALUES (?,?,?,?,?,?,1)',
            (user_id, 'Test home', 10.0, 76.0, 50, 'High'),
        )
        ingest(db, {
            'source': 'USGS', 'external_id': 'evt-email-1', 'text': 'Magnitude 6.1 earthquake',
            'location': 'Test region', 'lat': 10.0, 'lon': 76.0, 'published_at': now(),
            'mode': 'live', 'official': True, 'magnitude': magnitude,
            'url': 'https://earthquake.usgs.gov/earthquakes/eventpage/evt-email-1',
        })
        qualify_notifications(db)


def delivery_row():
    with connect() as db:
        return dict(db.execute("SELECT status,attempts,last_error,next_attempt,sent_at FROM deliveries WHERE channel='email'").fetchone())


def free_port():
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_local_relay_delivers_the_real_alert_message(mailbox, monkeypatch):
    with SmtpFixture() as smtp:
        monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
        monkeypatch.setenv('SMTP_PORT', str(smtp.port))
        monkeypatch.setenv('SMTP_SECURITY', 'plain')
        monkeypatch.setenv('SMTP_FROM', 'alerts@example.org')
        queue_alert()
        deliver_email()
        assert len(smtp.messages) == 1
        assert smtp.messages[0]['from'] == 'alerts@example.org'
        assert smtp.messages[0]['to'] == ['person@example.com']
        message = smtp.parsed()
        assert 'incident update (not a government warning)' in message['Subject']
        assert message['To'] == 'person@example.com'
        body = message.get_content()
        assert 'Test region' in body and 'Officially Confirmed' in body
        assert 'official emergency systems' in body
        # 'plain' means plain: the client must not try to upgrade or authenticate.
        assert not [line for line in smtp.commands() if line.startswith(('STARTTLS', 'AUTH'))]
    row = delivery_row()
    assert row['status'] == 'accepted' and row['attempts'] == 1
    assert row['sent_at'] and row['last_error'] is None


def test_starttls_with_authentication_is_the_default_path(mailbox, monkeypatch):
    with SmtpFixture(starttls=True, require_auth=('relay-user', 'relay-secret-9')) as smtp:
        monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
        monkeypatch.setenv('SMTP_PORT', str(smtp.port))
        monkeypatch.setenv('SMTP_FROM', 'alerts@example.org')
        monkeypatch.setenv('SMTP_USER', 'relay-user')
        monkeypatch.setenv('SMTP_PASSWORD', 'relay-secret-9')
        # Trust exactly the fixture's self-signed certificate; production uses the
        # system trust store with no way to disable verification.
        monkeypatch.setenv('SSL_CERT_FILE', smtp.ca_file)
        queue_alert()
        deliver_email()
        assert smtp.commands()[0].startswith('EHLO')
        assert any(line.startswith('STARTTLS') for line in smtp.commands())
        assert any(line.startswith('AUTH') for line in smtp.commands())
        assert len(smtp.messages) == 1
        assert smtp.parsed()['X-Disaster-Assistant-Channel'] == 'email'
    assert delivery_row()['status'] == 'accepted'


def test_rejected_credentials_fail_without_leaking_the_password(mailbox, monkeypatch):
    with SmtpFixture(starttls=True, require_auth=('relay-user', 'correct-horse-battery')) as smtp:
        monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
        monkeypatch.setenv('SMTP_PORT', str(smtp.port))
        monkeypatch.setenv('SMTP_FROM', 'alerts@example.org')
        monkeypatch.setenv('SMTP_USER', 'relay-user')
        monkeypatch.setenv('SMTP_PASSWORD', 'wrong-password-value')
        monkeypatch.setenv('SSL_CERT_FILE', smtp.ca_file)
        queue_alert()
        deliver_email()
        assert smtp.messages == []
    row = delivery_row()
    assert row['status'] == 'retry' and row['attempts'] == 1
    assert 'SMTPAuthenticationError' in row['last_error']
    assert 'wrong-password-value' not in row['last_error']
    assert 'relay-secret' not in row['last_error'] and 'correct-horse' not in row['last_error']


def test_provider_outage_retries_with_backoff_then_gives_up(mailbox, monkeypatch):
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    monkeypatch.setenv('SMTP_PORT', str(free_port()))  # nothing is listening here
    monkeypatch.setenv('SMTP_FROM', 'alerts@example.org')
    queue_alert()
    observed, delays = [], []
    for _ in range(5):
        deliver_email()
        with connect() as db:
            row = dict(db.execute("SELECT status,attempts,next_attempt FROM deliveries WHERE channel='email'").fetchone())
            delays.append((datetime.fromisoformat(row['next_attempt']) - datetime.now(timezone.utc)).total_seconds())
            # Simulate the retry window elapsing so the next run picks the row up again.
            db.execute("UPDATE deliveries SET next_attempt=? WHERE channel='email'", (now(),))
        observed.append((row['status'], row['attempts']))
    assert observed == [('retry', 1), ('retry', 2), ('retry', 3), ('retry', 4), ('failed', 5)]
    # Backoff doubles and is capped at an hour, so a provider outage never turns into a hot loop.
    assert 55 <= delays[0] <= 65, delays
    assert 115 <= delays[1] <= 125, delays
    assert 235 <= delays[2] <= 245, delays
    assert 470 <= delays[3] <= 490, delays
    assert delivery_row()['last_error'].startswith('MailError')


def test_email_check_proves_delivery_and_prints_no_secret(mailbox, monkeypatch, capsys):
    with SmtpFixture(starttls=True, require_auth=('relay-user', 'relay-secret-9')) as smtp:
        monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
        monkeypatch.setenv('SMTP_PORT', str(smtp.port))
        monkeypatch.setenv('SMTP_FROM', 'alerts@example.org')
        monkeypatch.setenv('SMTP_USER', 'relay-user')
        monkeypatch.setenv('SMTP_PASSWORD', 'relay-secret-9')
        monkeypatch.setenv('SSL_CERT_FILE', smtp.ca_file)
        monkeypatch.setattr(sys, 'argv', ['cli', 'email-check', '--to', 'operator@example.org'])
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
        assert exit_info.value.code == 0
        output = capsys.readouterr().out
        assert 'SMTP configuration: host=127.0.0.1' in output
        assert 'Provider accepted the message' in output
        assert 'relay-secret-9' not in output and 'relay-user' not in output
    assert len(smtp.messages) == 1
    assert smtp.messages[0]['to'] == ['operator@example.org']
    assert 'SMTP verification message (not a government warning)' in smtp.parsed()['Subject']
    assert 'Nothing has happened' in smtp.parsed().get_content()


def test_email_check_refuses_without_configuration(mailbox, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['cli', 'email-check', '--to', 'operator@example.org'])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert 'Email is disabled' in capsys.readouterr().out


def test_invalid_security_mode_is_rejected_before_connecting(mailbox, monkeypatch):
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    monkeypatch.setenv('SMTP_FROM', 'alerts@example.org')
    monkeypatch.setenv('SMTP_SECURITY', 'opportunistic')
    with pytest.raises(mailer.MailError) as error:
        mailer.settings()
    assert 'SMTP_SECURITY' in str(error.value)


def test_sending_without_a_host_reports_configuration_not_a_traceback(mailbox):
    with pytest.raises(mailer.MailError) as error:
        mailer.send(mailer.build_message('person@example.com', 'subject', 'body'))
    assert 'SMTP_HOST' in str(error.value)


def test_implicit_tls_on_the_submission_port_is_supported(mailbox, monkeypatch):
    """Provider port 465: TLS wraps the socket before the server greeting."""
    with SmtpFixture(implicit_tls=True, require_auth=('relay-user', 'relay-secret-9')) as smtp:
        monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
        monkeypatch.setenv('SMTP_PORT', str(smtp.port))
        monkeypatch.setenv('SMTP_SECURITY', 'ssl')
        monkeypatch.setenv('SMTP_FROM', 'alerts@example.org')
        monkeypatch.setenv('SMTP_USER', 'relay-user')
        monkeypatch.setenv('SMTP_PASSWORD', 'relay-secret-9')
        monkeypatch.setenv('SSL_CERT_FILE', smtp.ca_file)
        queue_alert()
        deliver_email()
        assert smtp.commands()[0].startswith('EHLO')
        assert not any(line.startswith('STARTTLS') for line in smtp.commands())
        assert any(line.startswith('AUTH') for line in smtp.commands())
        assert smtp.parsed()['X-Disaster-Assistant-Channel'] == 'email'
        assert smtp.parsed()['To'] == 'person@example.com'
    row = delivery_row()
    assert row['status'] == 'accepted' and row['attempts'] == 1 and row['sent_at']


def test_implicit_tls_still_verifies_the_provider_certificate(mailbox, monkeypatch):
    """No way to turn verification off: an untrusted certificate must fail closed."""
    with SmtpFixture(implicit_tls=True) as smtp:
        monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
        monkeypatch.setenv('SMTP_PORT', str(smtp.port))
        monkeypatch.setenv('SMTP_SECURITY', 'ssl')
        monkeypatch.setenv('SMTP_FROM', 'alerts@example.org')
        queue_alert()
        deliver_email()
        assert smtp.messages == []
    row = delivery_row()
    assert row['status'] == 'retry' and row['attempts'] == 1
    assert 'certificate' in row['last_error'].lower()


def test_stale_alert_is_cancelled_instead_of_mailed(mailbox, monkeypatch):
    """An alert that stopped being news must not reach an inbox hours later."""
    with SmtpFixture() as smtp:
        monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
        monkeypatch.setenv('SMTP_PORT', str(smtp.port))
        monkeypatch.setenv('SMTP_SECURITY', 'plain')
        monkeypatch.setenv('SMTP_FROM', 'alerts@example.org')
        queue_alert()
        with connect() as db:
            db.execute('UPDATE incidents SET published_at=?', ((datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(),))
        deliver_email()
        assert smtp.messages == []
    row = delivery_row()
    assert row['status'] == 'cancelled' and row['attempts'] == 0 and row['sent_at'] is None


def test_resolution_is_not_suppressed_by_the_freshness_window(mailbox, monkeypatch):
    """A correction to an old event is still worth sending."""
    with SmtpFixture() as smtp:
        monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
        monkeypatch.setenv('SMTP_PORT', str(smtp.port))
        monkeypatch.setenv('SMTP_SECURITY', 'plain')
        monkeypatch.setenv('SMTP_FROM', 'alerts@example.org')
        queue_alert()
        with connect() as db:
            db.execute('UPDATE incidents SET published_at=?, verification=?', ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(), 'Resolved'))
        deliver_email()
        assert len(smtp.messages) == 1
    assert delivery_row()['status'] == 'accepted'


def test_superseded_alert_version_is_cancelled(mailbox, monkeypatch):
    """If a newer version replaced the alert text, the queued copy must not arrive."""
    with SmtpFixture() as smtp:
        monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
        monkeypatch.setenv('SMTP_PORT', str(smtp.port))
        monkeypatch.setenv('SMTP_SECURITY', 'plain')
        monkeypatch.setenv('SMTP_FROM', 'alerts@example.org')
        queue_alert()
        with connect() as db:
            # The incident was revised after the alert text was queued.
            db.execute('UPDATE incidents SET version=version+1 WHERE id=(SELECT incident_id FROM notifications ORDER BY id LIMIT 1)')
        deliver_email()
        assert smtp.messages == []
    row = delivery_row()
    assert row['status'] == 'cancelled' and row['attempts'] == 0
