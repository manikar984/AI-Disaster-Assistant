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
  # The workspace runs exactly what a deployment runs: one API and one worker.
  for pid in "${API_PID:-}" "${WORKER_PID:-}"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
    fi
  done
  API_PID=""
  WORKER_PID=""
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

# The ingestion worker runs alongside the API, exactly as systemd starts it in a
# deployment, so the smoke run covers the heartbeat and the job loop too.
"$PYTHON" -m backend.worker >>"$API_LOG" 2>&1 &
WORKER_PID=$!

"$PYTHON" - <<'PY' >>"$API_LOG" 2>&1
import json, sys, time, urllib.request
health = False
for _ in range(60):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8001/api/health', timeout=2) as response:
            health = response.status == 200
        if health:
            with urllib.request.urlopen('http://127.0.0.1:8001/api/ready', timeout=2) as response:
                if json.load(response).get('checks', {}).get('worker') == 'alive':
                    sys.exit(0)
    except OSError:
        pass
    time.sleep(1)
sys.exit('API healthy but the worker never reported a heartbeat' if health else 'API did not start')
PY
[ $? -eq 0 ] || fail "the API or the worker did not become ready on port 8001"

TEST_URL="${TEST_URL:-http://127.0.0.1:8001}" npm run test:browser --prefix web >"$TEST_LOG" 2>&1
STATUS=$?
if [ $STATUS -ne 0 ]; then
  stop_server
  fail "browser test exited with status $STATUS"
fi

# The same deployment must satisfy the release gate it will be judged by.
printf '\n[smoke] release-gate checks (deploy/verify-live.sh)\n' >>"$TEST_LOG"
bash deploy/verify-live.sh "http://127.0.0.1:8001" >>"$TEST_LOG" 2>&1
GATE=$?
stop_server
if [ $GATE -ne 0 ]; then
  fail "release-gate checks reported $GATE failure(s)"
fi

cat "$TEST_LOG"
