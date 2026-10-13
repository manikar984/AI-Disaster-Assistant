# First deployment checklist — Oracle Cloud Always Free

**Read this before touching the console.** It is the step-by-step, copy-paste
version of `docs/DEPLOYMENT.md`, written for the first real deployment: every
command, every file path, what you should see after it, and the exact point at
which each claim becomes true. `docs/DEPLOYMENT.md` remains the reference runbook
(design rationale, cost table, rollback detail); this file is the thing you follow
line by line.

**Honesty rule for this document.** Nothing below has been executed on a real
Oracle VM. Everything that *can* be verified without a cloud account is, and is
listed as such in §13. Everything else is marked **"verify on the VM"** and stays
unproven until the matching command is run on the host and its output recorded in
`docs/VERIFICATION.md`.

**Where the alerts stand.** Deploying this application does not send anything to
anyone. Alerts go only to accounts that registered, saved a location, opted in,
and confirmed their email address (§10). The staged plan below keeps it that way
until the live feed, the qualification path, HTTPS, the worker and email delivery
have all been proven — do not announce or share the URL before Stage D passes.

---

## 0. The stages, and why they are ordered this way

| Stage | Goal | Gate to pass before the next stage |
|---|---|---|
| **A. Infrastructure** | VM, DNS, TLS, API, worker, database | `deploy/verify-live.sh` passes every check except email (`checks.email = not configured` is expected) |
| **B. Email transport** | Provider accepts and delivers from this host | The `email-check` message arrives in your own mailbox |
| **C. Live ingestion** | A real feed is fetched and stored | `cli sources` shows `usgs … healthy … records>0`, `/api/incidents?mode=live` returns real events |
| **D. One real alert** | The full path incident → qualification → your inbox | A real qualifying event produces a delivery row `accepted` **and** an email in your inbox |
| **E. Public** | Announce the URL, invite users | A–D recorded in `docs/VERIFICATION.md` |

Each stage has a rollback in §11. No stage costs money.

---

## 1. Prerequisites (all of these are yours; none can be done from this sandbox)

| # | What | Notes |
|---|---|---|
| 1 | Oracle Cloud account | Needs a **physical** Visa/Mastercard (Oracle rejects virtual, prepaid and single-use cards); a small temporary authorisation hold appears and is released. Your name and address must match the card statement, and do the signup on home broadband with no VPN. |
| 2 | An SSH key pair on your laptop | `ssh-keygen -t ed25519 -C "oracle-vm"` if you do not have one. The private key never leaves your laptop. |
| 3 | A hostname | A free DuckDNS subdomain works (`disaster-demo.duckdns.org`); a domain you own works the same way. |
| 4 | An SMTP account | Any transactional provider with SMTP (Brevo is the worked example: free tier, 300 messages/day). You will paste the credentials **only** into the VM's environment file, never into chat, git or a ticket. |
| 5 | A terminal with `ssh`, `curl`, `dig`, `scp` | Any Linux/macOS shell, or Windows with OpenSSH and WSL. |
| 6 | About an hour | Stage A is ~30 minutes of waiting for TLS and installs. |

**The one decision at signup you cannot take back:** your **home region**. Always
Free compute exists only in the home region, and Oracle will not move it later.
Pick the closest one that has A1 capacity — `ap-hyderabad-1` or `ap-mumbai-1` for
India. If the console flags a region as short of Always Free capacity, believe it
and pick the other; nearby regions can be worse for you on latency *and* no better
on capacity.

**Capacity is the real risk, not cost.** Many new accounts meet "Out of host
capacity" when launching an A1 instance. Options in order of preference: retry
(the pool frees up constantly, often within hours), try another availability
domain, or — only as a deliberate decision you make with the trade-offs known —
upgrade to Pay-As-You-Go, which removes the hard free cap and therefore can bill
you if anything exceeds the Always Free limits. **Do not upgrade without talking
to me first**; if you do, set a budget alert before creating anything.

**The first thing to do after signup:** Menu → Billing & Cost Management →
**Budgets** → create a budget of `₹0` (or `$0`) on your compartment with a 1 %
alert. It is free, and it is your tripwire for anything that starts costing money.

**What I need from you, ever:** the hostname, the region/shape you provisioned,
and the *outputs* of the commands below. **What you never send me:** the SMTP
password, the SMTP API key, the SSH private key, your Oracle password, or the
DuckDNS token.

---

## 2. Stage A — create the VM (console work)

