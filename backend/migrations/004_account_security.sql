-- Account security: verified email addresses, single-use expiring tokens, and
-- session metadata for listing and idle expiry.

ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN email_verified_at TEXT;

-- Accounts that already existed were created before verification was possible.
-- They are grandfathered as verified (an operator-action audit row records it)
-- so that enabling this feature never silently stops an existing subscriber's
-- alerts; every account created from now on must confirm its address.
UPDATE users SET email_verified=1, email_verified_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
 WHERE email_verified=0;

INSERT INTO audit(actor_id, action, note, created_at)
SELECT NULL, 'migration', '004: grandfathered existing accounts as email-verified (created before verification existed)', strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
 WHERE EXISTS (SELECT 1 FROM users);

CREATE TABLE IF NOT EXISTS tokens(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
  kind TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);
CREATE INDEX IF NOT EXISTS tokens_user_kind ON tokens(user_id, kind);
CREATE INDEX IF NOT EXISTS tokens_expires ON tokens(expires_at);

-- Sessions: creation time and last-seen time. Tokens themselves are still only
-- ever stored as a SHA-256 hash, and the cookie value is never persisted raw.
ALTER TABLE sessions ADD COLUMN created_at TEXT;
ALTER TABLE sessions ADD COLUMN last_seen_at TEXT;
UPDATE sessions
   SET created_at=COALESCE(created_at, strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
       last_seen_at=COALESCE(last_seen_at, strftime('%Y-%m-%dT%H:%M:%f+00:00','now'));
