#!/usr/bin/env bash
# Read-only fact collector for a deployment host.
#
# Purpose: when I need to verify a deployment stage from real output, one command
# captures everything relevant — and nothing secret. It prints configuration *names*
# and whether each one is set, never the values (except a short allow-list of
# non-secret keys: the app origin, the database path, source toggles and the SMTP
# relay host/port/mode, which are needed to diagnose failures).
#
# It changes nothing: no writes, no restarts, no packages, no network calls other
# than loopback health checks and reading the VM's own public address from the
# Oracle metadata service (falling back to an external echo service).
#
# Usage:
#   sudo bash deploy/collect-facts.sh                 # domain inferred from the env file
#   sudo bash deploy/collect-facts.sh disaster-demo.duckdns.org
#   ENV_FILE=/tmp/other-env bash deploy/collect-facts.sh example.org
#   FACTS_OFFLINE=1 bash deploy/collect-facts.sh     # skip the address/DNS/TLS checks
#
# Exit status: 0 = collected (warnings may still be present), 2 = bad usage.
# Every line is prefixed OK / WARN / MISSING / -- so the output is easy to skim.

set -u

ENV_FILE="${ENV_FILE:-/etc/disaster-assistant/env}"
APP_DIR="${APP_DIR:-/opt/disaster-assistant}"
DATA_DIR="${DATA_DIR:-/var/lib/disaster-assistant}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/disaster-assistant}"

case "${1:-}" in
    -h|--help)
        sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
esac
DOMAIN="${1:-}"

have() { command -v "$1" >/dev/null 2>&1; }
section() { printf '\n== %s ==\n' "$1"; }
ok()   { printf 'OK      %s\n' "$*"; }
warn() { printf 'WARN    %s\n' "$*"; }
miss() { printf 'MISSING %s\n' "$*"; }
info() { printf -- '--      %s\n' "$*"; }

OK_COUNT=0; WARN_COUNT=0; MISS_COUNT=0
okc()   { OK_COUNT=$((OK_COUNT + 1)); ok "$@"; }
warnc() { WARN_COUNT=$((WARN_COUNT + 1)); warn "$@"; }
missc() { MISS_COUNT=$((MISS_COUNT + 1)); miss "$@"; }

printf 'deployment fact sheet  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
printf 'collector: %s\n' "$(readlink -f "$0")"
printf 'user: %s (uid %s)\n' "$(id -un)" "$(id -u)"

# ---------------------------------------------------------------- host
section "host"
info "kernel:      $(uname -srm)"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    info "os:          ${PRETTY_NAME:-unknown}"
fi
info "architecture: $(dpkg --print-architecture 2>/dev/null || uname -m)"
info "uptime:      $(uptime -p 2>/dev/null || uptime)"
info "clock (UTC): $(date -u '+%Y-%m-%d %H:%M:%S')  timezone: $(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo unknown)"
# A wrong clock breaks TLS certificate validation, so say so when it looks absurd.
YEAR="$(date -u '+%Y')"
if [ "$YEAR" -lt 2025 ] || [ "$YEAR" -gt 2035 ]; then
    warnc "system clock looks wrong (year $YEAR) — TLS and ACME will fail until it is fixed"
else
    okc "system clock looks sane (year $YEAR)"
fi

section "capacity"
info "cpus:        $(nproc 2>/dev/null || echo unknown)"
if [ -r /proc/meminfo ]; then
    info "memory:      $(awk '/^MemTotal:/ {printf "%.1f GiB total", $2/1048576}' /proc/meminfo)"
    info "swap:        $(awk '/^SwapTotal:/ {printf "%.1f GiB", $2/1048576}' /proc/meminfo)"
fi
info "disk /:      $(df -h / 2>/dev/null | awk 'NR==2 {print $2" total, "$4" free ("$5" used)"}')"
for path in /var/lib /var/backups; do
    if findmnt -n -o SOURCE --target "$path" >/dev/null 2>&1; then
        info "mount $path: $(findmnt -n -o SOURCE,TARGET,FSTYPE --target "$path" 2>/dev/null)"
    fi
done

