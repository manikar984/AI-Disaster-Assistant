"""Common Alerting Protocol (CAP 1.2) warning feeds, configured by the operator.

CAP is the international standard for public warning messages, so this adapter
is the highest-trust path in the platform. It accepts either bare CAP
(``<alert>``/``<alerts>``) or an Atom/RSS feed whose entries embed CAP XML.

Mapping policy (see docs/SOURCES.md):
  * ``info.event`` is matched against a transparent category vocabulary;
  * ``info.severity`` Extreme/Severe/Moderate/Minor → Critical/High/Medium/Low;
  * ``info.certainty`` Observed/Likely keeps official confirmation, anything
    weaker (Possible/Unlikely/Unknown) is downgraded to ``Developing`` so a
    tentative alert is never presented as confirmed;
  * the first ``area/circle`` centre becomes the reported point and the record
    is labelled as an alert *area* centre, not a precise location;
  * ``info.instruction`` is stored verbatim as official guidance text and is
    never paraphrased into new safety instructions.
"""

import html
import re
from datetime import datetime, timezone

from .base import (
    OFFICIAL,
    SourceSpec,
    child,
    child_text,
    clean_text,
    descendants,
    fetch_feed,
    local_name,
    parse_datetime,
    parse_xml,
)

SPEC = SourceSpec(
    id='cap',
    name='Common Alerting Protocol feeds',
    kind=OFFICIAL,
    description='Operator-configured CAP 1.2 warning feeds from official alerting authorities.',
    env_prefix='CAP',
    feed_env='CAP_FEEDS',
    default_feed='',
    official_hosts=(),
    attribution='Respective alerting authorities, linked per alert',
    default_interval=120,
    scope='Official public warnings. Instructions are quoted verbatim and always attributed.',
)

CATEGORY_KEYWORDS = (
    ('Earthquake', ('earthquake', 'seismic', 'aftershock')),
    ('Tsunami', ('tsunami',)),
    ('Volcano', ('volcan', 'ash fall', 'lava')),
    ('Landslide', ('landslide', 'mudslide', 'rockfall', 'debris flow')),
    ('Wildfire', ('wildfire', 'forest fire', 'bushfire', 'fire weather')),
    ('Flood', ('flood', 'storm surge', 'dam break', 'inundation')),
    ('Storm', ('cyclone', 'hurricane', 'typhoon', 'tornado', 'storm', 'wind', 'blizzard')),
    ('Heatwave', ('heat', 'temperature extreme')),
    ('Industrial', ('chemical', 'hazmat', 'industrial', 'radiation', 'gas leak')),
)
SEVERITY_MAP = {'extreme': 'Critical', 'severe': 'High', 'moderate': 'Medium', 'minor': 'Low'}
CONFIRMED_CERTAINTY = ('observed', 'likely')


def fetch_raw(client, url):
    root = parse_xml(fetch_feed(client, url))
    alerts = descendants(root, 'alert')
    if alerts:
        return alerts[:200]
    # Atom/RSS wrapper: CAP XML arrives escaped inside entry content.
    collected = []
    for entry in descendants(root, 'entry') + descendants(root, 'item'):
        for element in entry.iter():
            if local_name(element.tag) in ('content', 'summary', 'description') and element.text:
                text = html.unescape(element.text).strip()
                if '<alert' in text:
                    nested = parse_xml(text.encode())
                    collected.extend([nested] if local_name(nested.tag) == 'alert' else descendants(nested, 'alert'))
    if not collected:
        raise ValueError('no CAP alerts in feed')
    return collected[:200]


def _info(alert):
    for element in list(alert):
        if local_name(element.tag) == 'info':
            return element
    raise ValueError('missing CAP info block')


def _category(event, headline):
    haystack = f'{event} {headline}'.lower()
    for category, keywords in CATEGORY_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return category
    return None


def _circle_centre(info):
    for circle in descendants(info, 'circle'):
        parts = (circle.text or '').strip().split()
        if len(parts) >= 2 and ',' in parts[0]:
            latitude, _, longitude = parts[0].partition(',')
            try:
                return float(latitude), float(longitude)
            except ValueError:
                continue
    return None


def normalize(alert, target):
    info = _info(alert)
    event = clean_text(child_text(info, 'event'), 200)
    headline = clean_text(child_text(info, 'headline'), 300)
    description = clean_text(child_text(info, 'description'), 1500)
    instruction = clean_text(child_text(info, 'instruction'), 1000)
    if not event and not headline:
        raise ValueError('missing CAP event description')
    sent = parse_datetime(child_text(alert, 'sent'))
    if sent is None:
        raise ValueError('missing CAP sent time')
    identifier = clean_text(child_text(alert, 'identifier'), 200)
    if not identifier:
        raise ValueError('missing CAP identifier')
    severity = SEVERITY_MAP.get(child_text(info, 'severity').strip().lower())
    certainty = child_text(info, 'certainty').strip().lower()
    urgency = child_text(info, 'urgency').strip().lower()
    area_description = ''
    for area in descendants(info, 'area'):
        area_description = area_description or clean_text(child_text(area, 'areadesc'), 200)
    centre = _circle_centre(info)
    references = clean_text(child_text(alert, 'references'), 500)
    parts = [part for part in (headline or event, event if headline else '', description) if part]
    if instruction:
        parts.append(f'Official instruction: {instruction}')
    return dict(
        source=target.id,
        source_name=target.name,
        source_kind=target.kind,
        external_id=f'{target.id}:{identifier}',
        url=target.url,
        text=' — '.join(parts)[:4000],
        location=area_description or 'Alert area not named',
        lat=centre[0] if centre else None,
        lon=centre[1] if centre else None,
        published_at=sent.isoformat(),
        mode='live',
        official=True,
        category=_category(event, headline),
        severity_hint=severity,
        verification_hint='Officially Confirmed' if certainty in CONFIRMED_CERTAINTY else 'Developing',
        geo_precision='alert-area-centre' if centre else 'unknown',
        attribution=target.attribution,
        provider_status=f'certainty={certainty or "unknown"};urgency={urgency or "unknown"}',
        official_instruction=instruction,
        references=references,
        expires=child_text(alert, 'expires'),
    )
