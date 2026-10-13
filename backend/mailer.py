"""Outbound email: one place builds messages, one place talks SMTP.

The worker, the operator verification command (`python -m backend.cli
email-check`) and the tests all use this module, so a channel that is proven
with the CLI is proven for real alerts too.

Configuration (environment only, never committed and never sent to the browser):

| Variable | Default | Notes |
|---|---|---|
| `SMTP_HOST` | unset | Mail submission host. Unset means email is disabled. |
| `SMTP_PORT` | `587` | 587 for STARTTLS, 465 for implicit TLS, 25 for `plain`. |
| `SMTP_SECURITY` | `starttls` | `starttls`, `ssl` or `plain`. `plain` is only for a local relay. |
| `SMTP_USER` / `SMTP_PASSWORD` | unset | Authenticate only when `SMTP_USER` is set. |
| `SMTP_FROM` | unset | Envelope/from address. Required for any send. |
| `SMTP_TIMEOUT` | `20` | Seconds for connect and command replies. |

Failure reasons never contain the password: it is redacted from every message
this module raises.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

DEFAULT_PORT = 587
DEFAULT_TIMEOUT = 20
SECURITY_MODES = ('starttls', 'ssl', 'plain')
ALERT_SUBJECT = 'Disaster Assistant — incident update (not a government warning)'
TEST_SUBJECT = 'Disaster Assistant — SMTP verification message (not a government warning)'
VERIFY_SUBJECT = 'Confirm your email address for disaster alerts'
RECOVERY_SUBJECT = 'Reset your disaster-assistant password'
NOTICE_SUBJECT = 'Disaster Assistant — sign-up notice for an existing account'


class MailError(RuntimeError):
    """Delivery or configuration failure, safe to print (never contains secrets)."""


def settings():
    """Read the SMTP configuration once, validating the shapes we rely on."""
    security = (os.getenv('SMTP_SECURITY', 'starttls') or 'starttls').strip().lower()
    if security not in SECURITY_MODES:
        raise MailError(f'SMTP_SECURITY must be one of {", ".join(SECURITY_MODES)}, got {security!r}')
    try:
        port = int(os.getenv('SMTP_PORT', str(DEFAULT_PORT)))
    except ValueError:
        raise MailError('SMTP_PORT must be a whole number') from None
    if not 1 <= port <= 65535:
        raise MailError('SMTP_PORT must be between 1 and 65535')
    try:
        timeout = float(os.getenv('SMTP_TIMEOUT', str(DEFAULT_TIMEOUT)))
    except ValueError:
        raise MailError('SMTP_TIMEOUT must be a number of seconds') from None
    host = os.getenv('SMTP_HOST', '').strip()
    sender = os.getenv('SMTP_FROM', '').strip()
    return {
        'host': host,
        'port': port,
        'security': security,
        'user': os.getenv('SMTP_USER', '').strip(),
        'password': os.getenv('SMTP_PASSWORD', ''),
        'sender': sender,
        'timeout': timeout,
        'configured': bool(host and sender),
    }


def describe(config=None):
    """One-line configuration summary. Never includes the password."""
    config = config or settings()
    return (
        f"host={config['host'] or '(unset)'} port={config['port']} "
        f"security={config['security']} from={config['sender'] or '(unset)'} "
        f"auth={'yes' if config['user'] else 'no'}"
    )


def build_message(recipient, subject, body, *, message_id=None, config=None):
    """Build the exact message that is about to be sent."""
    config = config or settings()
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = config['sender']
    message['To'] = recipient
    if message_id:
        message['Message-ID'] = message_id
    message['Auto-Submitted'] = 'auto-generated'
    message['X-Disaster-Assistant-Channel'] = 'email'
    message.set_content(body)
    return message


def send(message, config=None):
    """Deliver one prepared message. Raises MailError on any failure.

    Returns the two-digit SMTP reply code of the accepting command so callers can
    report what the provider actually said.
    """
    config = config or settings()
    if not config['configured']:
        raise MailError('SMTP_HOST and SMTP_FROM must both be set before email can be sent')
    context = ssl.create_default_context()
    try:
        if config['security'] == 'ssl':
            client = smtplib.SMTP_SSL(config['host'], config['port'], timeout=config['timeout'], context=context)
        else:
            client = smtplib.SMTP(config['host'], config['port'], timeout=config['timeout'])
    except Exception as exc:
        raise MailError(_clean(f'{type(exc).__name__} connecting to {config["host"]}:{config["port"]} '
                               f'({config["security"]}): {exc}', config)) from None
    with client:
        try:
            if config['security'] == 'starttls':
                client.starttls(context=context)
                client.ehlo()
            if config['user']:
                client.login(config['user'], config['password'])
            client.send_message(message)
        except Exception as exc:
            raise MailError(_clean(f'{type(exc).__name__}: {exc}', config)) from None
        return client.noop()[0]


def _clean(text, config):
    """Remove anything that must never appear in a log line or API response."""
    password = config.get('password') if config else ''
    if password:
        text = text.replace(password, '***')
    return text


def send_alert(recipient, body, notification_id):
    """Send one incident alert for a queued notification."""
    message = build_message(
        recipient,
        ALERT_SUBJECT,
        body,
        message_id=f'<notification-{notification_id}@disaster-assistant.local>',
    )
    return send(message)


def send_probe(recipient, detail=''):
    """Send the operator's own test message, clearly marked as a test."""
    body = (
        'This is a test message sent by the disaster-assistant operator command.\n\n'
        'Nothing has happened: there is no incident behind this message. It exists to prove\n'
        'that the configured SMTP provider accepts and delivers mail from this deployment.\n'
        'Real alerts are labelled "incident update" and always carry the incident, its\n'
        'verification state and where to find official guidance.\n\n'
        'This is not an official emergency warning service.\n'
    )
    if detail:
        body += f'\nDiagnostic detail:\n{detail}\n'
    return send(build_message(recipient, TEST_SUBJECT, body, message_id='<smtp-verification@disaster-assistant.local>'))


