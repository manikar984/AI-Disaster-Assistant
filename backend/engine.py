"""Screening, grouping, geographic matching and notification qualification.

Deliberately transparent: every decision here can be explained to a user in one
sentence. Structured hints from an official adapter (category, severity, CAP
certainty) are used when present; otherwise text screening applies. Volume never
upgrades trust — see :mod:`backend.verification`.
"""

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone

from . import verification
from .db import now

LEVELS = {'Low': 0, 'Medium': 1, 'High': 2, 'Critical': 3}

#: Hazard categories the platform can hold. Adapters may only name these.
CATEGORIES = (
    'Flood',
    'Earthquake',
    'Storm',
    'Wildfire',
    'Landslide',
    'Tsunami',
    'Heatwave',
    'Industrial',
    'Volcano',
)

VERIFICATIONS = ('Unverified', 'Developing', 'Corroborated', 'Officially Confirmed', 'Disputed', 'Resolved')

PATTERNS = {
    'Flood': r'\b(flood(?:ing|s)?|flash flood)\b',
    'Earthquake': r'\b(earthquake|seismic|aftershock)\b',
    'Storm': r'\b(cyclone|hurricane|typhoon|tornado|severe storm|extreme rainfall)\b',
    'Wildfire': r'\b(wildfire|forest fire|bushfire)\b',
    'Landslide': r'\b(landslide|mudslide)\b',
    'Tsunami': r'\btsunami\b',
    'Heatwave': r'\b(heatwave|extreme heat)\b',
    'Industrial': r'\b(chemical leak|industrial explosion|factory fire)\b',
    'Volcano': r'\b(volcanic eruption|volcano|volcanic ash|lava flow)\b',
}
#: Obvious negation, metaphor and exercise language that must never classify.
NEGATION = r'\b(drill|simulation|fiction|video game|no (?:flood|earthquake)|human flood|mike flood)\b'
SEVERE_WORDS = r'\b(deadly|destroyed|severe|major)\b'
MINOR_WORDS = r'\b(minor|small)\b'

GUIDANCE = 'Check local emergency authorities for current instructions. This service supplements, not replaces, official emergency systems. Do not approach the reported incident.'

#: Non-official reports may support an existing incident within this window.
MATCH_WINDOW = timedelta(hours=1)
MATCH_DISTANCE_KM = 25
#: Geographic precision labels attached to reports and incidents.
GEO_PRECISION = ('point', 'alert-area-centre', 'approximate-region', 'unknown')


def distance(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(min(1, a)))


def severity_from_magnitude(magnitude):
    return 'Critical' if magnitude >= 7 else 'High' if magnitude >= 5.5 else 'Medium' if magnitude >= 4 else 'Low'


def severity_from_text(text):
    if re.search(SEVERE_WORDS, text, re.I):
        return 'High'
    if re.search(MINOR_WORDS, text, re.I):
        return 'Low'
    return 'Medium'


def classify(text, magnitude=None, category=None, severity_hint=None):
    """(category, severity) for one record. Hints come from structured adapters."""
    if category in CATEGORIES:
        kind = category
    elif magnitude is not None:
        kind = 'Earthquake'
    elif re.search(NEGATION, text, re.I):
        return None, 'Low'
    else:
        kind = next((name for name, pattern in PATTERNS.items() if re.search(pattern, text, re.I)), None)
    if not kind:
        return None, 'Low'
    if severity_hint in LEVELS:
        severity = severity_hint
    elif magnitude is not None:
        severity = severity_from_magnitude(magnitude)
    else:
        severity = severity_from_text(text)
    return kind, severity


def _geographic_precision(record, lat):
    precision = record.get('geo_precision')
    if precision in GEO_PRECISION:
        return precision
    return 'point' if lat is not None else 'unknown'


def _verification(record, mode):
    if not record.get('official') or mode in ('sample', 'simulated'):
        return 'Unverified', 'Limited evidence'
    hint = record.get('verification_hint')
    if hint == 'Developing':
        return 'Developing', 'Official source, tentative'
    if hint == 'Officially Confirmed':
        return 'Officially Confirmed', 'Official source'
    return 'Officially Confirmed', 'Official source'


def _timeline_note(db, incident_id, note, status):
    """Append a timeline entry unless it repeats the previous one verbatim."""
    previous = db.execute(
        'SELECT note FROM timeline WHERE incident_id=? ORDER BY id DESC LIMIT 1', (incident_id,)
    ).fetchone()
    if previous and previous['note'] == note:
        return
    db.execute(
        'INSERT INTO timeline(incident_id,created_at,status,note) VALUES (?,?,?,?)',
        (incident_id, now(), status, note),
    )


