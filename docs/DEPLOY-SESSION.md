# Deployment session log — first real deployment

This is the running record of the first real deployment: what has actually been
done, the raw output that proves it, and what is waiting on the owner. The
procedure lives in `docs/DEPLOY-CHECKLIST.md`; the design rationale lives in
`docs/DEPLOYMENT.md`. Distilled results get copied into `docs/VERIFICATION.md`
once a stage is proven.

**Rules this log follows**

* A stage is "done" only when real command output on real infrastructure says so.
  Intended, planned or locally-simulated behaviour is recorded as *unverified*.
* No secrets in this file, ever. The fact collector masks them by design; if a
  value ever needs to be discussed, it is referred to as `<set>`.
* Public disaster alerts stay disabled until the live feed, alert qualification,
  HTTPS, worker health and email delivery have each been verified (stages A–D).
* Nothing is created, provisioned or billed without the owner's explicit
  approval. Upgrading the Oracle account to Pay-As-You-Go is such a decision: it
  removes the free-tier hard cap, so it needs a deliberate yes.

---

## Status board

| # | Stage | State | Evidence |
|---|---|---|---|
| 0 | Plan approved; repository prepared | **done** | `docs/DEPLOY-CHECKLIST.md`; commits `e9cf9b4`… |
| 1 | Owner prerequisites: Oracle account, SSH key, hostname, SMTP account | **in progress — the owner's first signup attempt failed; triage in `docs/DEPLOY-CHECKLIST.md` §1, exact error awaited** | this file, §1 record |
| 2 | VM created, networking, reserved IP, DNS record | not started | |
| 3 | Provisioned: packages, service account, units, proxy config | not started | |
| 4 | Application installed in `/opt/disaster-assistant`, frontend built | not started | |
| 5 | Environment file written (no secrets shared) | not started | |
| 6 | Services started; TLS certificate issued; release gate passed | not started | |
| A | Stage A gate: `deploy/verify-live.sh` passes (email may be unconfigured) | not started | |
| B | Email transport proven: `cli email-check` message arrives in the owner's mailbox | not started | |
| C | Live ingestion proven: real USGS events stored and visible | not started | |
| D | One real alert: qualifying event → delivery row `accepted` → email in the inbox | not started | |
| E | Public: URL shared, users invited | not started — gated on A–D | |

Alerts are **off** (`USGS_ENABLED=false`, `GDACS_ENABLED=false`, no feeds) until
stage C, and email-dependent paths stay unexercised until stage B.

---

## How each stage gets verified

| Stage | Command (as run on the host) | What its output proves | Recorded |
|---|---|---|---|
| Any time | `sudo bash deploy/collect-facts.sh <hostname>` | host, capacity, tooling, units, listeners, firewall, env names (no values), database, `/api/health` + `/api/ready`, DNS/TLS | this file, per stage |
| 3–5 | `systemctl is-active caddy disaster-api disaster-worker`, `systemctl list-timers disaster-backup.timer` | the units are installed and running | this file |
| 6 | `sudo journalctl -u caddy -n 50 --no-pager \| grep -i -e certificate -e acme` | Let's Encrypt issued a certificate for the real hostname | this file + `VERIFICATION.md` |
| A | `sudo -u disaster bash -c 'set -a; . /etc/disaster-assistant/env; set +a; cd /opt/disaster-assistant && bash deploy/verify-live.sh https://<host>'` | exit code = failed checks; TLS, headers, health, worker heartbeat | `VERIFICATION.md` |
| B | `… python -m backend.cli email-check --to <owner mailbox>` | the provider accepted the message **and** it arrived in a human inbox | `VERIFICATION.md` |
| C | `… python -m backend.cli poll --source usgs`, then `backend.cli sources` and `GET /api/incidents?mode=live` | a real feed was fetched, stored, and returned by the API | `VERIFICATION.md` |
| D | `sqlite3 …assistant.db "select status,attempts,sent_at from deliveries order by id desc limit 5;"` | the qualification path fired on a real event and email was delivered | `VERIFICATION.md` |
| — | `python -m pytest -q` (here, in the repository) | 133 tests: auth, alerts gating, email, retention, restore, headers, deployment assets | `VERIFICATION.md` |
| — | `pip-audit -r requirements.lock`, `npm audit --prefix web` | no known vulnerabilities in the pinned dependencies | `VERIFICATION.md` |

