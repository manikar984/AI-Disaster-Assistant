# Deployment runbook — Oracle Cloud Always Free

Target: one always-on Ubuntu 24.04 VM from Oracle's Always Free tier (Ampere A1,
2 OCPU / 12 GB — the owner-approved host, see `docs/HOSTING.md`). One machine runs
the API, the ingestion worker and Caddy; SQLite lives on the VM's block storage.

```text
browser ──HTTPS──▶ Caddy :443 ──▶ 127.0.0.1:8000 uvicorn backend.app:app
                                    └── worker thread-free process: systemd disaster-worker
                                    └── /var/lib/disaster-assistant/assistant.db   (SQLite, WAL)
                                    └── /var/backups/disaster-assistant/           (nightly snapshots)
```

**Honesty note:** this runbook has been written from the verified code path but
has **not** been executed against a real Oracle account from this environment
(no cloud account, no systemd, outbound egress restricted). The parts that can be
tested here are tested — `tests/test_deploy.py` checks that every script parses,
that the backup script produces a verified snapshot with retention pruning, and
that the units and proxy config keep the invariants below. Treat steps 1–2 as
yours and steps 3–9 as "run it and tell me what breaks".

## 0. What the owner does (I never ask for credentials here)

1. Create the Oracle Cloud account. Always Free requires a card for identity
   verification; Always Free resources are not billed.
2. A DNS name. A free DuckDNS subdomain is enough, e.g. `disaster-demo.duckdns.org`
   → point it at the VM's public IP. A domain you own works the same way.
3. An SMTP account for alerts — `docs/OPERATIONS.md` and step 7 below use Brevo;
   any provider works.

## 1. Create the VM

* **Shape:** `VM.Standard.A1.Flex`, 2 OCPU / 12 GB (Always Free ceiling since
  June 2026). If A1 capacity is unavailable, a second choice is 2× AMD
  `VM.Standard.E2.1.Micro`, but 1 GB RAM each is tight — prefer retrying A1 in
  another availability domain.
