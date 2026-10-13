# Source registry

Every record in this platform comes from a named adapter with a documented
publisher, feed, provenance and mapping policy. Sources are **opt-in**: nothing
is fetched until its environment variable is enabled. A feed failure is shown as
a failure — it never becomes sample data.

## Adapters

| id | Kind | Publisher and feed | Default | Interval | Attribution |
|---|---|---|---|---|---|
| `usgs` | official | [USGS Earthquake Hazards](https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson) — GeoJSON summary feeds | `USGS_ENABLED=false` | 60 s | U.S. Geological Survey |
| `gdacs` | official | [GDACS](https://www.gdacs.org/xml/rss.xml) GeoRSS alerts (EU JRC / UN OCHA) | `GDACS_ENABLED=false` | 300 s | GDACS — European Commission JRC and UN OCHA |
| `news-rss` | news | Operator-listed RSS 2.0 / Atom feeds, one source row per feed | `NEWS_FEEDS` empty | 300 s | The publisher named in each feed pair |
| `cap` | official | Operator-listed [CAP 1.2](https://docs.oasis-open.org/emergency/cap/v1.2/) warning feeds, one row per feed | `CAP_FEEDS` empty | 120 s | The alerting authority named in each feed pair |

Configuration:

```sh
USGS_ENABLED=true                     # then poll once with:
python -m backend.cli poll --source usgs
python -m backend.cli sources         # show every configured source + health
NEWS_FEEDS="Example News|https://news.example/rss.xml,Regional Daily|https://daily.example/feed"
CAP_FEEDS="Test Alerting Authority|https://alerts.example/cap.xml"
```

`<PREFIX>_FEED_URL` (or the pair lists above) are the **only** URLs the platform
will fetch. They are operator-controlled environment values; no user input ever
reaches the fetcher, and redirects are never followed.

## Record contract

Each adapter produces the same normalized record, so the pipeline downstream is
source-agnostic:

```text
source, source_name, source_kind, external_id, url, text, location,
lat, lon, published_at, mode, official,
[category], [magnitude], [severity_hint], [verification_hint],
[dedupe_key], geo_precision, attribution, provider_status
```

* `source_kind` is `official` or `news`; the engine never lets a news or
  community record claim `official`.
* `mode` is `live` for a fresh publication and is downgraded to `delayed` by the
  engine when the record is older than six hours. `sample`/`simulated` records
  can never notify.
* `geo_precision` is one of `point`, `alert-area-centre`, `approximate-region`,
  `unknown`; the UI shows it so a coordinate is never mistaken for a boundary.

## Mapping policies (deliberately conservative)

**Severity.** The source's own severity wins when it publishes one:

| Source | Mapping |
|---|---|
| USGS magnitude | ≥7 Critical · ≥5.5 High · ≥4 Medium · else Low |
| GDACS alert level | Red Critical · Orange High · Green Medium |
| CAP severity | Extreme Critical · Severe High · Moderate Medium · Minor Low |
| Text screening (news, community) | Severe wording → High, minor wording → Low, otherwise Medium |

Wording alone never exceeds High, and **volume never raises severity**.

**Verification.** Separate from severity and confidence:

| Source | State |
|---|---|
| USGS, GDACS structured events | `Officially Confirmed` |
| CAP with certainty `Observed`/`Likely` | `Officially Confirmed` |
| CAP with weaker certainty (`Possible`, `Unlikely`, `Unknown`) | `Developing` |
| News and community reports | `Unverified` until independent evidence exists |

**Corroboration rule** (`backend/verification.py`, also exposed at
`/api/policy`): an incident becomes `Corroborated` when it has **an official
source plus a second evidence domain**, or **three or more distinct evidence
domains**. An "evidence domain" is the host of the record's own URL, so several
feeds relaying one publisher's story never count as independent agreement.
Operator review (`Corroborated`/`Disputed`/`Resolved`) is authoritative and is
never overwritten automatically.

**Grouping.** Reports of the same category within 25 km and one hour attach to
the same incident as supporting evidence; attaching never changes an incident's
severity or verification. Official structured events keep their own identity per
event id, so nearby aftershocks are not merged.

**Syndication.** News records carry a dedupe key (normalized headline + UTC day):
the first copy is stored with its provenance and later copies from other feeds
are counted as skipped, so a wire story cannot masquerade as agreement between
publishers.

**Official instructions.** CAP `instruction` text is stored verbatim and quoted
with attribution. The platform never paraphrases it into new safety advice and
never issues evacuation orders of its own.

## Operating a source

* Health: `status` (`healthy`, `degraded`, `error`, `stale`, `disabled`,
  `pending`), `last_attempt`, `last_success`, `last_error`, `records_total`,
  `records_rejected`, `next_poll` — visible at `/api/sources`, in
  `python -m backend.cli sources` and on the Data sources page.
* `degraded` means the feed was collected but some records were malformed; those
  records are counted and skipped individually, never as a batch failure.
* `stale` means no successful collection within `max(10 minutes, 3 × interval)` —
  the signal to check the worker, which also publishes a heartbeat to
  `/api/ready`.
* Backoff: the worker multiplies the interval by `2^failures`, capped at one
  hour, and resets after a successful poll.

## Verification status of these adapters

The USGS, GDACS, RSS and CAP parsing rules and the full
fetch → normalize → ingest → incident path are covered by tests that run against
a local HTTP fixture server (`tests/test_sources.py`). **Live access to the
external hosts has not been verified in the development sandbox**, which permits
outbound traffic only to package and GitHub hosts. Before enabling a source in
production, poll it once with `python -m backend.cli poll --source <id>` and
confirm the feed parses and the records look right.

Candidate feeds that still need evaluation (availability, licence and
redistribution terms) before any adapter is written: national meteorological and
disaster-management bulletins, tsunami warning centres, and additional CAP
aggregators. None is claimed to work here.
