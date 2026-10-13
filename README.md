# Disaster Assistant

An evidence-first disaster monitoring **pilot**, evolved from the original Streamlit prototype. React/TypeScript frontend, FastAPI backend, SQLite persistence, a USGS polling adapter, and location-based notification qualification.

**Not production-certified. Not a government warning system.** Follow official emergency instructions. Live feeds are opt-in; historical samples are explicitly separated and cannot generate notifications.

## What works in this implementation
- Responsive dashboard with filters, searchable incident feed, offline vector map and optional street tiles.
- Evidence details, provenance labels, timestamps, event timeline and authoritative guidance links.
- Registration, login/logout, hashed-password cookie sessions, private saved locations, consent, alert history and account deletion.
- Normalized durable reports, conservative classification/severity screening, duplicate suppression, nearby community grouping, official event revisions.
- Single-worker USGS polling with backoff, malformed-record isolation and visible source health. Network access needed; live fetching unverified in Arena.
- Geographic/severity/freshness/trust gates, in-app notifications, SMTP outbox with retries and delivery attempt logs. Real SMTP provider delivery unverified.
- Administrator status/severity reviews and audit log, ingestion/delivery inspection.
- Versioned SQL migration, configuration example, container configuration, CI definition and tests.

No validated ML accuracy, calibrated probability, automated independent-source corroboration, browser push, SMS, WhatsApp, live X/RSS/CAP adapter, or public production deployment is claimed. See [limitations and deployment gates](docs/OPERATIONS.md).

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
| `backend/worker.py` | USGS adapter, scheduled ingestion, SMTP outbox |
| `backend/db.py`, `migrations/` | Persistence and versioned SQL schema |
| `backend/cli.py` | Explicit samples, admin provisioning, password reset |
| `web/src/` | React dashboard, map, account and admin workflows |
| `tests/`, `web/tests/` | Backend tests, full-stack browser smoke, static-preview smoke |
| `.github/scripts/` | CI smoke runner with annotation diagnostics |
| `docs/AUDIT.md` | Actual repository findings and implementation plan |
| `docs/ARCHITECTURE.md` | Contracts, trust model and technology decisions |
| `docs/OPERATIONS.md` | Setup, deployment, security, costs, retention, remaining work |

## GitHub Pages

The Pages workflow publishes a **static demonstration build** (project-path assets,
no API). It cannot sign anyone in or show live incidents, and it now says so on
the page instead of rendering an unexplained empty dashboard. The full
application requires a host that runs the API and the worker — see
[docs/OPERATIONS.md](docs/OPERATIONS.md).

## Preserved legacy prototype
`prototype.py`, `twitter_api.py`, `disaster_data.json`, `disaster_map.html` and the original presentation remain. The prototype is a separate legacy demonstration, not the new service. Install `requirements-legacy.txt` if you need to run it. It was inspected, but not runtime-verified here. Fixture overwrites, hardcoded credential, unsafe popup interpolation and misleading default coordinates were corrected.

**Security action for the repository owner:** revoke the apparent X token previously embedded in `prototype.py`. It has been removed from the working tree, but remains in Git history; assess history cleanup after rotation.