---

## §1 record — owner prerequisites (stage 0 → 1)

Waiting on the owner. To be filled in as each item is confirmed:

| Item | Value to record | State |
|---|---|---|
| Oracle home region chosen | e.g. `ap-hyderabad-1` | pending |
| Oracle tenancy created | yes/no + welcome email received | **first attempt failed** — the failure point and the verbatim error message are not yet recorded here |
| Always Free instance shape available | A1 visible in console, or capacity error text | pending |
| SSH key generated | `~/.ssh/id_ed25519.pub` fingerprint only | pending |
| Hostname | the DuckDNS subdomain or domain | pending |
| SMTP provider account ready | provider name + verified sender address (never the password) | pending |
| Pay-As-You-Go upgrade | **declined unless the owner says otherwise** | pending |

### Draft: email to Oracle support if signup keeps failing

Send **from the address you signed up with**, attach a screenshot of the error,
and change the bracketed parts. Nothing here is a secret, so it is safe to keep in
this repository.

```text
Subject: Unable to complete Oracle Cloud Free Tier signup - "Error processing
transaction" [<date>]

Hello,

I am unable to create my Oracle Cloud Free Tier account and would like your help.
The signup fails at the payment-verification step with:

    "<paste the exact error text here>"

Attempted on <date and time, with time zone>, from <city, country>, using <browser>.

I have already checked the usual causes:

  * The card is a physical <card brand> <credit/debit> card in my own name, issued
    in India. It is not prepaid, virtual or single-use.
  * International and online/e-commerce transactions are enabled for the card,
    confirmed with the issuing bank.
  * The billing address and name I entered match the card statement exactly,
    including the middle name.
  * No VPN or proxy was used, and browser extensions were disabled in a private
    window.
  * The temporary authorisation hold of <amount> appeared on the card and was
    reversed, so the card itself is working.

Could you please check whether my signup attempt is blocked on your side, and
clear it if so? I will then retry with the same email address
(<the address you signed up with>).

Thank you,
<name>
```

Expect ~24 hours for a first reply; the report from other users is that support
can lift the block and let the same email sign up again.

---

## What the owner sends, and what never leaves their machine

Safe to paste here: region and shape, the hostname, `collect-facts.sh` output
(it contains no secrets by design), command output like `verify-live.sh`,
`sources`, `poll` and `email-check` summaries, and any error text.

Never in chat, git or a ticket: SMTP password or API key, SSH private key, Oracle
account password, DuckDNS token, the contents of `/etc/disaster-assistant/env`,
or anything from `~/.ssh`.

---

## Decisions

| Decision | Value | Status |
|---|---|---|
| Deployment path | **systemd units + Caddy in `deploy/`** — the path `tests/test_deploy.py` covers. The Dockerfile/`compose.yaml` are a development convenience: no TLS proxy, no test coverage | decided (documentation corrected in `e9cf9b4`) |
| Hosting | Oracle Cloud Always Free, `VM.Standard.A1.Flex` 2 OCPU / 12 GB, Ubuntu 24.04, 100 GB boot | decided |
| Reserved public IP | recommended ($0, keeps DNS + ACME stable across instance recreation) | owner to confirm in the console |
| Pay-As-You-Go | **not approved** — it lifts the free hard cap. Only worth revisiting if A1 capacity cannot be obtained at all, and only with the owner's explicit go-ahead | open |
| Email | one free provider's SMTP, credentials only as host environment variables | decided |
| Reverse-proxy TLS | Caddy + Let's Encrypt, HTTP redirected to HTTPS, HSTS | decided |
| Backups | nightly on-host snapshots (30-day retention) + off-host `age`-encrypted copy; restore drill before inviting users | decided |
| Browser push (VAPID) | deferred behind email until stages B–D pass | deferred |

---

## Blockers

1. **Owner:** create the Oracle account (needs a physical Visa/Mastercard for
   verification) and keep the home region in mind — it is permanent and Always
   Free resources exist only there.
2. **Owner:** A1 capacity may be unavailable in the chosen region; if the console
   reports "out of host capacity", record the exact message rather than changing
   the plan.
3. **Owner:** SMTP credentials go into the host environment file, never here.
4. **Open, not blocking:** the X/social adapter stays unwired (revoke the old
   public token); rate limiting is in-process until the edge limiter is proven;
   off-host backup copies are manual until the owner asks for automation.
