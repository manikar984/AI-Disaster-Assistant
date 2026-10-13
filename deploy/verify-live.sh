#!/usr/bin/env bash
# Release-gate checks against a running deployment.
#
#   bash deploy/verify-live.sh https://alerts.example.org
#   bash deploy/verify-live.sh https://alerts.example.org --to you@example.org
#
# Run it on the host after starting the services. It checks what a public
# deployment must actually satisfy - a healthy API, a live worker heartbeat, a
# source registry with no failing feed, the published evidence policy, the
# served frontend, HTTPS/HSTS where TLS terminates, and (with --to) an email
# proof through the operator command. Nothing secret is read or printed: the
# script only displays public API responses and the mailer's redacted summary.
#
# Exit status counts failed checks (0 = release gate passed).
#
# Verified here: syntax, the failure path, and a full run against the local
# pilot (see docs/VERIFICATION.md). Not verified here: a run on a real host with
# TLS and configured SMTP, which is exactly what the deployment owner performs.

set -uo pipefail

BASE="${1:-http://127.0.0.1:8000}"
shift || true
TO=""
if [ -x ".venv/bin/python" ]; then
  PYTHON="${PYTHON:-.venv/bin/python}"
else
  PYTHON="${PYTHON:-python3}"
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --to) TO="${2:-}"; shift 2 ;;
    --python) PYTHON="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
BASE="${BASE%/}"
WORK="$(mktemp -d)"
PASSED=0
FAILED=0

trap 'rm -rf "$WORK"' EXIT

ok() { PASSED=$((PASSED + 1)); printf 'PASS  %s\n' "$*"; }
bad() { FAILED=$((FAILED + 1)); printf 'FAIL  %s\n' "$*"; }
info() { printf 'INFO  %s\n' "$*"; }

body() { curl -fsS --max-time 20 "$BASE$1" 2>"$WORK/curl.err"; }

# check_json <name> <path> <python-expression over d>
check_json() {
  local name="$1" path="$2" expression="$3" text
  if ! text="$(body "$path")"; then
    bad "$name — request to $path failed: $(head -c 120 "$WORK/curl.err" | tr '\n' ' ')"
    return 1
  fi
  if printf '%s' "$text" | "$PYTHON" -c "import json,sys
data = sys.stdin.read()
try:
    d = json.loads(data)
except ValueError as exc:
    print('response was not JSON:', data[:120]); raise SystemExit(3)
sys.exit(0 if ($expression) else 3)" 2>"$WORK/check.err"; then
    ok "$name"
    return 0
  fi
  bad "$name — $(head -c 200 "$WORK/check.err" | tr '\n' ' ')"
  return 1
}

printf 'Verifying %s\n\n' "$BASE"

check_json 'API health' /api/health "d.get('status') == 'ok'"
check_json 'database reachable' /api/ready "d.get('checks', {}).get('database') is True"

# The worker must be pollable, not merely present: a stalled heartbeat means
# ingestion and the delivery queue have stopped.
if ready="$(body /api/ready)"; then
  state="$(printf '%s' "$ready" | "$PYTHON" -c "import json,sys;d=json.load(sys.stdin);c=d.get('checks',{});print(c.get('worker'),c.get('worker_heartbeat_age_seconds'))" 2>/dev/null)"
  set -- $state
  worker="${1:-unknown}"
  age="${2:-unknown}"
  case "$worker" in
    alive) ok "worker heartbeat is live (age ${age}s)" ;;
    *) bad "worker heartbeat is $worker (age ${age}s) — check systemctl status disaster-worker" ;;
  esac
  printf '%s' "$ready" | "$PYTHON" -c "import json,sys;d=json.load(sys.stdin);[info('readiness: '+p) for p in d.get('problems',[])]" 2>/dev/null
  printf '%s' "$ready" | "$PYTHON" -c "import json,sys;d=json.load(sys.stdin);c=d.get('checks',{});print('INFO  email channel:', c.get('email','(not reported)'))" 2>/dev/null
else
  bad 'readiness endpoint unreachable'
fi

if sources="$(body /api/sources)"; then
  printf '%s' "$sources" | "$PYTHON" -c "
import json, sys
rows = json.load(sys.stdin)
enabled = [r for r in rows if r.get('enabled')]
print(f'INFO  sources: {len(rows)} registered, {len(enabled)} enabled')
for row in rows:
    flag = 'error' if row.get('status') == 'error' else ('stale' if row.get('status') == 'stale' else 'ok')
    print(f\"INFO  {row['id']:<16} {row.get('status'):<9} {flag:<5} records={row.get('records_total', 0)} rejected={row.get('records_rejected', 0)}\")
sys.exit(4 if any(r.get('status') == 'error' for r in rows) else 0)
" 2>"$WORK/sources.err" || bad "source registry reports a failing feed — $(head -c 200 "$WORK/sources.err" | tr '\n' ' ')"
else
  bad 'source registry unreachable'
fi

check_json 'evidence policy published' /api/policy "bool(d.get('corroboration_rule')) and bool(d.get('notification_gates'))"
check_json 'live incident feed' '/api/incidents?mode=live' "isinstance(d, list)"
check_json 'sample provenance stays separate' '/api/incidents?mode=sample' "isinstance(d, list)"

if curl -fsS --max-time 20 "$BASE/" 2>"$WORK/curl.err" | grep -q 'id="root"'; then
  ok 'frontend served by the API'
else
  bad 'frontend entry point not served (run the frontend build)'
fi

case "$BASE" in
  https://*)
    if curl -fsSI --max-time 20 "$BASE/" 2>/dev/null | grep -qi '^strict-transport-security:'; then
      ok 'HTTPS with HSTS header'
    else
      bad 'no Strict-Transport-Security header — check the reverse proxy configuration'
    fi
    info 'session cookie flags are not checkable without an account: sign in in a browser and confirm Secure; HttpOnly; SameSite=Strict'
    ;;
  *)
    info 'not HTTPS: TLS/HSTS checks skipped (expected for a local run)'
    ;;
esac

if [ -n "$TO" ]; then
  if [ -n "${SMTP_HOST:-}" ]; then
    if "$PYTHON" -m backend.cli email-check --to "$TO"; then
      ok "email provable: provider accepted a test message for $TO"
      info 'the channel counts as proven only when that message arrives — record it in docs/VERIFICATION.md'
    else
      bad 'email-check failed: the provider did not accept the test message'
    fi
  else
    info 'email not proven in this shell: SMTP_* is not in the environment.'
    info 'Run:  set -a; . /etc/disaster-assistant/env; set +a; then'
    info "      $PYTHON -m backend.cli email-check --to $TO"
  fi
fi

printf '\n%s\n' "checks passed: $PASSED, failed: $FAILED"
if [ "$FAILED" -gt 0 ]; then
  printf 'Release gate NOT met.\n'
fi
exit "$FAILED"