def send_account_verification(recipient, url):
    """Ask a new account holder to confirm their own address.

    Until this succeeds the account receives in-app notifications but no email,
    so a mistyped address can never be signed up for alerts by someone else.
    """
    body = (
        'Someone asked to receive disaster alerts at this address.\n\n'
        'Confirm it by opening this link (valid for 24 hours, usable once):\n'
        f'{url}\n\n'
        'Until you confirm, the account receives in-app notifications only. Email alerts are\n'
        'never sent to an unconfirmed address. If you did not create this account, ignore this\n'
        'message: no alerts will be sent here and the address can be removed by replying to the\n'
        'operator of this deployment.\n\n'
        'Disaster Assistant is not an official emergency warning service. Always follow your\n'
        'local authorities.\n'
    )
    return send(build_message(recipient, VERIFY_SUBJECT, body, message_id='<account-verify@disaster-assistant.local>'))


def send_account_notice(recipient, url):
    """Tell an existing account holder that a second sign-up used their address.

    This is what replaces the registration endpoint saying "that address is taken":
    the answer the requester sees is identical for a new and an existing address,
    and only the owner of the mailbox learns which of the two happened. The
    requester cannot read this message, so nothing is disclosed by it.
    """
    body = (
        'A sign-up request was made with this address, and an account already exists for it.\n\n'
        'Nothing changed: the password, saved locations and notification settings on that account\n'
        f'are untouched. Sign in here with the existing password:\n{url}\n\n'
        'If you have forgotten the password, use the "Forgot your password?" link on that page.\n\n'
        'If you did not ask for this, no action is needed: the duplicate request was refused and\n'
        'no data was modified. This message is sent at most once an hour per address.\n\n'
        'Disaster Assistant is not an official emergency warning service.\n'
    )
    return send(build_message(recipient, NOTICE_SUBJECT, body, message_id='<account-notice@disaster-assistant.local>'))


def send_password_recovery(recipient, url):
    """Single-use password reset link. Never reveals whether the account exists."""
    body = (
        'A password reset was requested for this address.\n\n'
        'Choose a new password by opening this link (valid for 1 hour, usable once):\n'
        f'{url}\n\n'
        'Setting a new password signs out every device that is currently signed in. If you did\n'
        'not request this, ignore the message: nothing changes and the link expires by itself.\n\n'
        'Disaster Assistant is not an official emergency warning service.\n'
    )
    return send(build_message(recipient, RECOVERY_SUBJECT, body, message_id='<password-recovery@disaster-assistant.local>'))
