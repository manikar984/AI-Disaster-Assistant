# Hosting recommendation

**Decision (owner, 2026-10-13): Oracle Cloud Always Free**, with the email
channel proven by a provider's SMTP credentials placed in the deployment's
environment file. Nothing has been created and nothing has been spent: account
signup, phone/card verification and the VM are the owner's own steps, and the
deployment run starts when the owner says the instance is ready.

The comparison below is kept because it explains *why* that choice fits: it is
the only $0 option that runs an always-on worker with a persistent disk.

## What the application actually needs

| Requirement | Why it is not optional here |
|---|---|
| A real disk or managed database | The incidents, sources, accounts, sessions and delivery log live in SQLite (`DATABASE_PATH`). An ephemeral filesystem loses all of it on every deploy. |
| A process that stays alive | `backend/worker.py` polls sources on their intervals and drains the email outbox. A free tier that sleeps after N minutes of inactivity stops ingestion — the app would then claim freshness it does not have. |
| A public HTTPS origin | Session cookies are `Secure; SameSite=Strict` and the frontend is served from the same origin as `/api`. The release gate is a public HTTPS deployment, not a screenshot. |
| Cost discipline | No recurring spend without your approval; the target is $0 if a serious option exists. |

## Options, as of October 2026

Prices and free-tier limits move; dates below are when the figure was published.
Verify on the provider's own pricing page before committing to anything.

| Option | Always-on worker | Persistent disk | Public HTTPS | Realistic cost | Verdict |
|---|---|---|---|---|---|
| **Oracle Cloud Always Free** (Ampere A1, ARM) | ✅ full VM, systemd services | ✅ 200 GB block storage included | ✅ on the VM (Caddy + Let's Encrypt) | **$0**, no expiry; card used for identity verification only | **Recommended** — the only $0 option that fits this architecture unchanged |
| Fly.io (`bom1` Mumbai) | ✅ one machine per process | ✅ volumes, $0.15/GB-month | ✅ managed | shared-cpu-1x 256 MB ≈ $1.94–2.02/month per machine; API + worker + 1 GB volume ≈ **$4–5/month**; no free allowance for new accounts ($5 trial credit) | Best paid option for India latency (Mumbai region), needs approval |
| DigitalOcean (Bangalore) | ✅ droplet | ✅ disk | ✅ | 512 MB ≈ $4/month, 1 GB ≈ $6/month | Simple and close to Hyderabad; needs approval |
| Hetzner Cloud | ✅ | ✅ | ✅ | CX23 (2 vCPU/4 GB) ≈ €5.99/month EU; ARM CAX11 ≈ €4.49–6.49/month | Cheapest reliable paid VM, but no India region (Singapore/Europe), so latency is higher |
| Render free | ❌ free web services sleep after 15 min and **no free background workers** | ❌ free Postgres is deleted 30 days after creation | ✅ | usable config ≈ $7 web + $7 worker + $6 Postgres ≈ **$20/month** | Rejected: sleeps and expires |
| Railway | ✅ | ✅ | ✅ | Hobby $5/month including $5 usage; no permanent free tier | Viable paid alternative, less control than a VM |
| Koyeb free | ❌ free instance **cannot be a worker service** and scales to zero after 1 h | ❌ no volumes on the free instance | ✅ | $0 tier too small; Pro ≈ $29/month | Rejected: structurally cannot run this app |
| GitHub Pages | ❌ static only | ❌ | ✅ | $0 | Keeps serving the **static demo** only; it cannot be the full deployment |

## Recommendation

**Deploy on Oracle Cloud Always Free** — one `VM.Standard.A1.Flex` instance
(up to 4 OCPU / 24 GB RAM; 2 OCPU / 12 GB is plenty) with 200 GB block storage
in a nearby region (`ap-mumbai-1` Mumbai or `ap-hyderabad-1` Hyderabad both
exist), running `docker compose` from this repository with Caddy in front for
HTTPS.

Why this one:

* **$0 with no expiry** and no sleeping: the worker keeps polling, so source
  freshness means something.
* **The architecture already matches it** — single node, SQLite WAL, one API
  container and one worker container on a persistent volume, exactly what
  `docs/OPERATIONS.md` and `docs/ARCHITECTURE.md` describe. No rewrite, no
  serverless reshaping of the poller.
* **India latency**: Mumbai/Hyderabad regions, and the free tier includes 10 TB
  of monthly egress, which no small-scale deployment will approach.
* **ARM is fine**: the app is pure Python plus static assets; `python:3.12-slim`
  and `caddy` have arm64 images.

Known drawbacks, stated plainly:

* Capacity errors when creating ARM instances are common in busy regions; retry
  or pick another availability domain. If it will not provision, the paid
  fallback below is the answer.
* Oracle may reclaim Always Free compute that looks idle (low CPU, memory **and**
  network for sustained periods). A polling worker plus a small cron heartbeat
  avoids that; the mitigation is in the runbook.
* A card is required to create the account (identity verification). Always Free
  resources are not billed, but I will not create anything there without you.
* One VM means one point of failure, no HA, and you own patching. That matches
  today's single-node design and is documented as a limitation, not hidden.

**Fallback, if Oracle will not provision for you:** Fly.io in `bom1` (Mumbai) at
≈$4–5/month for two small machines plus a volume — closest to Hyderabad of any
managed option and deploys straight from the repo. **Second fallback:**
DigitalOcean Bangalore at ≈$6/month, which many teams find easier to operate.
Both are recurring costs, so they need your approval before I create anything.

**Explicitly rejected:** Render's free tier (sleeps, no free worker, 30-day
database), Koyeb's free tier (no worker service, no volumes), and any
scale-to-zero host, because ingestion would silently stop while the UI still
said "live".

## What the deploy run needs from the owner

1. An Oracle Cloud account with an Always Free `VM.Standard.A1.Flex` instance
   (2 OCPU / 12 GB, 200 GB block storage) reachable over SSH — the owner creates
   it; I never handle the account's credentials.
2. The region the instance actually started in, and either a domain you already
   own or permission to use a free DuckDNS name for Let's Encrypt HTTPS.
3. A provider's SMTP credentials set in `/etc/disaster-assistant/env` on the host
   (free tiers are fine; `docs/DEPLOYMENT.md` step 7 uses Brevo only as the
   worked example). The secret never passes through chat or this repository.
4. Confirmation that the SMTP credentials are in place, so I can run
   `python -m backend.cli email-check --to <mailbox>` and record the proof.

`deploy/` (provisioning script, systemd units, Caddy config, tested backup) and
`docs/DEPLOYMENT.md` are already in the repository, so deployment is a run, not a
rewrite. `bash deploy/verify-live.sh https://<host>` is the release gate that
checks the running deployment before anything is called done.
