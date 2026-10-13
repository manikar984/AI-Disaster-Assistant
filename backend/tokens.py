"""Single-use, expiring account tokens.

Used for email verification and password recovery. Rules, all enforced here so
the API handlers stay small:

* the plaintext token exists only in the email that is sent; the database keeps
  a SHA-256 hash and nothing else;
* a token expires (verification 24 h, recovery 1 h) and can be redeemed exactly
  once — `consume` marks it used before the caller acts on it;
* issuing a new token of a kind invalidates that account's outstanding tokens of
  the same kind, so "resend" and "forgot" never leave two live links behind;
* expired rows are pruned opportunistically.

Nothing here is user-visible: the API never returns or logs a token value, and a
failed redemption is reported as the same generic message as an unknown token.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

#: Validity per token kind, in seconds.
LIFETIMES = {'verify': 24 * 60 * 60, 'reset': 60 * 60}
KINDS = tuple(LIFETIMES)


def _stamp():
    return datetime.now(timezone.utc)


def token_hash(token):
    return hashlib.sha256((token or '').encode()).hexdigest()


def issue(db, user_id, kind, lifetime_seconds=None):
    """Create a token of `kind` for a user and return its plaintext once."""
    if kind not in LIFETIMES:
        raise ValueError(f'unknown token kind {kind!r}')
    lifetime = lifetime_seconds or LIFETIMES[kind]
    stamp = _stamp()
    token = secrets.token_urlsafe(32)
    # One live token per kind: an older link stops working the moment a new one
    # is issued, which is what a user expects after pressing "resend".
    db.execute(
        'UPDATE tokens SET used_at=? WHERE user_id=? AND kind=? AND used_at IS NULL',
        (stamp.isoformat(), user_id, kind),
    )
    db.execute(
        'INSERT INTO tokens(user_id,kind,token_hash,created_at,expires_at) VALUES (?,?,?,?,?)',
        (user_id, kind, token_hash(token), stamp.isoformat(), (stamp + timedelta(seconds=lifetime)).isoformat()),
    )
    db.execute('DELETE FROM tokens WHERE expires_at<?', (stamp.isoformat(),))
    return token


def consume(db, kind, token):
    """Redeem a token once. Returns the user id, or None if it is not usable."""
    if not token or kind not in LIFETIMES:
        return None
    row = db.execute('SELECT * FROM tokens WHERE token_hash=? AND kind=?', (token_hash(token), kind)).fetchone()
    if row is None or row['used_at'] is not None:
        return None
    if row['expires_at'] <= _stamp().isoformat():
        return None
    db.execute('UPDATE tokens SET used_at=? WHERE id=?', (_stamp().isoformat(), row['id']))
    return row['user_id']


def outstanding(db, user_id, kind):
    return db.execute(
        'SELECT count(*) FROM tokens WHERE user_id=? AND kind=? AND used_at IS NULL AND expires_at>?',
        (user_id, kind, _stamp().isoformat()),
    ).fetchone()[0]
