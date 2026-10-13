"""Source adapter registry.

One place decides which feeds are polled. Static adapters (USGS, GDACS) carry
their own official default endpoint; operator-configured feeds (news RSS/Atom,
CAP warnings) are named in environment variables and never fetched otherwise.

Everything here is configuration-driven and side-effect free so it can be
tested, listed in the API and documented without running a poll.
"""

import os

from . import cap, gdacs, rss, usgs
from .base import (
    NEWS,
    OFFICIAL,
    FeedTarget,
    SourceSpec,
    env_flag,
    env_int,
    feed_host,
    slug,
)

#: Adapters with a fixed identity; `rss` and `cap` are configured per feed.
STATIC_ADAPTERS = (usgs, gdacs, rss, cap)
ADAPTER_MODULES = {module.SPEC.id: module for module in STATIC_ADAPTERS}

NEWS_INTERVAL = ('NEWS_INTERVAL', 300, 60, 86400)
CAP_INTERVAL = ('CAP_INTERVAL', 60 * 5, 60, 86400)


def specs():
    """Static adapter specs keyed by id (documentation, seeding, API listing)."""
    return {module.SPEC.id: module.SPEC for module in STATIC_ADAPTERS}


def adapter_for(target):
    module = ADAPTER_MODULES.get(target.adapter)
    if module is None:
        raise ValueError(f'unknown adapter {target.adapter!r}')
    return module


def parse_named_feeds(value):
    """Parse ``Name|https://url,Name two|https://url2`` into (name, url) pairs."""
    feeds = []
    for chunk in (value or '').split(','):
        chunk = chunk.strip()
        if not chunk or '|' not in chunk:
            continue
        name, _, url = chunk.partition('|')
        name, url = name.strip(), url.strip()
        if url.startswith(('http://', 'https://')):
            feeds.append((name or feed_host(url) or 'feed', url))
    return feeds


def feed_targets():
    """Every configured feed, in poll order. Empty when nothing is enabled."""
    targets = []
    for module in STATIC_ADAPTERS:
        spec = module.SPEC
        if spec.id in ('rss', 'cap'):
            continue  # operator-configured only, handled below
        if not spec.enabled():
            continue
        for url in spec.feed_urls():
            targets.append(
                FeedTarget(
                    id=spec.id,
                    name=spec.name,
                    kind=spec.kind,
                    url=url,
                    adapter=spec.id,
                    interval=spec.interval(),
                    description=spec.description,
                    attribution=spec.attribution,
                    scope=spec.scope,
                    official=spec.kind == OFFICIAL,
                )
            )
    if env_flag('NEWS_ENABLED', True):
        interval = env_int(*NEWS_INTERVAL)
        for name, url in parse_named_feeds(os.getenv('NEWS_FEEDS')):
            targets.append(
                FeedTarget(
                    id=f'news-{slug(name)}',
                    name=name,
                    kind=NEWS,
                    url=url,
                    adapter='news-rss',
                    interval=interval,
                    description='Public news/bulletin feed. Titles, links and timestamps only.',
                    attribution=name,
                    scope='Third-party reporting. Unverified until independently corroborated.',
                )
            )
    if env_flag('CAP_ENABLED', True):
        interval = env_int(*CAP_INTERVAL)
        for name, url in parse_named_feeds(os.getenv('CAP_FEEDS')):
            targets.append(
                FeedTarget(
                    id=f'cap-{slug(name)}',
                    name=name,
                    kind=OFFICIAL,
                    url=url,
                    adapter='cap',
                    interval=interval,
                    description='CAP 1.2 public warnings from an official alerting authority.',
                    attribution=name,
                    scope='Official warnings. Instructions are quoted verbatim with attribution.',
                    official=True,
                )
            )
    return targets


def source_rows():
    """Configuration rows for the API and database seeding.

    Includes static adapters that are currently disabled (so users can see what
    exists but is not configured) and every operator-configured feed.
    """
    rows = []
    for spec in specs().values():
        rows.append(
            dict(
                id=spec.id,
                name=spec.name,
                kind=spec.kind,
                description=spec.description,
                attribution=spec.attribution,
                scope=spec.scope,
                enabled=int(spec.enabled()) if spec.id not in ('rss', 'cap') else 0,
                interval_seconds=spec.interval(),
                feed_host='' if spec.id in ('rss', 'cap') else feed_host(spec.feed_urls()[0]) if spec.feed_urls() else '',
            )
        )
    for target in feed_targets():
        rows.append(
            dict(
                id=target.id,
                name=target.name,
                kind=target.kind,
                description=target.description,
                attribution=target.attribution,
                scope=target.scope,
                enabled=1,
                interval_seconds=target.interval,
                feed_host=feed_host(target.url),
            )
        )
    return rows
