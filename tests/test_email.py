"""Email channel tests over a real SMTP conversation on a loopback socket.

Nothing in the delivery path is mocked: the real worker selects the real queued
delivery, the real mailer builds a real MIME message and the real `smtplib`
client talks to a real SMTP server (the fixture below) over TCP. The fixture
stands in for the mail provider until the operator supplies credentials, so it
covers everything except the provider's own delivery to a human inbox — that
last step is what `python -m backend.cli email-check` proves on the deployment.
"""

import base64
import email
import email.policy
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone

import pytest

from backend import cli, mailer
from backend.db import connect, migrate, now
from backend.engine import ingest, qualify_notifications
from backend.worker import deliver_email


class SmtpFixture:
    """Minimal SMTP server: EHLO, STARTTLS, AUTH PLAIN, MAIL, RCPT, DATA, NOOP, QUIT."""

    def __init__(self, *, starttls=False, require_auth=None):
        self.starttls = starttls
        self.require_auth = require_auth
        self.messages = []
        self.session = []
        self.port = None
        self.ca_file = None
        self._context = None
        self._server = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self):
        if self.starttls:
            directory = tempfile.mkdtemp(prefix='smtp-fixture-')
            key, cert = os.path.join(directory, 'key.pem'), os.path.join(directory, 'cert.pem')
            subprocess.run(
                ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                 '-keyout', key, '-out', cert, '-subj', '/CN=localhost',
                 '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                check=True, capture_output=True,
            )
            self.ca_file = cert
            self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._context.load_cert_chain(cert, key)
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(('127.0.0.1', 0))
        self._server.listen(5)
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass
        return False

    def commands(self):
        return [line.upper() for line in self.session]

    def parsed(self, index=0):
        """The received message as a decoded object (unfolded headers, text body)."""
        return email.message_from_string(self.messages[index]['data'], policy=email.policy.default)

    # -- server ------------------------------------------------------------
    def _accept(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(connection,), daemon=True).start()

    @staticmethod
    def _reply(connection, line):
        connection.sendall((line + '\r\n').encode())

    def _session(self, connection):
        reader = connection.makefile('rb')
        try:
            self._reply(connection, '220 fixture.local ESMTP ready')
            authenticated = False
            envelope = {'from': None, 'to': []}
            body, data_mode = [], False
            while True:
                line = reader.readline()
                if not line:
                    return
                text = line.decode('utf-8', 'replace').rstrip('\r\n')
                if data_mode:
                    if text == '.':
                        data_mode = False
                        self.messages.append({'from': envelope['from'], 'to': list(envelope['to']), 'data': '\n'.join(body)})
                        body = []
                        self._reply(connection, '250 2.0.0 Ok: queued as FIXTURE1')
                    else:
                        body.append(text)
                    continue
                self.session.append(text)
                command, _, argument = text.partition(' ')
                command = command.upper()
                if command in ('EHLO', 'HELO'):
                    self._reply(connection, '250-fixture.local greets you')
                    self._reply(connection, '250-STARTTLS' if self.starttls else '250-8BITMIME')
                    self._reply(connection, '250 AUTH PLAIN LOGIN' if self.require_auth else '250 SMTPUTF8')
                elif command == 'STARTTLS':
                    if not self.starttls:
                        self._reply(connection, '502 5.5.1 STARTTLS not supported')
                    else:
                        self._reply(connection, '220 2.0.0 Ready to start TLS')
                        reader.close()
                        connection = self._context.wrap_socket(connection, server_side=True)
                        reader = connection.makefile('rb')
                elif command == 'AUTH':
                    mechanism, _, payload = argument.partition(' ')
                    if not self.require_auth:
                        self._reply(connection, '503 5.5.1 Authentication not required')
                    elif mechanism.upper() != 'PLAIN':
                        self._reply(connection, '504 5.5.4 Unrecognized authentication type')
                    else:
                        if not payload:
                            payload = reader.readline().decode().strip()
                        try:
                            decoded = base64.b64decode(payload).split(b'\x00')
                            user, password = decoded[-2].decode(), decoded[-1].decode()
                        except Exception:
                            user, password = '', ''
                        if (user, password) == self.require_auth:
                            authenticated = True
                            self._reply(connection, '235 2.7.0 Authentication successful')
                        else:
                            self._reply(connection, '535 5.7.8 Authentication credentials invalid')
                elif command == 'MAIL':
                    if self.require_auth and not authenticated:
                        self._reply(connection, '530 5.7.0 Authentication required')
                        continue
                    envelope['from'] = argument.partition(':')[2].strip().strip('<>')
                    self._reply(connection, '250 2.1.0 Sender ok')
                elif command == 'RCPT':
                    envelope['to'].append(argument.partition(':')[2].strip().strip('<>'))
                    self._reply(connection, '250 2.1.5 Recipient ok')
                elif command == 'DATA':
                    data_mode = True
                    self._reply(connection, '354 Send the message')
                elif command == 'NOOP':
                    self._reply(connection, '250 2.0.0 Ok')
                elif command == 'RSET':
                    self._reply(connection, '250 2.0.0 Ok')
                elif command == 'QUIT':
                    self._reply(connection, '221 2.0.0 Bye')
                    return
                else:
                    self._reply(connection, '502 5.5.2 Command not implemented')
        finally:
            try:
                connection.close()
            except OSError:
                pass


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
        db.execute("INSERT INTO users(email,password,role,created_at) VALUES (?,'x','user',?)", (email, now()))
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