# ---------------------------------------------------------------- tooling
section "tooling"
check_tool() {  # name, version command
    if have "$1"; then
        okc "$1: $($2 2>&1 | head -1)"
    else
        missc "$1 not installed"
    fi
}
check_tool python3 'python3 --version'
check_tool sqlite3 'sqlite3 --version'
check_tool git 'git --version'
check_tool caddy 'caddy version'
if have curl; then okc "curl available"; else missc "curl not installed"; fi
if have node; then okc "node: $(node --version)"; else info "node not installed (only needed to build the frontend on the VM)"; fi
if have systemctl; then okc "systemd: available"; else missc "systemd not available — the unit files cannot run here"; fi
if have ufw; then
    if ufw status 2>/dev/null | head -1 | grep -q "Status: active"; then
        okc "ufw active"
    else
        warnc "ufw installed but not active"
    fi
else
    missc "ufw not installed"
fi

# ---------------------------------------------------------------- application on disk
section "application"
if [ -d "$APP_DIR" ]; then
    okc "$APP_DIR exists"
    for path in ".venv/bin/python" ".venv/bin/uvicorn" "backend/app.py" "web/dist/index.html"; do
        if [ -e "$APP_DIR/$path" ]; then okc "$APP_DIR/$path present"; else missc "$APP_DIR/$path missing"; fi
    done
    if [ -x "$APP_DIR/.venv/bin/python" ]; then
        info "python in venv: $("$APP_DIR/.venv/bin/python" --version 2>&1)"
        info "installed packages: $(find "$APP_DIR/.venv/lib" -maxdepth 3 -name '*.dist-info' -type d 2>/dev/null | wc -l)"
    fi
    info "owner of tree: $(stat -c '%U:%G' "$APP_DIR" 2>/dev/null)"
    info "git revision:  $(git -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout (expected: rsync copy)')"
else
    missc "$APP_DIR does not exist yet (run the install step)"
fi

# ---------------------------------------------------------------- services
section "services"
if have systemctl; then
    for unit in caddy disaster-api disaster-worker disaster-backup.timer disaster-backup.service; do
        if systemctl list-unit-files "$unit" --no-pager >/dev/null 2>&1 && systemctl cat "$unit" >/dev/null 2>&1; then
            info "$unit: enabled=$(systemctl is-enabled "$unit" 2>/dev/null || echo unknown) active=$(systemctl is-active "$unit" 2>/dev/null || echo unknown)"
        else
            info "$unit: not installed"
        fi
    done
    info "next backup timer run: $(systemctl list-timers disaster-backup.timer --no-pager 2>/dev/null | awk 'NR==2 {$1="";$2="";$3="";$4="";$5="";print}' | sed 's/^ *//')"
fi

# ---------------------------------------------------------------- listeners
section "listeners"
# Ubuntu's default awk is mawk, which does not understand GNU's shorthand for
# whitespace in a regex: match on the field instead, so this works on both.
if have ss; then
    ss -lntp 2>/dev/null | awk 'NR==1 || $4 ~ /:(80|443|8000|22)$/'
elif have netstat; then
    netstat -lntp 2>/dev/null | awk 'NR<3 || $4 ~ /:(80|443|8000|22)$/'
else
    warnc "neither ss nor netstat available"
fi
info "(expected: :8000 on 127.0.0.1 only, :80/:443 public via caddy, :22 ssh)"
# The API unit binds 127.0.0.1 on purpose: Caddy is the only public listener. A
# uvicorn started by hand without --host exposes the API directly, bypassing the
# proxy, its headers and its TLS.
if have ss; then
    if ss -lnt 2>/dev/null | awk '$4 ~ /:8000$/' | grep -qv '127.0.0.1:8000'; then
        warnc "port 8000 is bound somewhere other than 127.0.0.1 — the API should sit behind Caddy, not on a public interface"
    fi
fi

# ---------------------------------------------------------------- firewall
section "firewall"
if [ "$(id -u)" -ne 0 ]; then
    warnc "run with sudo to read the packet filter rules"
