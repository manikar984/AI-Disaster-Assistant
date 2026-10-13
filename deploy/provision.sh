#!/usr/bin/env bash
# One-shot system provisioning for the disaster-assistant pilot.
#
# Target: a fresh Ubuntu 24.04 LTS host (Oracle Cloud Always Free Ampere VM or
# any equivalent). Idempotent: safe to re-run. It never writes a credential.
#
#   sudo DISASTER_DOMAIN=alerts-example.duckdns.org bash deploy/provision.sh
#   sudo DRY_RUN=1 DISASTER_DOMAIN=alerts-example.duckdns.org bash deploy/provision.sh
#
# It installs packages, creates the service account and directories, installs the
# systemd units and the Caddy configuration, and enables (but does not start) the
# services. Starting them is a separate, deliberate step in docs/DEPLOYMENT.md.
#
# Verified here: `bash -n` syntax and the backup script end to end
# (tests/test_deploy.py). Not verified here: no systemd or cloud host exists in
# the development sandbox, so this script has not provisioned a real machine yet.

set -euo pipefail

APP_USER="${APP_USER:-disaster}"
APP_DIR="${APP_DIR:-/opt/disaster-assistant}"
DATA_DIR="${DATA_DIR:-/var/lib/disaster-assistant}"
ETC_DIR="${ETC_DIR:-/etc/disaster-assistant}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/disaster-assistant}"
DOMAIN="${DISASTER_DOMAIN:-}"
DRY_RUN="${DRY_RUN:-0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf 'DRY-RUN: %s\n' "$*"
  else
    "$@"
  fi
}

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo DISASTER_DOMAIN=<name> bash deploy/provision.sh" >&2
  exit 1
fi
if [ -z "$DOMAIN" ]; then
  echo "Set DISASTER_DOMAIN to the public hostname, for example:" >&2
  echo "  sudo DISASTER_DOMAIN=alerts-example.duckdns.org bash deploy/provision.sh" >&2
  exit 1
fi

log "Installing packages (python, git, sqlite, caddy, ufw)"
run env DEBIAN_FRONTEND=noninteractive apt-get update -qq
run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip git sqlite3 ufw caddy

log "Creating service account ${APP_USER}"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  run useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
else
  log "Account already exists"
fi
run install -d -o "$APP_USER" -g "$APP_USER" -m 0755 "$APP_DIR"
run install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$DATA_DIR"
run install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$BACKUP_DIR"
# The environment file holds provider secrets: root-owned, group-readable by the
# service account only, never world-readable.
run install -d -o root -g "$APP_USER" -m 0750 "$ETC_DIR"
if [ ! -f "$ETC_DIR/env" ]; then
  run install -o root -g "$APP_USER" -m 0640 /dev/null "$ETC_DIR/env"
  log "Wrote an empty $ETC_DIR/env (fill it in before starting the services)"
else
  log "Keeping the existing $ETC_DIR/env"
fi

log "Installing systemd units"
for unit in disaster-api.service disaster-worker.service disaster-backup.service disaster-backup.timer; do
  if [ -f "$SCRIPT_DIR/$unit" ]; then
    run sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@DATA_DIR@|$DATA_DIR|g" -e "s|@BACKUP_DIR@|$BACKUP_DIR|g" \
      -e "s|@APP_USER@|$APP_USER|g" -e "s|@ETC_DIR@|$ETC_DIR|g" \
      "$SCRIPT_DIR/$unit" /etc/systemd/system/"$unit"
    log "installed /etc/systemd/system/$unit"
  fi
done

log "Installing the Caddy configuration for ${DOMAIN}"
if [ -f /etc/caddy/Caddyfile ] && ! grep -q "disaster-assistant" /etc/caddy/Caddyfile; then
  run cp -n /etc/caddy/Caddyfile /etc/caddy/Caddyfile.orig
fi
run sed -e "s|@DOMAIN@|$DOMAIN|g" "$SCRIPT_DIR/Caddyfile" /etc/caddy/Caddyfile

log "Configuring the firewall (SSH first, so provisioning cannot lock you out)"
run ufw allow OpenSSH
run ufw allow 80/tcp
run ufw allow 443/tcp
if [ "$DRY_RUN" != "1" ]; then
  ufw --force enable
fi

log "Reloading systemd and enabling services (not starting them yet)"
run systemctl daemon-reload
run systemctl enable caddy
run systemctl enable disaster-api
run systemctl enable disaster-worker
run systemctl enable disaster-backup.timer

cat <<NEXT

Provisioning complete. Next steps (see docs/DEPLOYMENT.md):
  1. Install the application into ${APP_DIR} (git clone + venv + frontend build).
  2. Fill in ${ETC_DIR}/env, including APP_ORIGIN=https://${DOMAIN} and COOKIE_SECURE=true.
  3. Start in order: systemctl start caddy disaster-api disaster-worker disaster-backup.timer
  4. Prove it: curl https://${DOMAIN}/api/ready, then
     ${APP_DIR}/.venv/bin/python -m backend.cli sources
     ${APP_DIR}/.venv/bin/python -m backend.cli poll --source usgs
     ${APP_DIR}/.venv/bin/python -m backend.cli email-check --to <your mailbox>
NEXT
