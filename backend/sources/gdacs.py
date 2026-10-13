"""GDACS — Global Disaster Alert and Coordination System (EU JRC / UN OCHA).

Independent second official source. GDACS publishes a GeoRSS feed of current
alerts with an explicit hazard type, coordinates, alert level and, for
earthquakes, a magnitude. Only the documented public RSS endpoint is fetched.

Mapping policy (documented in docs/SOURCES.md):
  * ``gdacs:eventtype`` maps to the platform hazard category;
  * ``gdacs:alertlevel`` Red/Orange/Green maps to Critical/High/Medium;
  * the alert level is the source's own severity for the event, and a magnitude is
    kept so the engine can map it when no alert level is published;
  * the record stays marked as an official *alert feed*, never as a
    life-safety instruction, and its published timestamp decides live/delayed.
"""

import re

from .base import (
    OFFICIAL,
    SourceSpec,
    child_text,
    clean_text,
    descendants,
    feed_entries,
    feed_host,
    fetch_feed,
    parse_datetime,
    parse_xml,
    target_from_spec,
)

SPEC = SourceSpec(
    id='gdacs',
    name='GDACS (EU JRC / UN OCHA)',
    kind=OFFICIAL,
    description='Official multi-hazard alert feed with alert levels for earthquakes, cyclones, floods, wildfires and volcanoes.',
    env_prefix='GDACS',
    feed_env='GDACS_FEED_URL',
    default_feed='https://www.gdacs.org/xml/rss.xml',
    official_hosts=('www.gdacs.org', 'gdacs.org'),
    attribution='GDACS — European Commission JRC and UN OCHA',
    default_interval=300,
    scope='Global hazard alerts. Alert level describes modelled potential impact, not an evacuation order.',
)

EVENT_TYPES = {
    'EQ': 'Earthquake',
    'TC': 'Storm',
    'FL': 'Flood',
    'WF': 'Wildfire',
    'VO': 'Volcano',
    'TS': 'Tsunami',
}
ALERT_LEVELS = {'red': 'Critical', 'orange': 'High', 'green': 'Medium'}
#: GDACS titles read like "Orange flood alert in Testland on 13/10/2026 12:00 UTC".
TITLE_LOCATION = re.compile(r"\bin\s+([A-Za-z][A-Za-z .,'\-]{1,80}?)(?:\s+on\s+|\s+\d{2}/|$)")


def target(url=None):
    """The concrete feed target for this adapter (configuration-aware)."""
    return target_from_spec(SPEC, url)


def fetch_raw(client, url):
    root = parse_xml(fetch_feed(client, url))
    entries = feed_entries(root)
    if not entries:
        raise ValueError('no feed entries')
    return entries[:500]


def _magnitude(entry):
    for element in descendants(entry, 'severity'):
        unit = (element.get('unit') or '').strip().lower()
        value = element.get('value')
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if unit in ('m', 'mw', 'mwp', 'mag', 'magnitude') and -2 <= number <= 10:
            return number
    return None


def normalize(entry, target):
    event_type = child_text(entry, 'eventtype').upper()
    category = EVENT_TYPES.get(event_type)
    alert_level = child_text(entry, 'alertlevel').strip().lower()
    title = clean_text(child_text(entry, 'title'), 300)
    description = clean_text(child_text(entry, 'description'), 1500)
    if not category or not title:
        raise ValueError('unsupported GDACS event type')
    latitude = child_text(entry, 'lat')
    longitude = child_text(entry, 'long')
    if not latitude or not longitude:
        raise ValueError('missing coordinates')
    magnitude = _magnitude(entry)
    published = parse_datetime(
        child_text(entry, 'pubdate') or child_text(entry, 'updated') or child_text(entry, 'date')
    )
    if published is None:
        raise ValueError('missing publication time')
    link = clean_text(child_text(entry, 'link'), 500)
    if link and feed_host(link) not in SPEC.official_hosts:
        # Evidence links must stay on the provider's own domain.
        link = ''
    event_id = child_text(entry, 'eventid') or clean_text(child_text(entry, 'guid'), 200)
    if not event_id:
        raise ValueError('missing event identifier')
    # The episode id changes each time GDACS revises an alert, so the stable
    # event id is used: revisions then update the incident instead of copying it.
    external_id = f'{event_type}-{event_id}'
    text = ' — '.join(part for part in (title, description) if part)
    return dict(
        source=target.id,
        source_name=target.name,
        source_kind=target.kind,
        external_id=external_id,
        url=link or target.url,
        text=text or title,
        location=(
            clean_text(child_text(entry, 'country'), 150)
            or clean_text((TITLE_LOCATION.search(title) or [None, ''])[1] if TITLE_LOCATION.search(title) else '', 150)
            or 'Location not named'
        ),
        lat=float(latitude),
        lon=float(longitude),
        published_at=published.isoformat(),
        mode='live',
        official=True,
        category=category,
        magnitude=magnitude,
        severity_hint=ALERT_LEVELS.get(alert_level),
        geo_precision='point',
        attribution=target.attribution,
        provider_status=alert_level,
    )