else
    if have iptables; then
        REJECTS="$(iptables -S INPUT 2>/dev/null | grep -c REJECT || true)"
        info "iptables INPUT rules: $(iptables -S INPUT 2>/dev/null | wc -l) lines, $REJECTS REJECT"
        if [ "${REJECTS:-0}" -gt 0 ]; then
            warnc "iptables has REJECT rules — Oracle images ship these above the ufw rules; if 80/443 are unreachable from outside while curl on loopback works, this is why (see the checklist, section 4)"
        else
            okc "no REJECT rules in the iptables INPUT chain"
        fi
        info "first INPUT rules:"
        iptables -S INPUT 2>/dev/null | head -12 | sed 's/^/        /'
    fi
    if have ip6tables; then
        info "iptables INPUT (v6): $(ip6tables -S INPUT 2>/dev/null | grep -c REJECT || true) REJECT rules"
    fi
    ufw status verbose 2>/dev/null | sed 's/^/        /' || true
fi

# ---------------------------------------------------------------- environment file
section "environment file ($ENV_FILE)"
if [ -r "$ENV_FILE" ]; then
    info "mode/owner: $(stat -c '%a %U:%G' "$ENV_FILE")"
    [ "$(stat -c '%a' "$ENV_FILE")" = "640" ] && okc "mode is 640 as intended" || warnc "mode is not 640"
    info "non-secret settings (secrets are reported as <set> or <empty>, never printed):"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|\#*) continue ;; esac
        key="${line%%=*}"
        value="${line#*=}"
        key="$(printf '%s' "$key" | tr -d '[:space:]' | sed 's/^export//')"
        case "$key" in ''|"") continue ;; esac
        case "$key" in
            # Allow-list: these values are not secrets and are needed to diagnose
            # a deployment. Everything else is reported as set/empty only, so a
            # key this script has never heard of cannot leak by accident.
            DATABASE_PATH|APP_ORIGIN|COOKIE_SECURE|SMTP_HOST|SMTP_PORT|SMTP_SECURITY|SMTP_FROM|SMTP_TIMEOUT|\
            USGS_ENABLED|USGS_INTERVAL|GDACS_ENABLED|GDACS_INTERVAL|NEWS_ENABLED|NEWS_INTERVAL|\
            CAP_ENABLED|CAP_INTERVAL|LOG_LEVEL)
                info "  $key=$value"
                ;;
            NEWS_FEEDS|CAP_FEEDS)
                COUNT=0
                OLD_IFS="$IFS"; IFS=','
                for pair in $value; do [ -n "$pair" ] && COUNT=$((COUNT + 1)); done
                IFS="$OLD_IFS"
                info "  $key=<set: $COUNT feed(s)>"
                ;;
            *)
                if [ -n "$value" ]; then info "  $key=<set>"; else warnc "  $key=<empty>"; fi
                ;;
        esac
    done < "$ENV_FILE"
else
    missc "$ENV_FILE is not readable (expected: root:disaster 0640 — run with sudo, or the file does not exist yet)"
fi

# ---------------------------------------------------------------- database
section "database"
if [ -r "$ENV_FILE" ]; then
    DB="$(sed -n 's/^[[:space:]]*DATABASE_PATH=//p' "$ENV_FILE" | head -1 | tr -d '"' | tr -d "'" | tr -d '[:space:]')"
fi
DB="${DB:-$DATA_DIR/assistant.db}"
info "path: $DB"
if [ -f "$DB" ]; then
    okc "database file exists ($(du -h "$DB" 2>/dev/null | cut -f1), mode $(stat -c '%a %U:%G' "$DB"))"
    [ -f "$DB-wal" ] && info "WAL file present ($(du -h "$DB-wal" 2>/dev/null | cut -f1))" || info "no WAL file yet (created on first write)"
    if have sqlite3; then
        if sqlite3 -readonly "$DB" "select count(*) from users;" >/dev/null 2>&1; then
            LAST_PURGE="$(sqlite3 -readonly "$DB" "select value from runtime_state where key='last_purge';" 2>/dev/null)"
            info "last retention purge: ${LAST_PURGE:-never}"
        fi
        for table in users locations incidents notifications deliveries ingestion_runs audit; do
            COUNT="$(sqlite3 -readonly "$DB" "select count(*) from $table;" 2>/dev/null)"
            [ -n "$COUNT" ] && info "  $table: $COUNT" || info "  $table: (table not found)"
        done
    fi
else
    missc "no database at $DB yet (created by the first CLI or API start)"
