#!/usr/bin/env bash
# Browser integration smoke runner.
#
# Starts the FastAPI application against a disposable database, then runs the
# full-stack browser test. Failures are printed and surfaced as GitHub check
# annotations, because annotations remain readable from restricted networks
# where the raw job log and artifacts cannot be downloaded.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python}"
DATABASE_PATH="${DATABASE_PATH:-/tmp/disaster-browser-ci.db}"
USGS_ENABLED="${USGS_ENABLED:-false}"
export PYTHON DATABASE_PATH USGS_ENABLED
rm -f "$DATABASE_PATH" "$DATABASE_PATH-wal" "$DATABASE_PATH-shm"

WORK="$(mktemp -d)"
TEST_LOG="$WORK/smoke.log"
API_LOG="$WORK/server.log"
RESULTS="$REPO_ROOT/web/test-results"
mkdir -p "$RESULTS"

stop_server() {
  if [ -n "${API_PID:-}" ] && kill -0 "$API_PID" 2>/dev/null; then
    kill "$API_PID" 2>/dev/null
    wait "$API_PID" 2>/dev/null
    API_PID=""
  fi
}

annotate() {
  # Keep the last lines, which hold the checkpoints and the diagnostics block.
  grep -v '^$' "$TEST_LOG" | tail -24 | tr -d '\r' | sed 's/%/%25/g' | while IFS= read -r line; do
    printf '::error::%s\n' "${line:0:220}"
  done
  printf '::error::--- last server lines ---\n'
  tail -6 "$API_LOG" | tr -d '\r' | sed 's/%/%25/g' | while IFS= read -r line; do
    printf '::error::%s\n' "${line:0:220}"
  done
}

fail() {
  echo "browser smoke failed: $1"
  [ -f "$TEST_LOG" ] && cp "$TEST_LOG" "$RESULTS/browser-smoke.log"
  [ -f "$API_LOG" ] && cp "$API_LOG" "$RESULTS/api-server.log"
  annotate
  stop_server
  exit 1
}

"$PYTHON" -m backend.cli sample >>"$API_LOG" 2>&1 || fail "could not load the historical sample fixture"

"$PYTHON" -m uvicorn backend.app:app --host 127.0.0.1 --port 8001 >>"$API_LOG" 2>&1 &
API_PID=$!
trap stop_server EXIT

"$PYTHON" - <<'PY' >>"$API_LOG" 2>&1
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

TEST_URL="${TEST_URL:-http://127.0.0.1:8001}" npm run test:browser --prefix web >"$TEST_LOG" 2>&1
STATUS=$?
stop_server

if [ $STATUS -ne 0 ]; then
  fail "browser test exited with status $STATUS"
fi

cat "$TEST_LOG"
