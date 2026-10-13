# Operations, safety and deployment runbook

## Status
This is a tested **single-node pilot**, not a certified emergency warning platform. A local Arena preview was started; no durable public production deployment, external feed fetch, provider delivery, failover test, or HTTPS configuration has been verified. Do not use it as the sole emergency channel.

## External access and cost checklist
* USGS public GeoJSON: no API account/key in this implementation. Requires outbound HTTPS to `earthquake.usgs.gov`; check upstream usage policies. `USGS_ENABLED=true` explicitly enables polling. This sandbox permits only development-package/GitHub hosts, so it remains disabled here.
* Maps: bundled Natural Earth land geometry works offline; optional OSM street tiles contact `tile.openstreetmap.org` from the browser and expose the browser IP to the tile service. Review OSM usage policy and choose a contracted provider for significant traffic.
* SMTP email: requires a verified sender/domain, TLS-capable relay, account and appropriate quotas. Provider charges and reputation controls are operator responsibilities. Set SMTP variables and get consent before testing. `accepted` means accepted by the SMTP server, **not delivered to a person's inbox**. No delivery webhooks implemented.
* X: legacy code can use `X_BEARER_TOKEN`; provider-approved API plan/access may cost money. Not connected to the new worker. No private access claimed.
* Browser push/VAPID, RSS/Atom, CAP, SMS, WhatsApp, LLM: not implemented or enabled; accounts/consent/security review required before implementation.
* Hosting: persistent VM/container host, domain/TLS reverse proxy, backups and monitoring require operator provisioned infrastructure and may incur costs. No cloud resources created here.

## Install and run locally
Python 3.11 and Node 22:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.lock
npm ci --prefix web
npm run build --prefix web
cp .env.example .env
# Edit .env. Plain shell-compatible values only; never commit it.
set -a; . ./.env; set +a
uvicorn backend.app:app --host 0.0.0.0 --port 8000
# In another terminal with the same environment:
python -m backend.worker
```

Build first, then start/restart FastAPI so it detects `web/dist`. Visit `http://localhost:8000`. For hot frontend development, run `npm run dev --prefix web` on 5173 alongside API 8000; Vite proxies `/api` so browsers never need a separate backend URL. Runtime does not automatically load `.env`; export it or use Compose `env_file`.

Fresh databases have **no incidents**. To explicitly load the original historical fixture:

```sh
python -m backend.cli sample
```

Select “Sample data” in the dashboard. It never becomes live and never qualifies alerts. Timestamp is explicitly a fixture placeholder; Kerala/Japan/New York are approximate region/city coordinates. The supplied legacy fixture has no current-event verification.

Create a local administrator without putting passwords in arguments/history:

```sh
python -m backend.cli admin --email admin@example.com
# Operator-assisted password recovery, after independently verifying identity:
python -m backend.cli reset-password --email person@example.com
```

Password changes revoke all sessions. Public registration never grants administrator privileges. Recovery has no public token/email flow yet.

## Container pilot deployment (not executed here)
1. Build with `docker compose build`; create `.env` with production origin, `COOKIE_SECURE=true`, source configuration and provider secrets.
2. `docker compose up -d`. API and exactly one worker share the `assistant-data` volume. SQLite WAL requires a reliable local filesystem, not networked storage. Do not scale replicas.
3. Put an HTTPS reverse proxy on the **same host**, with request body limit 20 KB, read/connect timeouts, per-IP throttling, TLS renewal, HSTS and upstream Host preservation. Compose binds host port to loopback; external traffic must use the proxy.
4. Example Caddy configuration (replace domain):
   ```caddy
   alerts.example.org {
     request_body { max_size 20KB }
     header Strict-Transport-Security "max-age=31536000"
     reverse_proxy 127.0.0.1:8000
   }
   ```
