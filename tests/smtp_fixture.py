"""A real SMTP server on loopback, shared by the email and account tests.

The fixture is deliberately a genuine SMTP conversation — EHLO, STARTTLS or
implicit TLS with a self-signed certificate, AUTH PLAIN, MAIL/RCPT/DATA — so
tests that redeem a link from an email are exercising the same path a user does.
It stands in for the mail provider until the deployment supplies credentials.
"""

import base64
import email
import email.policy
import os
import socket
import ssl
import subprocess
import tempfile
import threading


class SmtpFixture:
    """Minimal SMTP server: EHLO, STARTTLS, AUTH PLAIN, MAIL, RCPT, DATA, NOOP, QUIT."""

    def __init__(self, *, starttls=False, implicit_tls=False, require_auth=None):
        self.starttls = starttls
        self.implicit_tls = implicit_tls
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
        if self.starttls or self.implicit_tls:
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
        if self.implicit_tls:
            # Port 465 semantics: TLS is negotiated before the server greets. A
            # client that refuses this self-signed certificate just hangs up.
            try:
                connection = self._context.wrap_socket(connection, server_side=True)
            except (ssl.SSLError, OSError):
                return
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
                        try:
                            connection = self._context.wrap_socket(connection, server_side=True)
                        except (ssl.SSLError, OSError):
                            return
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
