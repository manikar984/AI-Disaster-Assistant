import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from .db import connect, now

#: Absolute session lifetime and inactivity window, overridable per deployment.
SESSION_DAYS = int(os.getenv('SESSION_DAYS', '30'))
SESSION_IDLE_DAYS = int(os.getenv('SESSION_IDLE_DAYS', '7'))
#: How often a session's last-seen stamp is refreshed (avoid a write per request).
TOUCH_INTERVAL = timedelta(minutes=5)


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return salt + ':' + digest


def password_ok(password, stored):
    salt, expected = stored.split(':')
    actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(expected, actual)


def session_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(request: Request):
    """Resolve the session cookie to a user, enforcing both expiry rules.

    A session dies at its absolute expiry or after `SESSION_IDLE_DAYS` without
    use, whichever comes first. Expired rows are deleted *inside* the connection
    block: raising inside it would roll the delete back, so the decision is taken
    first and the error raised after the transaction commits.
    """
    raw = request.cookies.get('session', '')
    if not raw:
        raise HTTPException(401, 'Please sign in')
    token = session_hash(raw)
    stamp = datetime.now(timezone.utc)
    failure = None
    with connect() as db:
        row = db.execute(
            'SELECT s.expires_at,s.last_seen_at,u.id,u.email,u.role,u.email_verified '
            'FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?',
            (token,),
        ).fetchone()
        if row is None:
            failure = 'Please sign in'
        elif row['expires_at'] <= stamp.isoformat():
            db.execute('DELETE FROM sessions WHERE token=?', (token,))
            failure = 'Session expired; please sign in again'
        elif row['last_seen_at'] and row['last_seen_at'] < (stamp - timedelta(days=SESSION_IDLE_DAYS)).isoformat():
            db.execute('DELETE FROM sessions WHERE token=?', (token,))
            failure = 'Session expired after inactivity; please sign in again'
        elif not row['last_seen_at'] or row['last_seen_at'] < (stamp - TOUCH_INTERVAL).isoformat():
            db.execute('UPDATE sessions SET last_seen_at=? WHERE token=?', (stamp.isoformat(), token))
    if failure:
        raise HTTPException(401, failure)
    return {'id': row['id'], 'email': row['email'], 'role': row['role'], 'email_verified': row['email_verified']}


def admin_user(request: Request):
    user = current_user(request)
    if user['role'] != 'admin':
        raise HTTPException(403, 'Administrator access required')
    return user