5. Set `APP_ORIGIN=https://alerts.example.org`. Ensure proxy cannot be bypassed. Uvicorn in the image does not trust forwarded IPs. In-process application rate limits therefore aggregate proxy traffic; configure real per-client limits at the edge. Before scaling, replace this with a shared rate limiter.
6. `docker compose exec api python -m backend.cli admin --email YOUR_EMAIL`; provision a strong unique password interactively.
7. Check `/api/health`, `/api/sources`, authenticated `/api/admin/overview`, logs, and real source updates. Use controlled test data in staging (not live fake disasters). Verify SMTP acceptance and actual inbox arrival separately.
8. Test account ownership/deletion, source loss/staleness, worker restart, backup restoration, and real HTTPS cookie behavior before inviting users.

Migrations are ordered SQL in `backend/migrations`; the API applies them transactionally before serving. Back up before every migration. Rollback means restore database + matching code version, not editing migration markers.

## Proving the email channel end-to-end

What the test suite already proves, without any provider account
(`tests/test_email.py`, real TCP + real `smtplib`, no mocks in the delivery
path): a queued delivery becomes an `accepted` row with a `sent_at` timestamp
and one attempt; the MIME message that arrives carries the incident, its
verification state and the "not a government warning" label; STARTTLS is used by
default and is verified against a pinned CA (the client never disables
certificate validation); `AUTH PLAIN` credentials are sent only when
`SMTP_USER` is set; rejected credentials are recorded as a `retry` with the
SMTP reason and the password redacted out of the stored error; a provider outage
escalates `retry` → `failed` after five attempts with 60 s → 480 s backoff; and
`python -m backend.cli email-check` fails closed when SMTP is not configured.

What only the owner can prove is delivery to a human inbox through a real
provider. Procedure on the deployment host:

1. Create an SMTP-enabled account with any transactional provider. The owner
   approved **Brevo** (free tier, 300 messages/day): host `smtp-relay.brevo.com`,
   port 587, security `starttls`, `SMTP_USER` is the account address and
   `SMTP_PASSWORD` is an SMTP key created in the Brevo dashboard. Other providers
   (Resend, SendGrid, Postmark, Mailgun, SES) or a Google Workspace app password
   work identically. Note the host, port and credential — do not paste them into
   chat, a ticket or the repository.
2. Put them in the host's `0600` environment file only (`/etc/disaster-assistant/env`
   on the Oracle VM — see `docs/DEPLOYMENT.md` steps 5 and 7), then restart the
   API and worker so both see it:
   `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` (`starttls` for 587, `ssl` for 465),
   `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` (an address the provider has
   authorised for the domain), then restart the API and worker so both see it.
3. `python -m backend.cli email-check --to YOUR_OWN_MAILBOX` — it prints the
   configuration summary (never the password) and the provider's final reply.
   Expect `Provider accepted the message (final SMTP reply 250)`.
4. Confirm the message actually arrived (check spam). If it did, the channel is
   proven end-to-end: record the date, provider, host and result in
   `docs/VERIFICATION.md`. If it did not, the channel is **configured but not
   proven** — say exactly that.
5. To prove the alert template rather than the test template, subscribe a
   location (My alerts → add a location with email enabled) and lower its
   minimum severity while a real source is enabled. Inspect the queue with
   `sqlite3 "$DATABASE_PATH" "SELECT status,attempts,last_error FROM deliveries WHERE channel='email'"`.
   Never fabricate a disaster to create a test alert: run the sample/staging path
   instead, which by design cannot notify, or wait for a real qualifying event.

Never log or paste `SMTP_PASSWORD`; `MailError` messages are redacted before they
reach a log line, an API response or the `deliveries.last_error` column.

