#!/usr/bin/env bash
# Restore a snapshot, verifying it before anything is written.
#
#   bash deploy/restore.sh /var/backups/disaster-assistant/assistant-<stamp>.db \
#        --to /var/lib/disaster-assistant/assistant.db
#
# Run a drill with a throwaway target first:
#
#   bash deploy/restore.sh <snapshot> --to /tmp/restore-drill.db
#
# Safety rails, in order:
#   1. the snapshot is verified (integrity, foreign keys, required tables, row
#      counts) BEFORE a single byte is written to the target;
#   2. `--to` is mandatory — there is no default that could turn out to be the
#      live database;
#   3. an existing target is refused unless `--force` is given, and the live
#      database (`DATABASE_PATH`) is refused unless `--force` is given as well;
#   4. the copy is written to a temporary file beside the target, verified, then
#      moved into place, so a failure never leaves a half-written database where
#      the application could open it.
#
# Overridable: PYTHON, DATABASE_PATH (only used for the live-database check).
# Verified end to end by tests/test_restore.py, which drills a snapshot back into
# an isolated path and drives the API and the engine against the result.

set -euo pipefail

SNAPSHOT="${1:-}"
shift || true
TARGET=""
FORCE=0
OWNER=""
PYTHON="${PYTHON:-python3}"

if [ -z "$SNAPSHOT" ]; then
  echo "Usage: bash deploy/restore.sh <snapshot.db> --to <target.db> [--force] [--owner user:group]" >&2
  exit 2
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --to) TARGET="${2:-}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --owner) OWNER="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$TARGET" ]; then
  echo "Refusing to guess a destination: pass --to <target.db>." >&2
  echo "This is deliberate; a restore with a default target can silently replace a live database." >&2
  exit 2
fi

if [ ! -f "$SNAPSHOT" ]; then
  echo "No snapshot at $SNAPSHOT." >&2
  exit 1
fi

LIVE="${DATABASE_PATH:-}"
if [ -n "$LIVE" ] && [ "$FORCE" -eq 0 ] && [ "$(readlink -f "$TARGET" 2>/dev/null || echo "$TARGET")" = "$(readlink -f "$LIVE" 2>/dev/null || echo "$LIVE")" ]; then
  echo "Refusing to overwrite the live database at $TARGET without --force." >&2
  echo "Stop disaster-api and disaster-worker first, then re-run with --force." >&2
  exit 1
fi

if [ -e "$TARGET" ] && [ "$FORCE" -eq 0 ]; then
  echo "Target $TARGET already exists; pass --force to replace it." >&2
  exit 1
fi

target_dir="$(dirname "$TARGET")"
mkdir -p "$target_dir"
temporary="$(mktemp "$target_dir/.restore-XXXXXX.db")"
trap 'rm -f "$temporary"' EXIT

# 1 + 4: verify the snapshot, write the copy, verify the copy.
"$PYTHON" - "$SNAPSHOT" "$temporary" <<'PY'
import sqlite3
import sys

snapshot_path, target_path = sys.argv[1], sys.argv[2]
REQUIRED = (
    'users', 'sessions', 'locations', 'sources', 'ingestion_runs', 'reports',
    'incidents', 'incident_reports', 'timeline', 'notifications', 'deliveries',
    'audit', 'tokens', 'runtime_state', 'migrations',
)
COUNTS = ('users', 'locations', 'reports', 'incidents', 'notifications', 'deliveries')

def inspect(path, label):
    try:
        db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    except sqlite3.DatabaseError as exc:
        raise SystemExit(f'{label}: not a usable SQLite database ({exc})') from None
    try:
        try:
            integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise SystemExit(f'{label}: not a usable SQLite database ({exc})') from None
        if integrity != 'ok':
            raise SystemExit(f'{label}: integrity check failed: {integrity}')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in REQUIRED if name not in tables]
        if missing:
            raise SystemExit(f'{label}: missing table(s): {", ".join(missing)}')
        broken = db.execute('PRAGMA foreign_key_check').fetchall()
        if broken:
            raise SystemExit(f'{label}: {len(broken)} broken foreign-key reference(s)')
        counts = {}
        for name in COUNTS:
            counts[name] = db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
        return counts
    finally:
        db.close()

snapshot_counts = inspect(snapshot_path, 'snapshot')
print('Snapshot verified: integrity ok, no broken references, all required tables present')
print('  rows: ' + ', '.join(f'{name}={value}' for name, value in snapshot_counts.items()))

source = sqlite3.connect(f'file:{snapshot_path}?mode=ro', uri=True)
target = sqlite3.connect(target_path)
try:
    source.backup(target)
finally:
    target.close()
    source.close()

restored_counts = inspect(target_path, 'restored copy')
if restored_counts != snapshot_counts:
    raise SystemExit(f'restored copy does not match the snapshot: {restored_counts} != {snapshot_counts}')
print('Restored copy verified: integrity ok, row counts match the snapshot')
PY

chmod 640 "$temporary"
if [ -n "$OWNER" ]; then
  chown "$OWNER" "$temporary"
fi
mv -f "$temporary" "$TARGET"
trap - EXIT

echo "Restored: $SNAPSHOT -> $TARGET"
echo "Start the services and check /api/ready; run the drill on a throwaway target before trusting a real restore."