1. **Create the instance.** Menu → Compute → Instances → *Create instance*.
   * **Name:** `disaster-assistant` (any name).
   * **Image:** Canonical **Ubuntu 24.04 LTS**.
   * **Shape:** *Ampere* → `VM.Standard.A1.Flex`, **2 OCPU / 12 GB**. (The June
     2026 Always Free A1 ceiling; if the console offers 4/24, that is also free
     and fine.) If A1 capacity is unavailable, try another availability domain
     (AD-2/AD-3) or another region — capacity errors are common and temporary.
   * **Boot volume:** 100 GB (within the 200 GB Always Free block-storage
     allowance).
   * **Networking:** *Create new virtual cloud network* + *public subnet*, check
     **Assign a public IPv4 address**. If that toggle is greyed out (a known
     quirk when the VCN does not exist yet), create the VCN first, then create
     the instance against it.
   * **SSH keys:** upload your **public** key (`~/.ssh/id_ed25519.pub`). Never
     enable password SSH.
   * *Create*. Wait for the green **Running** state and copy the **Public IP
     address**.
2. **Reserve the public IP (recommended, $0).** An *ephemeral* public IP can
   change if the instance is recreated or its VNIC is detached, which breaks DNS
   and certificate renewal. Instance → Resources → **Attached VNICs** → click the
   VNIC → **IPv4 Addresses** → the ⋮ menu → *Edit* → **Reserved public IP** →
   *Create new reserved public IP* → Update. Always Free includes reserved IPv4
   addresses at no charge; confirm the current count and price in the console
   before clicking (published sources disagree on the limit). If you prefer to
   skip this, you can fix DNS in seconds later instead — DuckDNS makes that easy.
3. **Open the firewall at the cloud layer.** Instance → *Primary VNIC* →
   **Subnet** → *Default Security List for …* → **Add Ingress Rules**, two rules:

   | Source CIDR | IP protocol | Destination port range | Description |
   |---|---|---|---|
   | `0.0.0.0/0` | TCP | `80` | HTTP — needed for Let's Encrypt and the redirect |
   | `0.0.0.0/0` | TCP | `443` | HTTPS |

   Port **22 is already open** in the default list. If you restricted it, keep it
   restricted to your own IP. Do not open anything else; the application listens
   on loopback only.
4. **Never *Terminate* the instance to save money** — terminating deletes the
   boot volume and with it the database. Use **Stop** (or leave it running; it is
   free).

---

## 3. Stage A — point DNS at the VM

On your laptop:

```sh
# DuckDNS (keep the token out of git and out of chat):
curl "https://www.duckdns.org/update?domains=disaster-demo&token=YOUR_TOKEN&ip=YOUR_VM_PUBLIC_IP"

# Verify — this must print the VM's public IP BEFORE you continue:
dig +short disaster-demo.duckdns.org
```

Expected: the DuckDNS response is `OK`, and `dig` prints exactly the public IP
from §2.1. Let's Encrypt will not issue a certificate until this resolves
correctly, so do not skip the check.

---

## 4. Stage A — first login and firewall check

On your laptop:

```sh
ssh ubuntu@YOUR_VM_PUBLIC_IP          # Ubuntu images use the 'ubuntu' user
```

Expected: a shell prompt like `ubuntu@disaster-assistant:~$`.

Oracle's Ubuntu images ship with a restrictive local `iptables` ruleset that
rejects inbound traffic even when the cloud Security List allows it — the most
common cause of "HTTPS never works". Check it now, before installing anything:

```sh
sudo iptables -L INPUT -n --line-numbers | head -20
```

Read the output: you are looking for a line containing `REJECT` **above** any
`ACCEPT` for ports 80/443. `provision.sh` (next step) installs and enables `ufw`,
which normally takes over the ruleset; if HTTPS still fails later, this is the
first thing to re-check:

```sh
sudo iptables -L INPUT -n --line-numbers      # is a REJECT still above ufw's rules?
sudo ufw status verbose                       # 'Status: active', 22/80/443 allowed
```

Only if external access fails while `curl http://127.0.0.1` works locally, apply
the documented Oracle fix and re-check (this reorders the ruleset without opening
anything beyond 22/80/443):

```sh
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 7 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

---

## 5. Stage A — provision the system

```sh
sudo apt-get update && sudo apt-get install -y git
git clone --branch main https://github.com/manikar984/AI-Disaster-Assistant.git ~/src
cd ~/src
sudo DRY_RUN=1 DISASTER_DOMAIN=disaster-demo.duckdns.org bash deploy/provision.sh   # optional preview
sudo DISASTER_DOMAIN=disaster-demo.duckdns.org bash deploy/provision.sh
```

*(Or run `deploy/provision.sh` straight from the clone; the script reads its own
directory, so either way works.)*

**What it does** (idempotent, safe to re-run, writes no credential):
installs `python3-venv python3-pip git sqlite3 ufw caddy`; creates the
`disaster` service account; creates `/opt/disaster-assistant`,
`/var/lib/disaster-assistant` (0750), `/var/backups/disaster-assistant` (0700),
`/etc/disaster-assistant` (0750) and an empty `/etc/disaster-assistant/env`
(root-owned, `0640`); installs the four systemd units with paths substituted;
writes `/etc/caddy/Caddyfile` for your hostname; allows SSH/80/443 in `ufw`
(**SSH first**, so provisioning cannot lock you out); enables — but does **not**
start — the services.

Expected tail of the output:

```text
Provisioning complete. Next steps (see docs/DEPLOYMENT.md):
  1. Install the application into /opt/disaster-assistant (git clone + venv + frontend build).
  2. Fill in /etc/disaster-assistant/env, including APP_ORIGIN=https://<your-host> and COOKIE_SECURE=true.
  3. Start in order: systemctl start caddy disaster-api disaster-worker disaster-backup.timer
  4. Prove it: curl https://<your-host>/api/ready, then
     /opt/disaster-assistant/.venv/bin/python -m backend.cli sources
     /opt/disaster-assistant/.venv/bin/python -m backend.cli poll --source usgs
     /opt/disaster-assistant/.venv/bin/python -m backend.cli email-check --to <your mailbox>
