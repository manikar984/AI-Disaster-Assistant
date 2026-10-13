# Disaster Assistant

An evidence-first disaster monitoring **pilot**, evolved from the original Streamlit prototype. React/TypeScript frontend, FastAPI backend, SQLite persistence, a USGS polling adapter, and location-based notification qualification.

**Not production-certified. Not a government warning system.** Follow official emergency instructions. Live feeds are opt-in; historical samples are explicitly separated and cannot generate notifications.

## What works in this implementation
- Responsive dashboard with filters, searchable incident feed, offline vector map and optional street tiles.
- Evidence details, provenance labels, timestamps, event timeline and authoritative guidance links.
- Registration, login/logout, hashed-password cookie sessions, private saved locations, consent, alert history and account deletion.
- Normalized durable reports, conservative classification/severity screening, duplicate suppression, nearby community grouping, official event revisions.
- Single-worker USGS polling with backoff, malformed-record isolation and visible source health. Network access needed; live fetching unverified in Arena.
- Accounts with confirmed email addresses: a new account gets in-app alerts immediately and email alerts only after it confirms its address with a single-use link; password recovery uses a 1-hour single-use link that signs out every device, and sessions can be listed and revoked (`SESSION_DAYS`, `SESSION_IDLE_DAYS`).
- Geographic/severity/freshness/trust gates, in-app notifications, and an SMTP outbox with retries and delivery-attempt logs. The SMTP path is exercised against a real server in the test suite; delivery through a commercial provider is unproven, and the first deployment is paused (see below).
- Administrator status/severity reviews and audit log, ingestion/delivery inspection.
- Versioned SQL migration, configuration example, container configuration, CI definition and tests.

No validated ML accuracy, calibrated probability, automated independent-source corroboration, browser push, SMS, WhatsApp, live X/RSS/CAP adapter, or public production deployment is claimed. See [limitations and deployment gates](docs/OPERATIONS.md).

The first deployment to Oracle Cloud Always Free is **paused by the owner's decision**: signup requires card verification that could not be completed, and paid services are not an option. Nothing was provisioned or billed, no live source is enabled, and no public alerts exist. The deployment preparation is finished and kept ready — procedure in [docs/DEPLOY-CHECKLIST.md](docs/DEPLOY-CHECKLIST.md), state in [docs/STATUS.md](docs/STATUS.md).

## Quick start

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
npm ci --prefix web
npm run build --prefix web
uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

Visit `http://localhost:8000`. API docs: `/docs`. Fresh installs contain no fake live events.

```sh
# Optional, explicit historical sample import:
python -m backend.cli sample
# Enable USGS only on a host that can reach the public feed:
USGS_ENABLED=true python -m backend.worker
# Administrator setup (password entered privately):
python -m backend.cli admin --email admin@example.com
# Prove the email channel once SMTP_* is configured (sends one labelled test message):
python -m backend.cli email-check --to you@example.org
# See what the retention policy would delete (add nothing to apply it; the worker runs it daily):
python -m backend.cli retention --dry-run
```

Set the same `USGS_ENABLED` value for API and worker. See `.env.example` and the runbook for environment loading, production cookies, SMTP and Compose. Never commit `.env`.

## Verification

```sh
python -m pytest -q
npm run build --prefix web
npm audit --prefix web
# Static-preview smoke (built frontend served with no API, as on GitHub Pages):
npm run test:static --prefix web
# Optional dependency audit:
pip install pip-audit
pip-audit -r requirements.lock
# Browser smoke: first start a built server on 8001 with a disposable DB and explicit samples.
npm run test:browser --prefix web
```

See [actual verification results](docs/VERIFICATION.md), not just these commands.

## Repository guide

| Path | Purpose |
|---|---|
| `backend/app.py`, `security.py` | API, sessions, input/ownership boundaries |
| `backend/engine.py` | Screening, grouping, geo matching, qualification |
| `backend/worker.py` | Polling worker: per-source schedule with backoff, heartbeat, delivery queue |
| `backend/poller.py`, `backend/sources/` | Source registry and the USGS, GDACS, RSS/Atom and CAP adapters |
| `backend/verification.py`, `backend/mailer.py` | Corroboration rule; SMTP transport for the email channel |
| `backend/db.py`, `migrations/` | Persistence and versioned SQL schema |
| `backend/cli.py` | Explicit samples, admin provisioning, password reset |
| `web/src/` | React dashboard, map, account and admin workflows |
| `tests/`, `web/tests/` | Backend tests, full-stack browser smoke, static-preview smoke |
| `.github/scripts/` | CI smoke runner with annotation diagnostics |
| `docs/AUDIT.md` | Actual repository findings and implementation plan |
| `docs/ARCHITECTURE.md` | Contracts, trust model and technology decisions |
| `docs/OPERATIONS.md` | Setup, deployment, security, costs, retention, remaining work |
| `docs/HOSTING.md` | Hosting options compared on worker/persistence/HTTPS, with the recommendation |
| `docs/SOURCES.md` | Every adapter, its publisher, mapping policy and health states |
| `docs/HOSTING.md` | Hosting comparison and the decision (Oracle Always Free) |
| `docs/DEPLOYMENT.md`, `deploy/` | Runbook, systemd units, Caddy config, provisioning and backup scripts |

## GitHub Pages

The Pages workflow publishes a **static demonstration build** (project-path assets,
no API). It cannot sign anyone in or show live incidents, and it now says so on
the page instead of rendering an unexplained empty dashboard. The full
application requires a host that runs the API and the worker — see
[docs/OPERATIONS.md](docs/OPERATIONS.md).

## Preserved legacy prototype
`prototype.py`, `twitter_api.py`, `disaster_data.json`, `disaster_map.html` and the original presentation remain. The prototype is a separate legacy demonstration, not the new service. Install `requirements-legacy.txt` if you need to run it. It was inspected, but not runtime-verified here. Fixture overwrites, hardcoded credential, unsafe popup interpolation and misleading default coordinates were corrected.

**Security action for the repository owner:** revoke the apparent X token previously embedded in `prototype.py`. It has been removed from the working tree, but remains in Git history; assess history cleanup after rotation.
