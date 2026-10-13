# Verification record

## 2026-10-13 — audit fixes on `arena/0aaecd40-ai-disaster-assistant`

Environment: Python 3.11.2, Node 22.22.3, npm 10.9.8, no Docker, outbound
network restricted to package and GitHub hosts (so no live feed, SMTP or
`github.io` fetch).

| Check | Command | Actual result |
|---|---|---|
| Backend suite | `python -m pytest -q` | **43 passed**, 1 Starlette/httpx deprecation warning |
| Python dependency audit | `pip-audit -r requirements.lock` | No known vulnerabilities |
| npm audit | `npm audit --prefix web` | 0 vulnerabilities |
| Frontend build | `npm run build --prefix web` | Passed (`tsc -b` + Vite; 480.39 kB JS / 39.69 kB CSS) |
| Pages base build | `npm run build --prefix web -- --base=/AI-Disaster-Assistant/` | `index.html` references `/AI-Disaster-Assistant/assets/…`, no root-relative asset URLs |
| Full-stack browser smoke | `bash .github/scripts/browser-smoke.sh` | Passed (desktop feed, map, filters, evidence, auth, saved location, report, deletion, mobile nav, no overflow, no page errors) |
| Static-preview browser smoke | `npm run test:static --prefix web` | Passed (discloses the missing backend, shows no incidents, blocks misleading actions) |
| CI “Checks” | run [38434775564](https://github.com/manikar984/AI-Disaster-Assistant/actions/runs/38434775564) | **All 10 steps pass**, including both browser smokes |

### CI browser failure: root cause and evidence

The reported failure was reproduced from CI evidence instead of guesswork. Raw
job logs and artifacts are unreachable from this network
(`results-receiver.actions.githubusercontent.com`, `*.blob.core.windows.net`
both refuse the connection), so the smoke test was first made diagnosable: it
prints a checkpoint per phase, reports browser channel, page/console errors,
failed requests, layout numbers and a screenshot, and the runner turns the test
log tail into check annotations (readable through `api.github.com`) plus an
uploaded artifact.

Annotations from run `38434082831` identified the fault exactly:

```
error: Search for a sample location returns exactly one row / 5 !== 1
```

The test typed a search term and read `.incident-row` count immediately, racing
the re-fetch: CI measured the unfiltered sample list, the slower local machine
measured the filtered list. The count is now awaited deterministically
(`waitForRowCount`) and the row content is still asserted — the check was fixed,
not weakened. CI has been green on every run since.

### GitHub Pages

Verified locally that the Pages workflow build (`--base=/AI-Disaster-Assistant/`)
emits correct project-path asset URLs and the application build stays at `/`.
The hosted static build has no API — `/api/*` returns a GitHub Pages HTML 404 —
so the frontend now detects the unreachable backend and states it plainly, with
unknown statistics, no sample shortcut and no fabricated records. `github.io`
itself cannot be fetched from this sandbox, so the rendered public page is
**not** independently verified here.

### Still unverified (unchanged)

* Live USGS (or any external feed) fetch: blocked by sandbox egress rules;
  adapter tests use mocked HTTP responses.
* Real SMTP delivery, browser push, SMS and WhatsApp: no credentials or
  providers configured. Only the outbox logic and a fake SMTP interface are
  exercised.
* Docker/Compose build and run: no Docker daemon available.
* No durable public deployment, HTTPS/cookie verification, load test,
  penetration test, accessibility audit or disaster-recovery rehearsal.
* No measured latency, uptime or classification accuracy — none is claimed.

## 2026-10-13 — pilot implementation (previous session, superseded above)

| Check | Actual result |
|---|---|
| `python -m pytest -q` | 43 passed |
| `npm run build --prefix web` | Passed |
| `npm run test:browser --prefix web` | Passed locally |
| `pip-audit -r requirements.lock`, `npm audit --prefix web` | Clean |
| `git diff --check`, Python compile check | Passed |
| Preview HTTP smoke (`/api/health`) | `{"status":"ok","version":"0.1.0"}` |

Backend coverage: ownership of locations/notifications, unauthenticated/admin
access denial, CSRF origin/header checks, registration/logout/login, password
salt verification, input/body validation, duplicate suppression, sample
isolation, geographic matching, freshness/severity/trust gates, official
revision handling, community grouping without trust escalation, aftershock
separation, failed and malformed source handling, audit/review/resolution,
consent withdrawal, retry and mocked SMTP acceptance.