```

---

## 6. Stage A — install the application where the services expect it

**This is the step that used to be wrong.** The systemd units run the API and the
worker from **`/opt/disaster-assistant`** with `ProtectHome=true`, so a clone in
`/home/ubuntu` would be invisible to them. Install into `/opt`:

```sh
sudo mkdir -p /opt/disaster-assistant
sudo rsync -a --delete --exclude '.git' ~/src/ /opt/disaster-assistant/

cd /opt/disaster-assistant
sudo python3 -m venv .venv
sudo .venv/bin/pip install --upgrade pip
sudo .venv/bin/pip install -r requirements.lock
```

**Frontend build** (the API serves `web/dist`, so it must exist before the API
starts). Either:

```sh
# Option A — build on the VM (needs Node 22, ~1 GB download)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
cd /opt/disaster-assistant
sudo npm ci --prefix web && sudo npm run build --prefix web

# Option B — build on your laptop and copy the result (no Node on the VM)
npm ci --prefix web && npm run build --prefix web
rsync -av --delete web/dist/ ubuntu@YOUR_VM_PUBLIC_IP:/tmp/dist/
ssh ubuntu@YOUR_VM_PUBLIC_IP 'sudo rsync -a --delete /tmp/dist/ /opt/disaster-assistant/web/dist/'
```

Do **not** build with `--base=/AI-Disaster-Assistant/`; that is only for the
GitHub Pages static demo.

Expected: `web/dist/index.html` plus `web/dist/assets/index-*.js` and `index-*.css`
exist:

```sh
ls /opt/disaster-assistant/web/dist /opt/disaster-assistant/web/dist/assets
```

Finally make sure the service account can read everything (it never needs to
write here — the units set `ProtectSystem=strict`):

```sh
sudo chown -R root:disaster /opt/disaster-assistant
# Directories and scripts keep their execute bit, plain files stay read-only:
# the unit calls /opt/disaster-assistant/.venv/bin/uvicorn, so `X` matters.
sudo chmod -R u=rwX,go=rX /opt/disaster-assistant
sudo -u disaster test -x /opt/disaster-assistant/.venv/bin/uvicorn && echo "disaster can execute the venv"
sudo -u disaster test -r /opt/disaster-assistant/web/dist/index.html && echo "disaster can read the built frontend"
```

Expected: both lines print. (`sudo -u <account>` works even though the `disaster`
account has a nologin shell: sudo runs the given command directly rather than the
account's login shell. That pattern is used throughout this checklist, and the
environment-file mechanism behind it — a `0640 root:disaster` file that an
unrelated account cannot read — was verified locally before writing this page.)

---

## 7. Stage A — the environment file (the only place secrets live)

```sh
sudoedit /etc/disaster-assistant/env
```

Start from exactly this, and change the hostname:

```sh
APP_ORIGIN=https://disaster-demo.duckdns.org
COOKIE_SECURE=true
DATABASE_PATH=/var/lib/disaster-assistant/assistant.db

# Data sources. Keep these FALSE for now: Stage C turns USGS on deliberately.
USGS_ENABLED=false
GDACS_ENABLED=false
NEWS_FEEDS=
CAP_FEEDS=

# Email. Leave empty until Stage B.
SMTP_HOST=
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=
SMTP_TIMEOUT=20
```

Rules that matter:

* The file is `0640 root:disaster` — root writes it, the service account reads
  it, nobody else can. Do not "fix" the permissions to make editing easier.
* Never paste this file's contents into chat, a ticket, a screenshot or git. If
  you want to show me a setting, show me the line with the secret blanked out.
* `COOKIE_SECURE=true` is required for HTTPS: without it the browser will not
  send the session cookie over TLS-only cookies rules. `APP_ORIGIN` must match
  the URL in the browser exactly (scheme + host, no trailing slash).
* After any edit: `sudo systemctl restart disaster-api disaster-worker` — systemd
  reads the file at start, so a running process keeps the old values.

Verify the file without printing the secrets:

```sh
sudo systemctl cat disaster-api | grep -i environmentfile     # /etc/disaster-assistant/env
sudo stat -c '%a %U:%G %n' /etc/disaster-assistant/env        # 640 root:disaster
sudo -u disaster sh -c 'grep -c . /etc/disaster-assistant/env' # line count only
```

---

## 8. Stage A — start, watch, and prove TLS

```sh
sudo systemctl daemon-reload
sudo systemctl start caddy
sudo systemctl start disaster-api
sudo systemctl start disaster-worker
sudo systemctl start disaster-backup.timer