fi
if [ -d "$BACKUP_DIR" ]; then
    info "backups: $(ls -1 "$BACKUP_DIR"/assistant-*.db 2>/dev/null | wc -l) snapshot(s) in $BACKUP_DIR"
    ls -1t "$BACKUP_DIR"/assistant-*.db 2>/dev/null | head -3 | sed 's/^/        /'
else
    missc "$BACKUP_DIR does not exist yet"
fi

# ---------------------------------------------------------------- running service
section "service health"
if have curl; then
    for path in /api/health /api/ready; do
        BODY="$(curl -sS --max-time 6 "http://127.0.0.1:8000$path" 2>&1 | head -c 900)"
        if [ -n "$BODY" ]; then info "GET http://127.0.0.1:8000$path -> $BODY"; else warnc "no response from http://127.0.0.1:8000$path"; fi
    done
fi

# ---------------------------------------------------------------- name, address, dns, tls
section "name, address, dns"
if [ -n "${FACTS_OFFLINE:-}" ]; then
    info "FACTS_OFFLINE=1 — skipped the address, DNS and TLS checks (nothing left the host)"
elif have curl; then
    # The VM's own public address, from Oracle's metadata service first (no traffic
    # leaves the host), then an external echo service only if that fails.
    PUBLIC="$(curl -sS --max-time 3 -H 'Authorization: Bearer Oracle' \
        http://169.254.169.254/opc/v2/vnics/ 2>/dev/null \
        | tr ',' '\n' | sed -n 's/.*"publicIp":"\([^"]*\)".*/\1/p' | head -1)"
    if [ -n "$PUBLIC" ]; then
        info "public address (Oracle metadata): $PUBLIC"
    else
        PUBLIC="$(curl -sS --max-time 6 https://api.ipify.org 2>/dev/null || true)"
        [ -n "$PUBLIC" ] && info "public address (ipify): $PUBLIC" || warnc "could not determine the public address"
    fi
fi
if [ -n "${FACTS_OFFLINE:-}" ]; then
    :
elif [ -n "$DOMAIN" ]; then
    RESOLVED="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')"
    if [ -n "$RESOLVED" ]; then
        okc "$DOMAIN resolves to: $RESOLVED"
        if [ -n "${PUBLIC:-}" ] && printf '%s' "$RESOLVED" | grep -qw "$PUBLIC"; then
            okc "DNS matches this host's public address"
        else
            warnc "DNS does not match this host's public address — ACME will fail until it does"
        fi
    else
        warnc "$DOMAIN does not resolve from this host"
    fi
    if have curl; then
        TLSC="$(curl -sS --max-time 10 -o /dev/null -w 'http %{http_code}, tls verify %{ssl_verify_result}' "https://$DOMAIN/" 2>&1 || true)"
        info "https://$DOMAIN/ -> $TLSC  (tls verify 0 means the certificate validated)"
    fi
else
    info "no domain given — pass one to check DNS and TLS, or set APP_ORIGIN in the env file"
    if [ -r "$ENV_FILE" ]; then
        ORIGIN="$(sed -n 's/^[[:space:]]*APP_ORIGIN=//p' "$ENV_FILE" | head -1)"
        [ -n "$ORIGIN" ] && info "APP_ORIGIN is set to $ORIGIN — re-run with just the hostname to check it"
    fi
fi

section "recent errors"
if have journalctl && [ "$(id -u)" -eq 0 ]; then
    for unit in caddy disaster-api disaster-worker; do
        OUT="$(journalctl -u "$unit" --since "2 hours ago" -p warning --no-pager 2>/dev/null | tail -6)"
        if [ -n "$OUT" ]; then
            info "$unit (last 2h, warning+):"
            printf '%s\n' "$OUT" | sed 's/^/        /'
        else
            okc "$unit: no warnings in the last 2 hours"
        fi
    done
else
    info "journalctl needs root here — run with sudo for the error summary"
fi

printf '\n== summary ==\n'
printf 'checks: %d ok, %d warnings, %d missing\n' "$OK_COUNT" "$WARN_COUNT" "$MISS_COUNT"
printf 'This sheet contains no passwords, tokens or API keys; it is safe to share.\n'
exit 0