* **Image:** Ubuntu 24.04 LTS. **Region:** one close to your users (Hyderabad for
  the owner's location, else Mumbai/Singapore).
* **Storage:** 100 GB boot volume (within the 200 GB Always Free allowance).
* **SSH key:** upload your public key; disable password SSH.
* **Security list / NSG ingress:** `22` from your own IP only, `80` and `443`
  from `0.0.0.0/0`. Nothing else is open.
* Note the public IP. `ufw` is configured by the provisioning script as a second
  layer.

## 2. Point DNS at the VM

```sh
# DuckDNS example (token comes from your DuckDNS account, keep it out of git):
curl "https://www.duckdns.org/update?domains=disaster-demo&token=YOUR_TOKEN&ip="
dig +short disaster-demo.duckdns.org     # must print the VM's public IP before step 5
```

Let's Encrypt issuance in step 5 needs DNS to resolve *before* Caddy starts.

## 3. Install the application

```sh
ssh ubuntu@<vm-ip>
sudo apt-get update && sudo apt-get install -y git

git clone --branch main https://github.com/manikar984/AI-Disaster-Assistant.git
cd AI-Disaster-Assistant
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.lock
```

**Frontend build.** The API serves `web/dist`, so it must exist before start:

```sh
# Option A (recommended): Node 22 from NodeSource, then build on the VM
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
npm ci --prefix web && npm run build --prefix web

# Option B: build locally and copy the artefacts (no Node on the VM)
rsync -av --delete web/dist/ ubuntu@<vm-ip>:~/AI-Disaster-Assistant/web/dist/
```

Do **not** build with `--base=/AI-Disaster-Assistant/`; that is only for the
GitHub Pages demo. The application build stays at the root path.

## 4. Provision the system (packages, service account, units, proxy)

```sh
sudo DISASTER_DOMAIN=disaster-demo.duckdns.org bash deploy/provision.sh
```

This is idempotent. It installs Caddy/sqlite/ufw, creates a `disaster` service
account, makes `/var/lib/disaster-assistant` and
`/var/backups/disaster-assistant`, installs the four systemd units and writes
`/etc/caddy/Caddyfile` for your hostname. It enables the services but does not
start them.

## 5. Fill in the environment file

`/etc/disaster-assistant/env`, root-owned mode 0640, is the only place secrets
live. Minimum content:

```sh
APP_ORIGIN=https://disaster-demo.duckdns.org
COOKIE_SECURE=true
DATABASE_PATH=/var/lib/disaster-assistant/assistant.db

# Data sources — official feeds stay off until you enable them deliberately.
USGS_ENABLED=true
GDACS_ENABLED=true
# Operator feeds, comma-separated as Name|https://url (empty = none configured).
NEWS_FEEDS=
CAP_FEEDS=

# Email (see step 7)
SMTP_HOST=
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=
```

Edit it with `sudoedit /etc/disaster-assistant/env`. Never commit it, never paste
it into chat, a ticket or a PR.

## 6. Start and watch

```sh
sudo systemctl daemon-reload
sudo systemctl start caddy disaster-api disaster-worker disaster-backup.timer
systemctl status caddy disaster-api disaster-worker --no-pager
journalctl -u disaster-api -f      # first startup applies migrations
```

## 7. Prove it works (this is the release gate, not a formality)

From the host, with the environment loaded so the script can also run the email
proof:

```sh
cd ~/AI-Disaster-Assistant
set -a; . /etc/disaster-assistant/env; set +a
bash deploy/verify-live.sh https://disaster-demo.duckdns.org --to your-own-mailbox
```

The gate fails (non-zero exit) unless the API answers, the database is reachable,
the worker heartbeat is live, no source is in `error`, the policy is published,
the frontend is served, and — on HTTPS — HSTS is present. The same script runs in
CI against the smoke server on every push, so the checks cannot rot.

The same checks by hand:

```sh
curl -s https://disaster-demo.duckdns.org/api/health      # {"status":"ok",...}
curl -s https://disaster-demo.duckdns.org/api/ready       # status/worker/sources/email

.venv/bin/python -m backend.cli sources                   # registry + health
.venv/bin/python -m backend.cli poll --source usgs        # fetch a real feed ONCE
.venv/bin/python -m backend.cli sources                   # expect usgs: healthy, records>0
```

* `/api/ready` must report `worker: alive`; `checks.email` is `configured` once
  step 7's SMTP variables are in the environment file. `degraded` with
  `"no live source is enabled"` is the honest state until step 5 enables a feed.
* A `poll` that fails is a failure to report, not to hide: put the message in
  `docs/VERIFICATION.md` as-is.
* Register a throwaway account over HTTPS and confirm in the browser devtools
  that the session cookie is `Secure; HttpOnly; SameSite=Strict`.
* Confirm the sample path is still explicit: fresh installs show no incidents
  until `python -m backend.cli sample` is run by an operator.

**Email (Brevo example).** In the Brevo dashboard: create an SMTP key, then set
`SMTP_HOST=smtp-relay.brevo.com`, `SMTP_PORT=587`, `SMTP_SECURITY=starttls`,
`SMTP_USER=<account email>`, `SMTP_PASSWORD=<SMTP key>`, `SMTP_FROM=<verified
sender>`. Add SPF/DKIM records for your domain before using it as the sender.
Then:

```sh
sudo systemctl restart disaster-worker disaster-api
.venv/bin/python -m backend.cli email-check --to your-own-mailbox
```

The command prints the configuration summary (never the password) and the
provider's reply. **The channel counts as proven only when the message arrives
in a human inbox** — record the date and provider in `docs/VERIFICATION.md`.

## 8. Backups and a restore drill

```sh
sudo -u disaster DATABASE_PATH=/var/lib/disaster-assistant/assistant.db bash deploy/backup.sh
ls -l /var/backups/disaster-assistant/            # 0600 snapshots, 30-day retention
```

The timer runs it nightly at 02:30 with a random delay. **Drill the restore
before inviting anyone** — and rehearse it with a throwaway target, not the live
file:

```sh
cd ~/AI-Disaster-Assistant
sudo -u disaster DATABASE_PATH=/var/lib/disaster-assistant/assistant.db bash deploy/restore.sh \
    /var/backups/disaster-assistant/assistant-<stamp>.db --to /tmp/restore-drill.db

# The restored file is a normal database: point the app at it and look around.
sudo -u disaster DATABASE_PATH=/tmp/restore-drill.db .venv/bin/python -m backend.cli sources
sudo -u disaster DATABASE_PATH=/tmp/restore-drill.db .venv/bin/python -c \
  "from backend.db import connect
with connect() as db:
    print([r[0] for r in db.execute('SELECT count(*) FROM incidents')])"
```

`deploy/restore.sh` verifies the snapshot (integrity, foreign keys, required
tables, row counts) **before** writing anything, refuses to guess a destination,
refuses an existing target or the live database without `--force`, and moves the
restored copy into place atomically. `tests/test_restore.py` performs this whole
drill against isolated data on every CI run, including driving the API and the
ingestion engine against the restored file.

Only if the live database is genuinely damaged:

```sh
sudo systemctl stop disaster-api disaster-worker
sudo -u disaster DATABASE_PATH=/var/lib/disaster-assistant/assistant.db bash deploy/restore.sh \
    /var/backups/disaster-assistant/assistant-<stamp>.db \
    --to /var/lib/disaster-assistant/assistant.db --force
sudo systemctl start disaster-api disaster-worker
curl -s https://<your-domain>/api/ready
```

Copy snapshots off the machine (encrypted) as soon as you consider the data real.

## 9. Free-tier housekeeping

* **Idle reclamation.** Oracle may reclaim Always Free instances that stay under
  20 % CPU, memory *and* network for seven days. The worker polls on schedule and
  writes heartbeats, so the instance is never truly idle; if you want a wider
  margin, add a small cron entry that touches the database and writes a log line
  every 15 minutes.
* **Stay inside the allowance:** 200 GB block storage, 10 TB egress/month, 2 OCPU
  / 12 GB on A1. Anything extra is billed — check the cost page before resizing.
* **Upgrades:** a Pay-As-You-Go upgrade returns the 4 OCPU / 24 GB allowance and
  removes the reclaim risk, but it also means usage above the free limits is
  billed instead of blocked. Do not do that without approving it first.
* Patch monthly: `sudo apt-get update && sudo apt-get upgrade`, then restart the
  services.

## 10. Rollback

* **Code:** `git log --oneline`, `git checkout <previous-sha>`,
  `sudo systemctl restart disaster-api disaster-worker`.
* **Schema:** migrations are append-only and applied transactionally; rolling
  back means restoring the matching database snapshot, not editing migration
  markers. Take a fresh backup (`deploy/backup.sh`) before any migration.
* **DNS/TLS:** if certificate issuance fails, check that DNS resolves to the VM
  and that port 80/443 are open; `journalctl -u caddy` shows the ACME error.

## 11. Cost summary

| Item | Cost |
|---|---|
| Ampere A1 2 OCPU / 12 GB, always on | ₹0 (Always Free) |
| 100 GB boot volume (≤200 GB allowance) | ₹0 |
| Outbound ≤10 TB/month | ₹0 |
| DuckDNS subdomain | ₹0 |
| Let's Encrypt certificate | ₹0 |
| Brevo email (300/day free tier) | ₹0 |

Anything beyond these limits, any second VM, or a PAYG upgrade is billed and
needs your explicit approval.
