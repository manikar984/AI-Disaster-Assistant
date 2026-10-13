# Milestone 2 — architecture and contracts

Browser → same-origin React/FastAPI → SQLite WAL. A separate, single worker polls configured public USGS GeoJSON every 60 seconds and processes a durable outbox. No paid API is required for USGS; attribution and source links are retained. Runtime network access is required. No private social-media access is claimed. X, RSS, push, SMS, WhatsApp, LLM and trained NLP adapters are future work, not operational integrations.

SQLite reduces deployment complexity for a **single-node pilot**, not horizontally scaled production. Versioned SQL migrations run at startup. PostgreSQL/PostGIS migration, shared rate limits and queue locking are required before multiple API/worker replicas. This is deliberately a reliable smaller system instead of disconnected integration stubs.

## Contracts
See `/docs` for generated OpenAPI. Public GET `/api/incidents` supports mode, severity, type and search; GET `/api/incidents/{id}` returns evidence and timeline. Frontend polls every 15 seconds; no streaming claim. `/api/sources` exposes actual feed health. Cookie sessions guard `/api/me`, `/api/locations`, `/api/notifications`, `/api/reports`; admin role guards `/api/admin/*`. Mutations require same-origin Origin or absent Origin for non-browser clients. JSON-only body validation prevents form CSRF; logout/deletion additionally require a custom header. Cookies are HTTP-only, SameSite=strict; secure flag on production. Sessions are opaque random tokens stored hashed, expire in 7 days, and are revoked on logout/deletion. Passwords use scrypt with random salts. Recovery requires operator-assisted reset for now; no fake recovery email flow.

## Data model
`users`, `sessions`, `locations` (coordinate, radius, severity and explicit email consent), `sources`, `ingestion_runs`, `reports` (source identity, original ID, timestamps, raw payload, provenance, text, coordinates), `incidents`, `incident_reports`, `timeline`, `notifications`, `deliveries`, `audit`. Foreign keys cascade private account data. Public reports are anonymized when an account is deleted. Unique constraints provide source-record and per-user/incident/version/channel suppression. Retention is enforced by `backend/retention.py` on the schedule the worker drives: raw source payloads and ingestion runs 30 days, notification/delivery history 90 days, inactive never-corroborated incidents 90 days, security and admin audit 365 days. Sample rows, corroborated/officially confirmed incidents, user accounts and recent audit entries are never purged, and a backup taken before a purge still contains what was purged until it expires.

## Detection and trust
Official structured earthquake magnitude + coordinates inform rules. Unstructured reports use transparent word-boundary heuristics with negation and obvious metaphor exclusion, never automatic confirmation. Coordinates are supplied, not guessed from arbitrary text. Close reports of the same type/time/mode can group; official event IDs remain separate to avoid merging aftershocks. Unknown coordinates never plot or qualify for alerts. No calibrated probability or validated ML claim. Evidence grades describe provenance only. Volume never upgrades verification. Admin review may corroborate/dispute/resolve with mandatory justification. This pilot has no automated independent-source corroboration or misinformation model.

Severity is a screening estimate, not a government warning. USGS event confirmation does not imply an official public safety warning. Notifications require current (<6h), non-sample, geographically matching, corroborated or officially confirmed incidents and threshold severity. Resolved/disputed updates can notify previous recipients. No evacuation orders generated. Conservative radius is subscription distance from reported point, **not an impact footprint**.

## Deployment boundary
Single API process + single worker + persistent local volume, behind HTTPS reverse proxy. Admin is provisioned via local CLI, never public registration. SMTP requires an operator-provided verified mail service (costs/provider quotas apply); email consent required. Email integration must be staging-tested before enabling. In-app delivery works without external services. Map uses OpenStreetMap browser tiles (network and tile-policy compliance required); no unsupported area overlays. Production gates and retention in OPERATIONS.md.

## Account security

| Concern | Rule |
|---|---|
| Passwords | scrypt with a per-account salt, verified in constant time; 12–128 characters enforced by the API schema. |
| Sessions | Random 256-bit cookie value; only its SHA-256 hash is stored. `HttpOnly`, `SameSite=Strict`, `Secure` when `COOKIE_SECURE=true`. Absolute lifetime `SESSION_DAYS` (default 30) and inactivity expiry `SESSION_IDLE_DAYS` (default 7); expired rows are deleted when encountered. |
| Session control | `GET /api/sessions` lists this account's sessions (prefix, created, last seen, expiry, current flag) and never returns a token. `POST /api/auth/logout-all` revokes every session. |
| Email confirmation | New accounts start unconfirmed. Confirmation uses a single-use token valid 24 h; in-app notifications work immediately, email deliveries only after confirmation. Accounts that predate the feature are grandfathered (audited). |
| Password recovery | Single-use token valid 1 h, delivered by email; redeeming it sets the new password and revokes every session. `forgot` and `resend-verification` always answer identically so addresses cannot be enumerated; the operator command `python -m backend.cli reset-password` remains for locked-out accounts. |
| Token storage | Only SHA-256 hashes of tokens are stored. Issuing a new token of a kind invalidates the previous one. Expired tokens are pruned opportunistically. |
| Rate limiting | `/api/auth/*` is limited to 10 requests/minute per client in-process; the reverse proxy must add real per-client limits (see the runbook). |

**Known limitation (documented, not hidden).** `POST /api/auth/register` answers
409 when an address already has an account, which lets a caller test whether an
address is registered. Removing that leak means answering 201 and emailing the
existing account holder instead; it is deliberately deferred because the current
behaviour is asserted by existing tests and product copy, and the exposure is
limited to address existence.