systemctl is-active caddy disaster-api disaster-worker     # three times 'active'
systemctl list-timers disaster-backup.timer --no-pager     # next run ~02:30
```

The first API start applies the database migrations (they are versioned SQL in
`backend/migrations/`, applied transactionally; every CLI command also applies
them, so a stray `cli` call can never find a stale schema).

Watch the first boot:

```sh
sudo journalctl -u disaster-api -n 50 --no-pager
```

Expected: `Uvicorn running on http://127.0.0.1:8000`, no traceback, no
`Permission denied`. Watch the worker too — it registers the source registry and
starts its loop:

```sh
sudo journalctl -u disaster-worker -n 30 --no-pager
```

**TLS.** Caddy obtains a Let's Encrypt certificate on first start, which takes
about 10–60 seconds and requires DNS (§3) and port 80 to be reachable:

```sh
sudo journalctl -u caddy -n 50 --no-pager | grep -i -e certificate -e acme -e error
```

From your laptop (not the VM):

```sh
curl -sS https://disaster-demo.duckdns.org/api/health     # {"status":"ok",...}
curl -sSI https://disaster-demo.duckdns.org/ | head -20   # HTTP/2 200, strict-transport-security
```

If the browser-facing URL times out while `curl 127.0.0.1:8000/api/health` works
on the VM, the block is one of two firewalls: re-read §4 (host `iptables`) and
§2.3 (cloud Security List).

---

## 9. Stage A — the release gate

Run it as the service account with the environment file loaded. The
`set -a; . …; set +a` incantation is what turns the file into environment
variables for that one shell; without it the gate runs blind to the SMTP settings
and reports the email check as unconfigured:

```sh
sudo -u disaster bash -c 'set -a; . /etc/disaster-assistant/env; set +a; cd /opt/disaster-assistant && bash deploy/verify-live.sh https://disaster-demo.duckdns.org'
```

Expected (this is the real output shape; the same 12 checks pass in CI against a
live server on every push):

```text
Verifying https://disaster-demo.duckdns.org

PASS  API health
PASS  database reachable
PASS  worker heartbeat is live (age 1s)
INFO  email channel: not configured
INFO  sources: 4 registered, 0 enabled
...
PASS  frontend served by the API
PASS  header Content-Security-Policy present (frame-ancestors 'none')
...
PASS  HTTPS with HSTS header

checks passed: 12, failed: 0
```

`INFO  email channel: not configured` is correct at this stage: Stage B fixes it.
A non-zero exit means the deployment is **not** done — fix the named check first.

**Capture the evidence for the record.** One read-only command reports the host,
capacity, tooling, units, listeners, firewall state, database, both health
endpoints and the DNS/TLS picture — with every secret reported as `<set>` rather
than printed, so the output is safe to paste anywhere:

```sh
sudo bash /opt/disaster-assistant/deploy/collect-facts.sh disaster-demo.duckdns.org
```

Run it at each stage (after provisioning, after the first start, after Stage C and
after Stage D) and record the output in `docs/DEPLOY-SESSION.md`; the distilled
result goes into `docs/VERIFICATION.md`.

---

## 10. Stages B, C, D — email, live feed, one real alert

### Stage B — prove the email transport (5 minutes)

Put your provider's SMTP credentials into `/etc/disaster-assistant/env`
(`sudoedit`, §7), then:

```sh
sudo systemctl restart disaster-api disaster-worker
sudo -u disaster bash -c 'set -a; . /etc/disaster-assistant/env; set +a; cd /opt/disaster-assistant && .venv/bin/python -m backend.cli email-check --to YOUR_OWN_MAILBOX'
```

Expected:

```text
SMTP configuration: host=smtp-relay.brevo.com port=587 security=starttls from=alerts@your-domain (or verified sender) auth=yes
Provider accepted the message (final SMTP reply 250).
Confirm that it arrived at YOUR_OWN_MAILBOX (check the spam folder), then record it in docs/VERIFICATION.md.
```

**Gate:** the message must actually arrive. "Accepted" is the provider's promise,
not delivery. If it does not arrive, check the provider's activity log, SPF/DKIM
for the sending domain, and the spam folder before changing anything.

### Stage C — turn on the live feed (10 minutes)

```sh
sudoedit /etc/disaster-assistant/env      # USGS_ENABLED=true   (leave GDACS false for now)
sudo systemctl restart disaster-worker disaster-api
sudo -u disaster bash -c 'set -a; . /etc/disaster-assistant/env; set +a; cd /opt/disaster-assistant && .venv/bin/python -m backend.cli poll --source usgs'
```