def ingest(db, record):
    """Normalized internal record → durable report, incident association and evidence.

    Official trust is set by the adapter, never by public input. Returns the
    incident id, or ``None`` when the record is a duplicate, a syndicated copy
    or did not classify as a supported hazard.
    """
    text = str(record['text']).strip()[:10000]
    if not text:
        raise ValueError('empty report text')
    published = datetime.fromisoformat(str(record['published_at']).replace('Z', '+00:00'))
    if published.tzinfo is None or published > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError('invalid publication timestamp')
    lat, lon = record.get('lat'), record.get('lon')
    if (lat is None) != (lon is None) or (
        lat is not None
        and (not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180)
    ):
        raise ValueError('invalid coordinates')
    mode = record.get('mode', 'live')
    if mode not in ('live', 'delayed', 'sample', 'simulated'):
        raise ValueError('invalid provenance')
    if mode == 'live' and datetime.now(timezone.utc) - published > timedelta(hours=6):
        mode = 'delayed'
    source = str(record['source'])[:80]
    external_id = str(record.get('external_id') or hashlib.sha256(text.casefold().encode()).hexdigest())[:200]
    dedupe_key = (record.get('dedupe_key') or None)
    geo_precision = _geographic_precision(record, lat)
    collected = now()
    cursor = db.execute(
        'INSERT OR IGNORE INTO reports(source,external_id,url,text,location,lat,lon,published_at,collected_at,'
        'mode,official,raw,user_id,dedupe_key,geo_precision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            source,
            external_id,
            record.get('url'),
            text,
            record.get('location', 'Unknown'),
            lat,
            lon,
            published.isoformat(),
            collected,
            mode,
            int(record.get('official', False)),
            json.dumps(record),
            record.get('user_id'),
            dedupe_key,
            geo_precision,
        ),
    )
    if not cursor.rowcount:
        existing = db.execute(
            'SELECT * FROM reports WHERE source=? AND external_id=?', (source, external_id)
        ).fetchone()
        if not existing:
            # Suppressed as a syndicated copy of a story already stored.
            return None
        changed = existing['text'] != text or existing['lat'] != lat or existing['lon'] != lon
        if not changed or not record.get('official'):
            return None
        db.execute(
            'UPDATE reports SET text=?,lat=?,lon=?,raw=?,collected_at=?,geo_precision=? WHERE id=?',
            (text, lat, lon, json.dumps(record), collected, geo_precision, existing['id']),
        )
        incident = db.execute(
            'SELECT i.* FROM incidents i JOIN incident_reports ir ON ir.incident_id=i.id WHERE ir.report_id=?',
            (existing['id'],),
        ).fetchone()
        if incident:
            kind, updated_severity = classify(
                text, record.get('magnitude'), record.get('category'), record.get('severity_hint')
            )
            # Preserve human review decisions while updating the supporting evidence.
            reviewed = db.execute('SELECT 1 FROM audit WHERE incident_id=?', (incident['id'],)).fetchone()
            db.execute(
                'UPDATE incidents SET summary=?,lat=?,lon=?,severity=?,geo_precision=?,last_updated=?,'
                'version=version+1 WHERE id=?',
                (
                    ' '.join(text.split())[:400],
                    lat,
                    lon,
                    incident['severity'] if reviewed else updated_severity,
                    geo_precision,
                    collected,
                    incident['id'],
                ),
            )
            db.execute(
                'INSERT INTO timeline(incident_id,created_at,status,note) VALUES (?,?,?,?)',
                (incident['id'], collected, incident['verification'], 'Official source revised the observation.'),
            )
            return incident['id']
        return None
    report_id = cursor.lastrowid
    kind, severity = classify(text, record.get('magnitude') if record.get('official') else None,
                              record.get('category'), record.get('severity_hint'))
    if not kind:
        return None
    trust, confidence = _verification(record, mode)
    match = None
    if lat is not None and not record.get('official'):
        # Support an existing incident (including official ones) without altering
        # its severity or verification state. Conservative distance and window.
        candidates = db.execute(
            "SELECT * FROM incidents WHERE type=? AND mode=? AND verification NOT IN ('Resolved','Disputed') "
            'ORDER BY id DESC LIMIT 100',
            (kind, mode),
        ).fetchall()
        for candidate in candidates:
            if candidate['lat'] is None:
                continue
            published_candidate = datetime.fromisoformat(candidate['published_at'])
            if abs((published - published_candidate).total_seconds()) < MATCH_WINDOW.total_seconds() and distance(
                lat, lon, candidate['lat'], candidate['lon']
            ) < MATCH_DISTANCE_KM:
                match = candidate
                break
    if match:
        incident_id = match['id']
        db.execute('UPDATE incidents SET last_updated=? WHERE id=?', (collected, incident_id))
        _timeline_note(
            db,
            incident_id,
            f'Supporting report attached from {record.get("source_name") or source}.',
            match['verification'],
        )
    else:
        cursor = db.execute(
            'INSERT INTO incidents(type,location,lat,lon,severity,verification,confidence,mode,summary,'
            'first_detected,last_updated,published_at,geo_precision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                kind,
                record.get('location', 'Unknown'),
                lat,
                lon,
                severity,
                trust,
                confidence,
                mode,
                ' '.join(text.split())[:400],
                collected,
                collected,
                published.isoformat(),
                geo_precision,
            ),
        )
        incident_id = cursor.lastrowid
        db.execute(
            'INSERT INTO timeline(incident_id,created_at,status,note) VALUES (?,?,?,?)',
            (
                incident_id,
                collected,
                trust,
                f'Detected from {record.get("source_name") or source}. Severity is a screening estimate.',
            ),
        )
    db.execute('INSERT INTO incident_reports VALUES (?,?)', (incident_id, report_id))
    verification.maybe_corroborate(db, incident_id)
    return incident_id


