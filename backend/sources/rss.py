"""News and bulletin feeds (RSS 2.0 / Atom), configured by the operator.

Not a scraper: only feeds the operator explicitly lists in ``NEWS_FEEDS`` are
fetched, one aggregate entry per article is stored, and the original link,
publisher name and publication time travel with the record. Full article text is
never republished — the stored text is the feed's own title/summary.

Syndicated copies of one story are stored once: a normalized headline/day key is
written with the first copy, and later copies from other configured feeds are
counted as skipped instead of creating parallel incidents.

Records carry no coordinates unless the feed itself supplies GeoRSS points, so
they cannot qualify for location alerts until a location is established. The
location field says so explicitly.
"""

import hashlib
import html
import re

from .base import (
    NEWS,
    SourceSpec,
    child,
    child_text,
    clean_text,
    feed_entries,
    fetch_feed,
    parse_datetime,
    parse_xml,
)

SPEC = SourceSpec(
    id='news-rss',
    name='News and bulletin feeds (RSS/Atom)',
    kind=NEWS,
    description='Operator-configured public RSS/Atom feeds. Titles, links, publisher and timestamps only.',
    env_prefix='NEWS',
    feed_env='NEWS_FEEDS',
    default_feed='',
    official_hosts=(),
    attribution='Respective publishers, linked per item',
    default_interval=300,
    scope='Third-party reporting, always unverified until corroborated. Never treated as an official warning.',
)

TAG_RE = re.compile(r'<[^>]+>')
TITLE_NOISE_RE = re.compile(r'[^a-z0-9 ]+')


def fetch_raw(client, url):
    root = parse_xml(fetch_feed(client, url))
    entries = feed_entries(root)
    if not entries:
        raise ValueError('no feed entries')
    return entries[:200]


def _link(entry):
    """Item link: RSS text content or Atom href (preferring rel=alternate)."""
    for element in list(entry):
        if element.tag.rsplit('}', 1)[-1].lower() != 'link':
            continue
        href = element.get('href')
        if href:
            rel = (element.get('rel') or 'alternate').lower()
            if rel == 'alternate':
                return href.strip()
            continue
        if element.text and element.text.strip():
            return element.text.strip()
    return ''


def _coords(entry):
    """GeoRSS point ("lat lon") or geo:lat/geo:long pair, when present."""
    point = child_text(entry, 'point').split()
    if len(point) == 2:
        try:
            return float(point[0]), float(point[1])
        except ValueError:
            return None
    latitude, longitude = child_text(entry, 'lat'), child_text(entry, 'long')
    if latitude and longitude:
        try:
            return float(latitude), float(longitude)
        except ValueError:
            return None
    return None


def normalize(entry, target):
    title = clean_text(html.unescape(TAG_RE.sub(' ', child_text(entry, 'title'))), 300)
    if not title:
        raise ValueError('missing title')
    summary = clean_text(
        html.unescape(
            TAG_RE.sub(
                ' ',
                child_text(entry, 'description')
                or child_text(entry, 'summary')
                or child_text(entry, 'content'),
            )
        ),
        1200,
    )
    published = parse_datetime(
        child_text(entry, 'pubdate') or child_text(entry, 'published') or child_text(entry, 'updated')
        or child_text(entry, 'date')
    )
    if published is None:
        raise ValueError('missing publication time')
    link = _link(entry)[:500]
    if link and not link.startswith(('http://', 'https://')):
        link = ''
    identifier = clean_text(child_text(entry, 'guid') or child_text(entry, 'id'), 200) or link or title
    coordinates = _coords(entry)
    # Syndication key: normalized headline plus the UTC day it was published.
    title_key = ' '.join(TITLE_NOISE_RE.sub(' ', title.lower()).split())
    dedupe_key = hashlib.sha256(f'{title_key}|{published.date().isoformat()}'.encode()).hexdigest()
    return dict(
        source=target.id,
        source_name=target.name,
        source_kind=target.kind,
        external_id=hashlib.sha256(identifier.encode()).hexdigest()[:48],
        url=link or target.url,
        text=f'{title} — {summary}' if summary else title,
        location='Not established from feed',
        lat=coordinates[0] if coordinates else None,
        lon=coordinates[1] if coordinates else None,
        published_at=published.isoformat(),
        mode='live',
        official=False,
        dedupe_key=dedupe_key,
        geo_precision='point' if coordinates else 'unknown',
        attribution=target.attribution,
        provider_status='rss',
    )