Expected:

```text
usgs    status=ok    fetched=~200 stored=… skipped=… rejected=…  duration=…
```

Then:

```sh
# registry + health, as the service account:
sudo -u disaster bash -c 'set -a; . /etc/disaster-assistant/env; set +a; cd /opt/disaster-assistant && .venv/bin/python -m backend.cli sources'
# usgs  official  healthy  enabled=1 interval=60s records=<n> rejected=<n> last_success=<timestamp> last_error=-

curl -s https://disaster-demo.duckdns.org/api/ready | python3 -m json.tool
# "status": "ready" (or "degraded" naming a real reason), "checks": {"database": true, "worker": "alive", ...}

curl -s 'https://disaster-demo.duckdns.org/api/incidents?mode=live' | python3 -c 'import json,sys; print(len(json.load(sys.stdin)), "live incidents")'
```

The dashboard's feed shows the same events, each with its source, evidence and
verification state. A failed poll is reported, never hidden: paste the real
message into `docs/VERIFICATION.md` and fix the cause (DNS egress, provider
outage, or a changed feed URL).

### Stage D — one real alert in your own inbox (the hard one; be patient)

The qualification rules are in `backend/engine.py` and are strict on purpose.
A notification is created only when **all** of these hold:

| Rule | Value |
|---|---|
| Provenance | `mode` is `live` (never a sample) |
| Freshness | published within the last **6 hours** |
| Location | the event has coordinates |
| Verification | `Corroborated` or `Officially Confirmed` (USGS events are officially confirmed) |
| Your location | within your saved **radius** (1–500 km) |
| Your threshold | event severity ≥ your `min_severity` (magnitude ≥ 4.0 = Medium, ≥ 5.5 = High, ≥ 7.0 = Critical) |
| Email | your address is **confirmed** and the location has email enabled |

Operator steps:

1. Register an account on the site and confirm the address from the email
   (`?verify=` link). Confirm the dashboard shows no "Confirm your email" banner
   afterwards.
2. Add a saved location with a **500 km radius** and **min_severity Medium**
   (locations accept 1–500 km). A wide radius is honest here: it is a real
   subscription over a real area, not a fabricated event.
3. Optional but much faster: add a second location in a genuinely active seismic
   region you care about (e.g. a point in the Japan/Indonesia/South-America
   belts). Events there are frequent, so the alert path proves itself in hours
   rather than weeks. Hyderabad itself is intraplate: a qualifying M≥4.0 within
   6 hours near it can take months.
4. Watch for the qualifying event on the VM:

```sh
sqlite3 /var/lib/disaster-assistant/assistant.db \
  "SELECT d.status,d.attempts,d.sent_at,substr(d.last_error,1,60),n.message FROM deliveries d JOIN notifications n ON n.id=d.notification_id WHERE d.channel='email' ORDER BY d.id DESC LIMIT 5;"
```

Expected after a real qualifying event: one row with `status=accepted`,
`attempts=1`, a `sent_at` timestamp — and the email in your inbox.

**Never manufacture an event to make this pass.** Inserting a fake incident,
lowering the freshness rule, or marking a sample as live would prove nothing and
break the project's central promise. If no event occurs, the honest status stays
"email transport proven (Stage B), alert path not yet observed", and the alert
fires for real users later. (A clearly-labelled *alert template preview* command
could be added to close this gap — it is not implemented today; ask and I will
scope it.)

**Stage E — only now:** share the URL. A–D must be recorded in
`docs/VERIFICATION.md` first.

---

## 11. Operating the deployment

**Update to a new commit**

```sh
cd ~/src
git fetch origin
git log --oneline origin/main -5          # see what is coming
git checkout main && git pull --ff-only   # or: git checkout <specific-sha>

# 1. Snapshot BEFORE anything else: migrations have no down-path.
sudo -u disaster bash -c 'set -a; . /etc/disaster-assistant/env; set +a; cd /opt/disaster-assistant && DATABASE_PATH=/var/lib/disaster-assistant/assistant.db bash deploy/backup.sh'

# 2. Stop the writers, copy the new code in, refresh dependencies.
sudo systemctl stop disaster-api disaster-worker
sudo rsync -a --delete --exclude '.git' --exclude '.venv' --exclude 'web/node_modules' --exclude 'web/dist' ~/src/ /opt/disaster-assistant/
cd /opt/disaster-assistant
sudo .venv/bin/pip install -r requirements.lock
sudo npm ci --prefix web && sudo npm run build --prefix web        # only if web/ changed

# 3. Start and watch: migrations are applied at API start.
sudo systemctl start disaster-api disaster-worker
sudo journalctl -u disaster-api -n 30 --no-pager
sudo -u disaster bash -c 'set -a; . /etc/disaster-assistant/env; set +a; cd /opt/disaster-assistant && bash deploy/verify-live.sh https://<host>'
```

