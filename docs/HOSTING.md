# Hosting options and recommendation

## Decision (2026-10-13)

The owner chose **Oracle Cloud Always Free**, and **Brevo** for the email
channel. Deployment is prepared in `deploy/` and `docs/DEPLOYMENT.md`: systemd
units, a Caddy reverse proxy with automatic HTTPS, a system-provisioning script
and a tested nightly SQLite backup. Nothing has been created in Oracle yet — the
account, the VM and the mail account are the owner's steps, and the runbook
instructs exactly what to run.

**Status: recommendation only — nothing has been created, and no cost has been
incurred.** Items marked *(verify at signup)* are published prices that must be
re-confirmed on the provider's checkout page before committing.

## What this application needs from a host

| Requirement | Why | Consequence of getting it wrong |
|---|---|---|
| **Persistent disk or managed DB** | The app stores incidents, sources, users, sessions and notification history in SQLite (`DATABASE_PATH`). | On an ephemeral filesystem every user, saved location and incident disappears on each deploy or restart. |
| **A long-lived process** | `backend/worker.py` polls enabled sources on per-source intervals (USGS every 60 s) and sends email. It is a thread inside the web process and must keep running when nobody is browsing. | On a scale-to-zero host the poller only runs while a visitor is present, so ingestion stops and "live" data silently goes stale — exactly what the project forbids. |
| **Public HTTPS** | The definition of done requires a public HTTPS deployment; sessions use `Secure`/`SameSite` cookies and the frontend is served by the same origin as `/api`. | No HTTPS means no secure session cookies and no honest "deployed" claim. |
| **No surprise cost** | The project rule is that no money is spent without explicit approval. | A card on a metered plan can start billing the moment a limit is crossed. |

GitHub Pages keeps hosting the **static demo only** (it has no backend), so it
does not satisfy this requirement and is not a deployment target.

## Comparison

Free allowance / price figures as published in October 2026 *(verify at signup)*.

| Option | Compute | Storage | Always on? | Worker/poller | Cost | Main catch |
|---|---|---|---|---|---|---|
| **Oracle Cloud Always Free** (Ampere A1) | 2 OCPU / 12 GB RAM, always-on VM | 200 GB block storage, 10 TB egress/mo | ✅ | ✅ full VM, runs `uvicorn` + worker under systemd | **$0**, no expiry | Signup is frequently rejected/out of capacity; instances that stay under 20 % CPU, memory **and** network for 7 days may be reclaimed; allowance was halved from 4 OCPU/24 GB in June 2026; card required |
| **Hetzner Cloud CX22** | 2 vCPU / 4 GB, always-on | 40 GB NVMe | ✅ | ✅ | **€4.51/mo ≈ ₹430** | Card + sometimes ID verification; nearest region is Singapore (~60 ms from Hyderabad) |
| **Fly.io** | shared-cpu-1x 256–512 MB, auto-stop optional | Persistent volume **$0.15/GB-mo** | ✅ (configured) | ✅ one machine runs web + worker | **~$2–4/mo** + volume | No free tier for new accounts (legacy only); pay-as-you-go from the first second; **Mumbai (`bom`) region**) |
| **Railway** | Hobby plan | Volume available | ✅ | ✅ | **$5/mo** (includes $5 usage) | Usage that exceeds the included $5 bills the card; no permanent free tier |
| **Render** | Free web service | ❌ no free disk; free Postgres **expires after 30 days** | ❌ sleeps after 15 min idle | ❌ **no free background workers** ($7/mo) | $7 web + $7 worker + $6 DB ≈ **$20/mo** to be usable | Free tier cannot run this app honestly |
| **Koyeb** | 1 free web service, 0.1 vCPU / 512 MB | ❌ no volumes on free; Postgres 5 compute-h/mo | ❌ scales to zero after 1 h idle | ❌ "cannot be a worker service" | $29/mo Pro after the free tier | Unsuitable |
| **Google Cloud** (e2-micro, Always Free) | 1 e2-micro / 1 GB | 30 GB standard disk | ✅ | ✅ | IPv4 on a running VM is **billed ≈ $3.65/mo** *(verify)*; only 1 GB egress/mo | 1 GB/month egress is exhausted by a few thousand page views, and the free tier is not actually $0 with an IPv4 |

## Recommendation

**Primary: Oracle Cloud Always Free, Ampere A1 (2 OCPU / 12 GB), region
`ap-hyderabad-1`** *(verify capacity and current limits at signup)*.

* Cost: ₹0 and no expiry — the only option that is both free and genuinely
  always-on with a persistent disk.
* Everything this app needs is native to a plain VM: SQLite on local disk,
  `uvicorn` + the polling worker in one systemd unit, Caddy for automatic
  Let's Encrypt HTTPS, `USGS_ENABLED=true` for the first live source.
* Latency from Hyderabad is the best of any option (local region).
* Its two real risks have known mitigations:
  * **Idle reclamation** — the worker polls continuously, but to keep the
    instance demonstrably active I will add a small cron job that does real
    work (a health check plus a periodic log heartbeat) so the instance never
    looks idle. If it is ever reclaimed, the deployment is one command to
    rebuild, and the setup steps will be checked in.
  * **Account/quota friction** — if the Ampere shape is unavailable, the same
    Terraform-free setup runs on the two tiny AMD micro VMs, or we fall back.

**Fallback if Oracle will not provision: Hetzner CX22 in Singapore (~₹430/mo)
or Fly.io in Mumbai (~$2–4/mo).** Both are cheap and robust, but both cost
money, so they need your explicit approval first.

**Not recommended:** Render, Koyeb and any scale-to-zero platform, because the
poller stops when the app sleeps — that would make ingestion unreliable while
still calling the data "live".

## Deployment shape (one VM, no orchestrator)

```text
Caddy (443, automatic HTTPS) ──▶ 127.0.0.1:8000 uvicorn backend.app:app
                                   └── in-process worker thread (poller + email)
                                   └── /var/lib/disaster-assistant/assistant.db   (SQLite)
                                   └── /etc/disaster-assistant/env                (0600 env file)
```

* HTTPS hostname: a free DuckDNS/nip.io name pointed at the VM's public IP keeps
  the deployment zero-cost and gives a real Let's Encrypt certificate. A domain
  you already own can be substituted at any time.
* Secrets live only in the `0600` env file (never in git, never in the build).
* Backups: a nightly `sqlite3 .backup` copy plus the existing run/audit tables;
  the free tier's 200 GB block storage is far more than needed.
* This same shape works unchanged on Hetzner/Fly/GCP, so choosing a different
  provider later costs one provision step, not a rewrite.

## What I need from you

1. **Approve Oracle Cloud Always Free** (free, but it does require a card at
   signup and account creation needs a phone/e-mail verification you must do
   yourself — I will never ask for those credentials in chat), **or** approve one
   of the paid fallbacks (Hetzner ≈ ₹430/mo, Fly.io ≈ $2–4/mo).
2. If you pick Oracle, tell me the region you actually got capacity in, and
   whether I should use a DuckDNS name or a domain you own.

Until then I will keep the code deployable-as-is (Dockerfile, Compose file and
the env contract already in the repo) and will not create any cloud resource.