## Notification semantics and recovery
* Current (<6h), corroborated/official, non-sample events only; geographic radius + minimum severity. Critical processing ordered first. No radius represents a validated impact footprint.
* Unique `(user, incident, version)` suppresses duplicates. Noncritical alerts capped at 20/hour/user; critical/corrections bypass cap. Official revised observations increment version. Admin review preserves a timeline and audit.
* Email outbox survives restart and retries five times with exponential delay (60s–16m). Stale/superseded/unconsented queued mail is cancelled. A critical email can't preempt an already-running SMTP operation (20s timeout).
* SMTP is at-least-once: crash after SMTP acceptance but before database commit can cause a duplicate. Stable Message-ID helps but is not an exactly-once guarantee. Multi-worker leasing, dead-letter replay UI, per-channel provider limits, and delivery webhooks remain outstanding.
* Worker polls USGS with exponential failure backoff capped at one hour. Provider-specific Retry-After scheduling is not yet implemented. Bad records are counted/skipped; malformed feed errors are exposed, never replaced by samples. Only a fixed official feed URL is fetched (no user-driven SSRF fetches).
* Use process supervisor/container restart plus external health monitoring. `/api/health` only tests API/database, not worker liveness; stale source timestamps are the current worker-failure signal. Add heartbeat metrics before production.

## Account security operations

* A new account is usable immediately but receives **email** alerts only after it
  confirms its address (24-hour single-use link). In-app notifications are not
  gated. `GET /api/me` returns `email_verified` for the UI banner.
* A user who cannot confirm or has lost their password can use
  "Send a recovery link" (1-hour single-use link, revokes all sessions on use).
  If email is not configured or the mailbox is unreachable, an operator can still
  run `python -m backend.cli reset-password --email <address>` after verifying the
  person's identity out of band.
* "Sign out everywhere" (`POST /api/auth/logout-all`) revokes every session for
  the account, including the current one.
* Tune `SESSION_DAYS` (absolute) and `SESSION_IDLE_DAYS` (inactivity) in the
  environment file; `PUBLIC_BASE_URL` (or `APP_ORIGIN`) must be the public HTTPS
  origin or the emailed links will point at the wrong host.
* Emailed links never contain a password, and token values exist only in the
  email and are stored as hashes. Logs never contain tokens or credentials.
* Known limitation: registration replies 409 for an address that already has an
  account, so address existence is testable — see `docs/ARCHITECTURE.md`.

## Privacy, retention and backups
Minimize collection: email, scrypt password, explicit saved coordinates, consent and delivery history. No background GPS. Private tables are user-scoped. Admin report/audit inspection is privileged. Account deletion removes sessions, locations, notifications and delivery rows; reports lose user linkage/raw payload identifiers. Public report text itself is retained, so warn users not to put personal information there. A moderation/removal process for sensitive report text remains necessary.

**Operator retention policy to enforce before production:** raw source payloads 30 days, ingestion runs 30 days, delivery history 90 days, inactive unverified incidents 90 days, security/admin audit 1 year (subject to legal requirements). No automatic retention purger implemented. Include upstream redistribution/deletion rules in the final policy. Never log passwords, cookies, tokens or complete SMTP errors. Protect logs and database access at the host.

Nightly backup with Python `sqlite3.Connection.backup()` (not naive copying of a live WAL database). Encrypt offsite backups, restrict keys and test restoration monthly. Suggested 30-day backup retention; deleted account data can remain in backups until expiry—document this in deployed privacy policy. Define/test RPO and RTO with the deployment owner; no guarantees measured here.

## Observability
Runs record start/finish, processed count, malformed count and failure class. Reports preserve publication/collection time; incidents preserve detection/update time; notifications and deliveries record creation, attempts and accepted timestamp. Admin API computes mean source latency. Worker failure logs are structured JSON events; server access logs use Uvicorn defaults.

Not yet implemented: exported histograms/SLOs, detection-to-publication measurement, dispatch latency dashboard, false-positive rate, duplicate incident rate, pager escalation, automatic worker heartbeat and calibrated confidence evaluation. Raw timestamps do not substitute for measured SLOs.

## Remaining release gates
PostgreSQL/PostGIS design/migration under load, source-independent corroboration and conflict detection, trained/evaluated NLP, precise affected areas, map clustering at higher volume, provider-verified email delivery (tooling ready, awaiting credentials) and verified recovery flows, push/SMS/WhatsApp providers, admin merge/type correction/moderation, privacy export/retention tooling, account takeover protections/MFA, distributed throttling/queue leases, penetration/accessibility/load tests, disaster recovery rehearsal, and verified public HTTPS deployment.