**Restart / stop**

```sh
sudo systemctl restart disaster-api disaster-worker   # after an env change
sudo systemctl stop disaster-api disaster-worker      # before a manual restore
sudo systemctl status disaster-api disaster-worker --no-pager
```

**Logs**

```sh
sudo journalctl -u disaster-api -n 100 --no-pager        # API requests and errors
sudo journalctl -u disaster-worker -n 100 --no-pager     # cycles, source results, deliveries
sudo journalctl -u disaster-worker -f                    # follow live
sudo journalctl -u caddy -n 50 --no-pager                # TLS/ACME and access log location
sudo journalctl --since "1 hour ago" -p err              # everything that went wrong recently
sudo tail -f /var/log/caddy/disaster-access.log          # request log (see Caddyfile)
```

Worker log lines are structured JSON, e.g.
`{"event": "worker_cycle_failed", "error": "OSError"}` — they name a category,
never a secret.

**Health, in this order**

```sh
curl -s https://<host>/api/health | python3 -m json.tool      # process is up
curl -s https://<host>/api/ready  | python3 -m json.tool      # database, worker heartbeat, sources, email
sqlite3 /var/lib/disaster-assistant/assistant.db "SELECT key,substr(value,1,80),updated_at FROM runtime_state;"
```

`checks.worker` must be `alive` with a heartbeat age under 90 seconds. `unknown`
means the worker has never run; `stalled` means it stopped — `systemctl status
disaster-worker` and its journal come next.

**Migrations.** Applied automatically at every API start and by every `cli`
command. There is no separate migrate step, and no down-migrations: rolling back
a schema change means restoring the snapshot you took before the update (§12).

**Rollback**

| What broke | Do this |
|---|---|
| A code change | `cd /opt/disaster-assistant && sudo git -C ~/src checkout <previous-sha>` (in `~/src`), re-rsync as in *Update*, restart the two services. The database is untouched. |
| A bad environment edit | `sudoedit /etc/disaster-assistant/env`, restore the previous values, `systemctl restart disaster-api disaster-worker`. |
| A schema/migration problem | Stop the services, restore the pre-update snapshot (`deploy/restore.sh … --to /var/lib/disaster-assistant/assistant.db --force`, §12), then pin `~/src` to the commit that matches. |
| TLS/DNS | `sudo journalctl -u caddy -n 50`; check §3 DNS and §2.3/§4 firewalls, then `sudo systemctl reload caddy`. |

---

## 12. Storage, backups, and recovery

**What is persistent.** The VM's boot volume holds everything stateful:

| Path | Contents | Survives reboot | Survives instance *terminate* |
|---|---|---|---|
| `/var/lib/disaster-assistant/assistant.db` | **all** state: accounts, sessions, incidents, deliveries, runtime state (SQLite WAL) | yes | **no** — the boot volume is deleted |
| `/var/backups/disaster-assistant/` | nightly snapshots (`0600`, 30-day retention) | yes | **no** |
| `/etc/disaster-assistant/env` | the secrets file | yes | **no** — keep a copy in your password manager |
| `/opt/disaster-assistant` | code + built frontend, rebuildable from git | yes | no (rebuildable) |

So: **Stop, never Terminate**, and keep an off-host encrypted copy of the
snapshots plus the SMTP credentials somewhere else.

**Nightly backup.** `disaster-backup.timer` runs `deploy/backup.sh` every day at
02:30 (+ up to 10 minutes random) using SQLite's online backup API — consistent
even while the API is writing. Check it:

```sh
systemctl list-timers disaster-backup.timer --no-pager
sudo systemctl start disaster-backup.service             # run one now
sudo journalctl -u disaster-backup -n 20 --no-pager      # 'verified snapshot with 15 tables'
sudo ls -l /var/backups/disaster-assistant/
```

**Off-host encrypted backup (your laptop or any storage you already have).** The
private key stays **off the VM**, so a compromised VM cannot decrypt your
backups:

```sh
# once, on your laptop:
age-keygen -o disaster-backup.key          # keep this file safe; print the public key
# once, on the VM (age ships in Ubuntu 24.04's universe repository; gpg works too):
sudo apt-get install -y age

# monthly / weekly, on the VM — encrypt the newest snapshot and pull it:
newest=$(sudo ls -1t /var/backups/disaster-assistant/assistant-*.db | head -1)
sudo age -r age1YOUR_PUBLIC_KEY -o /tmp/$(basename "$newest").age "$newest"
# then from your laptop:
scp ubuntu@YOUR_VM_PUBLIC_IP:/tmp/assistant-*.age ~/backups/
# decrypt only when you need it:
age -d -i disaster-backup.key assistant-20261014T023001Z.db.age > restored.db
```

`rclone` to any object storage you already use works the same way (sync the
`.age` files). If you would rather not install `age`, `gpg --symmetric` with a
passphrase kept in your password manager encrypts just as well for this purpose
(remember the passphrase, or the backups are unreadable).

