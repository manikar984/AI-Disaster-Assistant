# Status board

Last updated 2026-10-13, branch `arena/0aaecd40-ai-disaster-assistant`
(PR [#2](https://github.com/manikar984/AI-Disaster-Assistant/pull/2)).
Every line is backed by a command in `docs/VERIFICATION.md`; anything not
verified there is marked as not claimed.

## Definition of done

| Criterion | Status | Evidence / what is missing |
|---|---|---|
| Live ingestion from a real source | **Implemented, not yet observed live** | USGS, GDACS, RSS/Atom and CAP adapters with per-source health, counters and backoff; verified end-to-end against a local HTTP fixture server (`tests/test_sources.py`, 13 tests). Outbound access to the real hosts is blocked in this sandbox, so each source must be enabled and polled once on the deployment host (`python -m backend.cli poll --source usgs`). |
| One notification channel proven end-to-end | **Half proven** | The email path is proven over a real SMTP conversation (STARTTLS, AUTH, MIME, retries, redaction) and has an operator command that proves a live deployment. Delivery to a human inbox through a commercial provider needs the owner's credentials and is **not claimed**. |
| Public HTTPS deployment | **Prepared, blocked on approval** | `docs/HOSTING.md` compares providers on persistence, worker support, free tier and cost, and recommends Oracle Cloud Always Free. No cloud resource is created without the owner's approval; plan limitation: the deployment script itself is not finalised until the host is chosen. |
| Protection of private data | **Implemented, reviewable** | scrypt password hashing, HttpOnly/SameSite=Strict sessions, per-user row scoping, ownership checks, CSRF origin + header checks, 20 KB body cap, rate limits, no secrets in the frontend bundle, redacted mail errors, documented retention/backup policy. Not measured: penetration test, MFA, distributed throttling. |

## Delivered on this branch

| Commit | What it delivered |
|---|---|
| `9de35bd`–`20d1969` | Diagnosable CI browser smoke, the real root cause of the flaky step fixed (not weakened), static-preview honesty on GitHub Pages, Pages asset-path assertion |
| `b154499` | `docs/AUDIT.md`, `docs/VERIFICATION.md` |
| `b7e5c00` | **Milestone 4 — ingestion breadth**: source registry, USGS/GDACS/RSS/CAP adapters, `/api/ready`, `/api/policy`, per-source health, corroboration rule, CLI `sources`/`poll`, 13 new tests |
| `f414c8e` | Dashboard surfaces source health, readiness, policy and evidence |
| `95fc7ff` | Geographic-precision defect found in the live pilot and fixed (migration 003 + API guard + tests) |
| `0a93cca` | **Email channel provable**: `backend/mailer.py`, `email-check` command, 8 real-SMTP tests, `docs/OPERATIONS.md` proof procedure |
| (this commit) | `docs/HOSTING.md` recommendation, this status board, README/`.env.example` refresh |

Test suite: **66 passed** (`python -m pytest -q`). Browser smokes: full-stack and
static-preview both pass locally and in CI.

## Waiting on the owner

1. **Hosting approval** — Oracle Cloud Always Free (recommended, ₹0) or a paid
   fallback (Hetzner ≈ ₹430/month, Fly.io ≈ $2–4/month). Account creation,
   verification and the credit card are the owner's steps; no credentials or
   card details ever go through chat. See `docs/HOSTING.md`.
2. **SMTP provider + one test mailbox** — the owner creates the account and puts
   the credentials in the host's `0600` environment file, then runs
   `python -m backend.cli email-check --to <mailbox>`; that single message is
   what turns "configured" into "proven end-to-end". See `docs/OPERATIONS.md`.
3. **Decision on the X/social adapter** — it stays unwired until a paid plan is
   approved; the previously public bearer token must be revoked by the owner.

## Next implementable work (no owner input needed)

Ordered by risk closed per unit of effort, from `docs/AUDIT.md` §9:

1. Account security: email verification, single-use expiring recovery tokens
   (SMTP-gated, testable against the SMTP fixture), session list + revoke-all.
2. Content-Security-Policy and `frame-ancestors` headers with a documented
   threat-model update.
3. Retention/backup/restore tooling with a test that restores a database from a
   backup and verifies row counts.
4. Live updates over SSE with reconnect/backoff and a polling fallback, plus
   explicit stale-data indicators in the feed.
5. Administration: incident merge/split, type correction, moderation queue,
   delivery replay — each audited.
6. Frontend: incident routes, editable saved locations, place search, map
   clustering, accessibility and keyboard-navigation tests.

## Explicitly not claimed

No public deployment exists yet, no live feed has been fetched from this
environment, no message has been delivered to a human inbox by a provider, no
SMS/WhatsApp/push channel works, no load, penetration, accessibility or
disaster-recovery test has been run, and nothing here is an official emergency
warning service.
