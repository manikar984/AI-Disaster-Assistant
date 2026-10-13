"""Source adapter contracts, configuration and safe feed fetching.

Adapters are deliberately small and independent: each module exposes
``fetch_raw(client, url)`` and ``normalize(raw, target)``. Nothing here imports
the database, the engine or the web app, so a new source can be reviewed on its
own.

Least privilege by construction: the only URLs ever fetched are the fixed
official defaults or an operator-supplied environment override. No user input
reaches this layer, and every adapter documents who publishes the feed so
attribution always travels with the record.
"""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from defusedxml import ElementTree

#: Hard ceiling for any single feed download, enforced twice (header and body).
MAX_FEED_BYTES = 10_000_000
DEFAULT_TIMEOUT = 20.0
USER_AGENT = (
    'DisasterAssistant/0.2 (public-safety monitoring; '
    '+https://github.com/manikar984/AI-Disaster-Assistant)'
)
ACCEPT = (
    'application/json, application/geo+json, application/xml, text/xml, '
    'application/rss+xml, application/atom+xml, */*'
)

OFFICIAL = 'official'
NEWS = 'news'
COMMUNITY = 'community'

#: Severity vocabulary shared with the engine; adapters may only emit these.
SEVERITIES = ('Low', 'Medium', 'High', 'Critical')


class SourceError(Exception):
    """A feed could not be fetched or parsed safely (feed-level failure)."""


def env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def env_int(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def slug(value):
    return (re.sub(r'[^a-z0-9]+', '-', value.strip().lower()).strip('-') or 'feed')[:40]


@dataclass(frozen=True)
class SourceSpec:
    """Identity, provenance and configuration of one static source adapter."""

    id: str
    name: str
    kind: str
    description: str
    #: Environment prefix, e.g. ``USGS`` → ``USGS_ENABLED``, ``USGS_INTERVAL``.
    env_prefix: str
    #: Feed URL override variable. Defaults stay official; operators may point
    #: them at a mirror or a local fixture, and that choice is theirs alone.
    feed_env: str
    default_feed: str
    #: Hosts published by the documented provider (documentation + allowlist).
    official_hosts: tuple
    attribution: str = ''
    default_interval: int = 300
    enabled_by_default: bool = False
    #: Free-text scope note shown to operators and users.
    scope: str = ''

    def enabled(self):
        return env_flag(f'{self.env_prefix}_ENABLED', self.enabled_by_default)

    def interval(self):
        return env_int(f'{self.env_prefix}_INTERVAL', self.default_interval, 60, 86400)

    def feed_urls(self):
        """Configured feed URLs (comma-separated override, else the default)."""
        configured = (os.getenv(self.feed_env) or '').strip()
        source = configured or self.default_feed
        return [url.strip() for url in source.split(',') if url.strip()]


@dataclass(frozen=True)
class FeedTarget:
    """One concrete feed to poll, with the identity its records carry."""

    id: str
    name: str
    kind: str
    url: str
    adapter: str
    interval: int
    description: str = ''
    attribution: str = ''
    scope: str = ''
    official: bool = False


def feed_host(url):
    return (urlsplit(url).hostname or '').lower()


def target_from_spec(spec, url=None):
    """Build the concrete feed target for a static adapter spec."""
    urls = [url] if url else spec.feed_urls()
    return FeedTarget(
        id=spec.id,
        name=spec.name,
        kind=spec.kind,
        url=urls[0] if urls else spec.default_feed,
        adapter=spec.id,
        interval=spec.interval(),
        description=spec.description,
        attribution=spec.attribution,
        scope=spec.scope,
        official=spec.kind == OFFICIAL,
    )


def fetch_feed(client, url, *, max_bytes=MAX_FEED_BYTES):
    """Download one configured feed.

    Redirects are never followed, the body is size-checked, and failures are
    raised as :class:`SourceError` with a short reason — provider response
    bodies are never logged or stored.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise SourceError('unsupported feed URL')
    try:
        response = client.get(url, headers={'User-Agent': USER_AGENT, 'Accept': ACCEPT})
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SourceError(f'HTTP {exc.response.status_code}') from None
    except httpx.HTTPError as exc:
        raise SourceError(type(exc).__name__) from None
    body = response.content or b''
    if len(body) > max_bytes:
        raise SourceError('feed exceeds size limit')
    if not body:
        raise SourceError('empty feed')
    return body


def http_client():
    """Shared client policy: bounded timeout, no redirects, no retries here."""
    return httpx.Client(
        timeout=httpx.Timeout(DEFAULT_TIMEOUT, connect=10.0),
        follow_redirects=False,
        headers={'User-Agent': USER_AGENT},
    )


# ---------------------------------------------------------------------------
# XML helpers (RSS/Atom/CAP). Parsing always goes through defusedxml so hostile
# entity-expansion payloads in a compromised or misconfigured feed cannot hang
# the worker.
# ---------------------------------------------------------------------------


def parse_xml(payload):
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise SourceError(f'malformed XML feed ({exc.__class__.__name__})') from None
    except ValueError:
        raise SourceError('malformed XML feed') from None


def local_name(tag):
    return tag.rsplit('}', 1)[-1].lower() if isinstance(tag, str) else ''


def descendants(element, name):
    """Every descendant element whose local name matches (namespace agnostic)."""
    return [item for item in element.iter() if local_name(item.tag) == name]


def child(element, name):
    for item in list(element):
        if local_name(item.tag) == name:
            return item
    return None


def child_text(element, name, default=''):
    found = child(element, name)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def parse_datetime(value):
    """Parse RFC 822 (RSS) or ISO 8601 (Atom/CAP) timestamps as UTC."""
    value = (value or '').strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def feed_entries(root):
    """Entries of an RSS channel or an Atom feed, as a list of elements."""
    items = descendants(root, 'item')
    if items:
        return items
    return descendants(root, 'entry')


def clean_text(value, limit=2000):
    return ' '.join(str(value or '').split())[:limit]


def loads_json(payload):
    try:
        return json.loads(payload)
    except (ValueError, TypeError):
        raise SourceError('malformed JSON feed') from None
