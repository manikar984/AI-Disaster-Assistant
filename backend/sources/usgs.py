"""USGS Earthquake Hazards Program — official structured earthquake feed.

Publisher: United States Geological Survey (public domain). The adapter accepts
only measured earthquakes that carry a magnitude, a validity-checked evidence
URL on the USGS host, and coordinates. Severity is mapped from magnitude by the
engine's documented policy; a USGS observation is *not* a public safety warning.
"""

import math
from datetime import datetime, timezone

from .base import OFFICIAL, SourceSpec, fetch_feed, loads_json, target_from_spec

SPEC = SourceSpec(
    id='usgs',
    name='USGS Earthquake Hazards Program',
    kind=OFFICIAL,
    description='Official seismic observations with measured magnitude, depth and review status.',
    env_prefix='USGS',
    feed_env='USGS_FEED_URL',
    default_feed='https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson',
    official_hosts=('earthquake.usgs.gov',),
    attribution='U.S. Geological Survey',
    default_interval=60,
    scope='Global earthquakes measured by USGS networks; not a warning or impact prediction.',
)


def target(url=None):
    """The concrete feed target for this adapter (configuration-aware)."""
    return target_from_spec(SPEC, url)


def fetch_raw(client, url):
    """Fetch the GeoJSON feature collection and return its raw features."""
    document = loads_json(fetch_feed(client, url))
    features = document.get('features') if isinstance(document, dict) else None
    if not isinstance(features, list):
        raise ValueError('malformed feature collection')
    return features[:5000]


def normalize(feature, target):
    """Convert one GeoJSON feature into the shared normalized record contract."""
    properties = feature['properties']
    coordinates = feature['geometry']['coordinates']
    if properties.get('type') != 'earthquake':
        raise ValueError('not an earthquake record')
    if properties.get('mag') is None:
        raise ValueError('missing magnitude')
    magnitude = float(properties['mag'])
    if not math.isfinite(magnitude) or not -2 <= magnitude <= 10:
        raise ValueError('invalid magnitude')
    url = properties.get('url', '')
    if not url.startswith('https://earthquake.usgs.gov/'):
        raise ValueError('invalid evidence URL')
    published = datetime.fromtimestamp(properties['time'] / 1000, timezone.utc)
    return dict(
        source=target.id,
        source_name=target.name,
        source_kind=target.kind,
        external_id=str(feature['id']),
        url=url,
        text=f'Magnitude {magnitude:g} earthquake — {properties.get("place") or "Location not named"}',
        location=properties.get('place') or 'Location not named',
        lat=float(coordinates[1]),
        lon=float(coordinates[0]),
        published_at=published.isoformat(),
        mode='live',
        official=True,
        category='Earthquake',
        magnitude=magnitude,
        geo_precision='point',
        attribution=target.attribution,
        provider_status=properties.get('status') or '',
    )
