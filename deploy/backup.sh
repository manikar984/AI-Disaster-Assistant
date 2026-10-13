#!/usr/bin/env bash
# Consistent SQLite backup with retention pruning.
#
# Uses the sqlite3 online backup API, so the snapshot is consistent even while
# the API and worker are writing; a plain file copy of a live WAL database is
# not. Intended to run from deploy/disaster-backup.timer every night.
#
#   DATABASE_PATH=/var/lib/disaster-assistant/assistant.db \
#   BACKUP_DIR=/var/backups/disaster-assistant bash deploy/backup.sh
#
# Overridable: DATABASE_PATH, BACKUP_DIR, BACKUP_RETENTION_DAYS, PYTHON.
# Verified end to end by tests/test_deploy.py (snapshot contents, integrity and
# retention pruning against a temporary database).

set -euo pipefail

DATABASE="${DATABASE_PATH:-/var/lib/disaster-assistant/assistant.db}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/disaster-assistant}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
PYTHON="${PYTHON:-python3}"

if [ ! -f "$DATABASE" ]; then
  echo "No database at $DATABASE; nothing to back up." >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$BACKUP_DIR/assistant-$stamp.db"

"$PYTHON" - "$DATABASE" "$target" <<'PY'
import sqlite3
import sys

source_path, target_path = sys.argv[1], sys.argv[2]
source = sqlite3.connect(f'file:{source_path}?mode=ro', uri=True)
target = sqlite3.connect(target_path)
try:
    source.backup(target)
    integrity = target.execute('PRAGMA integrity_check').fetchone()[0]
    if integrity != 'ok':
        raise SystemExit(f'integrity check failed: {integrity}')
    tables = {row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for required in ('incidents', 'reports', 'sources', 'users'):
        if required not in tables:
            raise SystemExit(f'backup is missing the {required} table')
finally:
    target.close()
    source.close()
print(f'verified snapshot with {len(tables)} tables')
PY

chmod 600 "$target"
echo "Backup written: $target"

# Retention: drop snapshots older than BACKUP_RETENTION_DAYS (default 30).
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'assistant-*.db' -mtime "+$RETENTION_DAYS" -print -delete