def qualify_notifications(db):
    """Decide which incidents deserve an alert, and for whom.

    Gates (all must hold): live/delayed provenance, published within 6 hours,
    an established location, corroborated or officially confirmed verification,
    the user's radius, and the user's minimum severity. Duplicates are prevented
    by ``(user, incident, version)``; non-critical alerts are capped per hour.
    Corrections (Resolved/Disputed) reach users who were already notified.
    """
    current = datetime.now(timezone.utc)
    incidents = db.execute(
        "SELECT * FROM incidents WHERE mode IN ('live','delayed') "
        "ORDER BY CASE severity WHEN 'Critical' THEN 0 WHEN 'High' THEN 1 ELSE 2 END"
    ).fetchall()
    for i in incidents:
        previous = {r[0] for r in db.execute('SELECT DISTINCT user_id FROM notifications WHERE incident_id=?', (i['id'],))}
        fresh = timedelta(0) <= current - datetime.fromisoformat(i['published_at']) <= timedelta(hours=6)
        eligible = fresh and i['lat'] is not None and i['verification'] in ('Corroborated', 'Officially Confirmed')
        if not eligible and not (previous and i['verification'] in ('Resolved', 'Disputed')):
            continue
        for user in db.execute('SELECT id,email_verified FROM users').fetchall():
            locations = db.execute('SELECT * FROM locations WHERE user_id=?', (user['id'],)).fetchall()
            matches = [
                l
                for l in locations
                if eligible
                and LEVELS[i['severity']] >= LEVELS[l['min_severity']]
                and distance(i['lat'], i['lon'], l['lat'], l['lon']) <= l['radius']
            ]
            if not matches and not (user['id'] in previous and (eligible or i['verification'] in ('Resolved', 'Disputed'))):
                continue
            # 20 non-critical notifications/hour/user. Critical and corrections bypass digest throttling.
            count = db.execute(
                'SELECT count(*) FROM notifications WHERE user_id=? AND created_at>?',
                (user['id'], (current - timedelta(hours=1)).isoformat()),
            ).fetchone()[0]
            if count >= 20 and i['severity'] != 'Critical' and i['verification'] not in ('Resolved', 'Disputed'):
                continue
            stamp = now()
            message = f"{i['severity']} {i['type']} · {i['location']} · {i['verification']}. {GUIDANCE}"
            cur = db.execute(
                'INSERT OR IGNORE INTO notifications(user_id,incident_id,version,message,created_at) VALUES (?,?,?,?,?)',
                (user['id'], i['id'], i['version'], message, stamp),
            )
            if cur.rowcount:
                db.execute(
                    "INSERT INTO deliveries(notification_id,channel,status,next_attempt,sent_at) "
                    "VALUES (?,'in-app','delivered',?,?)",
                    (cur.lastrowid, stamp, stamp),
                )
                # Email requires consent *and* a confirmed address: an unconfirmed
                # address may belong to someone who never signed up. The in-app
                # notification above is unaffected.
                if user['email_verified'] and any(l['email_enabled'] for l in (matches or locations)):
                    db.execute(
                        "INSERT INTO deliveries(notification_id,channel,status,next_attempt) VALUES (?,'email','pending',?)",
                        (cur.lastrowid, stamp),
                    )