`rclone` to any object storage you already use works the same way (sync the
`.age` files). Object storage beyond Always Free limits costs money — check the
cost page before enabling it, and tell me if you want me to wire it into the
timer as an automated step.

**Restore drill (do this once before inviting anyone, and after any suspicious
event):**

```sh
sudo -u disaster bash -c 'set -a; . /etc/disaster-assistant/env; set +a; cd /opt/disaster-assistant && bash deploy/restore.sh $(ls -1t /var/backups/disaster-assistant/assistant-*.db | head -1) --to /tmp/restore-drill.db'
# the script verifies the snapshot before writing, refuses to guess a target, and refuses a live target without --force

sudo -u disaster bash -c 'cd /opt/disaster-assistant && DATABASE_PATH=/tmp/restore-drill.db .venv/bin/python -c "
from backend.db import connect
with connect() as db:
    for table in (\"users\",\"incidents\",\"reports\",\"notifications\",\"deliveries\"):
        print(table, db.execute(f\"SELECT count(*) FROM {table}\").fetchone()[0])
"'
sudo rm -f /tmp/restore-drill.db
```

Expected: `Snapshot verified … row counts match the snapshot`, then the counts.
This is the same procedure `tests/test_restore.py` runs end-to-end in CI.

**Real restore (only when the live database is genuinely damaged):**

```sh
sudo systemctl stop disaster-api disaster-worker
sudo -u disaster bash -c 'set -a; . /etc/disaster-assistant/env; set +a; cd /opt/disaster-assistant && bash deploy/restore.sh <snapshot> --to "$DATABASE_PATH" --force'
sudo systemctl start disaster-api disaster-worker
sudo -u disaster bash -c 'set -a; . /etc/disaster-assistant/env; set +a; cd /opt/disaster-assistant && bash deploy/verify-live.sh https://<host>'
```

**Full recovery after losing the VM** (in order): create a new VM (§2) → DNS (§3)
→ firewall check (§4) → provision (§5) → install the app (§6) → **restore the
environment file from your password manager** (§7) → pull the newest `.age`
snapshot from off-host storage, decrypt it, and restore it into
`/var/lib/disaster-assistant/assistant.db` with `deploy/restore.sh` (§12) → start
the services (§8) → run the gate (§9) → re-run Stage B/C proofs. Expect
about 45 minutes, most of it waiting for TLS.

**Retention.** `backend/retention.py` deletes what the documented policy says:
raw source payloads and ingestion runs after 30 days, notification/delivery
history and inactive never-corroborated incidents after 90 days, security/admin
audit after 365 days, expired sessions and single-use tokens immediately. Samples,
corroborated/official incidents, accounts and recent audit rows are never purged.
Preview it any time: `sudo -u disaster bash -c 'set -a; . /etc/disaster-assistant/env; set +a; cd /opt/disaster-assistant && .venv/bin/python -m backend.cli retention --dry-run'`.

---

## 13. Blockers, and what can only be verified on the VM

**Blockers that need you before anything can start**

1. **Create the Oracle account and the VM** (§2). Requires a card for identity
   verification; Always Free resources are not billed. A1 capacity may be
   temporarily unavailable — retry or change AD/region.
2. **Give me the hostname** (§3) and confirm DNS resolves.
3. **Decide on the reserved public IP** (§2.2, $0 per Oracle's current docs,
   verify in the console).
4. **Have the SMTP credentials ready** and place them in the env file yourself
   (§7). Never in chat.
5. **Approve this plan.** Nothing is created or changed until you say go.

**Not blockers, but known and open:** the X/social adapter stays unwired (the
previously public bearer token should still be revoked); rate limiting is still
in-process and needs the edge limiter once real traffic exists; off-host backup
copies are manual until you approve more automation.

**Verified here, in this repository (no VM needed)**

| Behaviour | Evidence |
|---|---|
| `provision.sh`, `backup.sh`, `restore.sh`, `verify-live.sh` parse; provisioning refuses without root or domain; `DRY_RUN=1` is side-effect-free; no script carries a credential | `tests/test_deploy.py` (11 tests) |
| Nightly backup produces a verified snapshot with retention pruning | `tests/test_deploy.py` |
| A snapshot restores into a usable database: integrity, foreign keys, row counts, every relationship, login with the snapshot's password, incident detail, a new write, new ingestion, idempotent migrations, source database byte-identical afterwards | `tests/test_restore.py` (10 tests) |
| Release gate passes against a live server and fails loudly (13 failures, exit 13) against a dead one; CI runs it on every push | `tests/test_deploy.py`, `.github/scripts/browser-smoke.sh` |
| Every API behaviour: auth, sessions, alerts gating, email transport in three modes, retention, headers | 125 tests (`python -m pytest -q`) |
| Browser journey under the real CSP: zero violations, map, filters, register→sign-in, mobile | `web/tests/browser.mjs` via `browser-smoke.sh` |

