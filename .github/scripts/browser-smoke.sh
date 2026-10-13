#!/usr/bin/env bash
# Browser integration smoke runner.
#
# Starts the FastAPI application against a disposable database, then runs the
# full-stack browser test. Failures are printed and surfaced as GitHub check
# annotations, because the annotations are readable even from restricted
# environments where the raw job log cannot be downloaded.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

DATABASE_PATH="${DATABASE_PATH:-/tmp/disaster-browser-ci.db}"
export DATABASE_PATH
export USGS_ENABLED="${USGS_ENABLED:-false}"
PYTHON="${PYTHON:-python}"
export PYTHON
rm -f "$DATABASE_PATH" "$DATABASE_PATH-wal" "$DATABASE_PATH-shm"

LOG="${RUNNER_TEMP:-/tmp}/browser-smoke.log"
mkdir -p "$(dirname "$LOG")"
: > "$LOG"

annotate() {
  # Truncate to the annotation limit and keep the last lines, which hold the error.
  tail -c 1200 "$LOG" | tr -d '\r' | sed 's/%/%25/g' | while IFS= read -r line; do
    [ -n "$line" ] && printf '::error::%s\n' "$line"
  done
}

fail() {
  echo "browser smoke failed: $1"
  annotate
  # Keep the full log beside the failure screenshot so the uploaded artifact is self-contained.
  mkdir -p "$REPO_ROOT/web/test-results"
  cp "$LOG" "$REPO_ROOT/web/test-results/browser-smoke.log" 2>/dev/null
  [ -n "${API_PID:-}" ] && kill "$API_PID" 2>/dev/null
  exit 1
}

"$PYTHON" -m backend.cli sample >>"$LOG" 2>&1 || fail "could not load the historical sample fixture"

"$PYTHON" -m uvicorn backend.app:app --host 127.0.0.1 --port 8001 >>"$LOG" 2>&1 &
API_PID=$!

"$PYTHON" - <<'PY' >>"$LOG" 2>&1
import sys, time, urllib.request
for _ in range(40):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8001/api/health', timeout=2) as response:
            if response.status == 200:
                sys.exit(0)
    except OSError:
        time.sleep(1)
sys.exit('API did not start')
PY
[ $? -eq 0 ] || fail "API did not become healthy on port 8001"

TEST_URL="${TEST_URL:-http://127.0.0.1:8001}" npm run test:browser --prefix web >>"$LOG" 2>&1
STATUS=$?
kill "$API_PID" 2>/dev/null

if [ $STATUS -ne 0 ]; then
  fail "browser test exited with status $STATUS"
fi
cat "$LOG"