**Only the VM can verify (each stays unproven until run there)**

| Item | The command that proves it | Where to record it |
|---|---|---|
| `apt`/`ufw`/`systemd`/Caddy install and unit activation | §5, §8 `systemctl is-active` | `docs/VERIFICATION.md` |
| Let's Encrypt certificate issuance, HSTS, HTTPS round trip | §8 `curl -sSI https://<host>/` | same |
| Host firewall + cloud Security List reachability | §4 and §2.3 | same |
| A real USGS feed fetched and stored | §10 Stage C | same |
| Real SMTP delivery to a human inbox | §10 Stage B | same |
| A real qualifying event producing a delivered alert | §10 Stage D | same |
| The nightly timer firing at 02:30 and retention over days | §12 | same |
| Off-host encrypted copy and a restore from it | §12 | same |
| Oracle's idle-reclaim behaviour, A1 capacity, real latency/load | §14 | same |

---

## 14. Free-tier housekeeping

* **Idle reclaim:** Oracle may reclaim Always Free instances that stay under 20 %
  CPU, memory *and* network for seven days. The worker polls and writes
  heartbeats continuously, so the instance is not idle in practice; if you want
  more margin, add a cron line that writes a log line every 15 minutes.
* **Stay inside the allowance:** 200 GB block storage, 10 TB egress/month, 2 OCPU
  / 12 GB on A1 (PAYG accounts keep 4 OCPU / 24 GB). Anything else is billed.
* **Do not upgrade to Pay-As-You-Go** without deciding first: it removes the
  reclaim risk but bills usage beyond the free limits.
* **Patch monthly:** `sudo apt-get update && sudo apt-get upgrade`, then
  `sudo systemctl restart disaster-api disaster-worker`.

---

## 15. Appendix — symptom → cause → fix

| Symptom | Likely cause | Fix |
|---|---|---|
| `https://<host>` times out; `curl 127.0.0.1:8000/api/health` works on the VM | firewall: cloud Security List or host `iptables` | §2.3 and §4 |
| Caddy log shows ACME challenge failures | DNS not resolving yet, or port 80 closed | §3, §2.3 |
| `systemctl status disaster-api` shows `Permission denied` or `No such file` | the app is not in `/opt/disaster-assistant`, or not readable by `disaster` | §6 |
| API starts but `/` returns a 404 with no dashboard | `web/dist` missing (frontend not built) | §6 |
| Browser cannot stay logged in; cookies rejected | `COOKIE_SECURE=true` with `APP_ORIGIN` not matching the exact URL, or plain HTTP | §7 |
| `/api/ready` shows `worker: unknown` | worker never started, or wrong `DATABASE_PATH` | §8, §11 |
| `/api/ready` shows `worker: stalled` | worker crashed in a loop | `journalctl -u disaster-worker -n 100` |
| `/api/ready` shows `checks.email: not configured` | SMTP variables empty | §10 Stage B |
| `email-check` prints `Email is disabled` | env file not loaded by that shell, or variables empty | use the `set -a; . /etc/disaster-assistant/env` form |
| `cli poll --source usgs` fails on the VM but the site is up | outbound egress blocked, or the provider changed its URL | `curl -sI https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson` |
| Emails accepted but never arrive | SPF/DKIM missing for the sending domain, or spam filtering | provider dashboard, then re-test Stage B |
| Snapshot directory empty | the timer has not fired yet | `sudo systemctl start disaster-backup.service` |

---

## 16. Appendix — exact paths and names

| Thing | Value |
|---|---|
| Application (code, venv, built frontend) | `/opt/disaster-assistant` |
| Database (SQLite WAL) | `/var/lib/disaster-assistant/assistant.db` |
| Nightly snapshots | `/var/backups/disaster-assistant/assistant-<UTC stamp>.db` |
| Secrets | `/etc/disaster-assistant/env` (`0640 root:disaster`) |
| Reverse proxy config | `/etc/caddy/Caddyfile` |
| Services | `caddy`, `disaster-api`, `disaster-worker`, `disaster-backup.service`, `disaster-backup.timer` |
| API bind | `127.0.0.1:8000` (loopback only; Caddy is the only public listener) |
| Release gate | `deploy/verify-live.sh <url> [--to <mailbox>]` |
| Evidence collector (read-only, prints no secrets) | `deploy/collect-facts.sh [hostname]` |
| Session record | `docs/DEPLOY-SESSION.md` |
| Backup / restore / provision | `deploy/backup.sh`, `deploy/restore.sh`, `deploy/provision.sh` |
| Operator CLI | `.venv/bin/python -m backend.cli {sources,poll,email-check,retention,admin,reset-password,sample}` |
| Static demo (Pages) | `https://manikar984.github.io/AI-Disaster-Assistant/` — the demo, not the deployment |
