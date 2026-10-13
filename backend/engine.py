"""Conservative, explainable screening; no probability or trained-model claim."""
import hashlib
import json
import math
import re
from datetime import datetime, timezone, timedelta
from .db import now

LEVELS = {'Low': 0, 'Medium': 1, 'High': 2, 'Critical': 3}
PATTERNS = {
    'Flood': r'\b(flood(?:ing|s)?|flash flood)\b',
    'Earthquake': r'\b(earthquake|seismic|aftershock)\b',
    'Storm': r'\b(cyclone|hurricane|typhoon|tornado|severe storm|extreme rainfall)\b',
    'Wildfire': r'\b(wildfire|forest fire|bushfire)\b',
    'Landslide': r'\b(landslide|mudslide)\b',
    'Tsunami': r'\btsunami\b',
    'Heatwave': r'\b(heatwave|extreme heat)\b',
    'Industrial': r'\b(chemical leak|industrial explosion|factory fire)\b',
}
GUIDANCE = 'Check local emergency authorities for current instructions. This service supplements, not replaces, official emergency systems. Do not approach the reported incident.'


def distance(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2-p1)/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(math.radians(lon2-lon1)/2)**2
    return 6371 * 2 * math.asin(math.sqrt(min(1, a)))


def classify(text, magnitude=None):
    if magnitude is not None:
        return 'Earthquake', ('Critical' if magnitude >= 7 else 'High' if magnitude >= 5.5 else 'Medium' if magnitude >= 4 else 'Low')
    if re.search(r'\b(drill|simulation|fiction|video game|no (?:flood|earthquake)|human flood|mike flood)\b', text, re.I):
        return None, 'Low'
    for kind, pattern in PATTERNS.items():
        if re.search(pattern, text, re.I):
            severity = 'High' if re.search(r'\b(deadly|destroyed|severe|major)\b', text, re.I) else 'Medium'
            if re.search(r'\b(minor|small)\b', text, re.I):
                severity = 'Low'
            return kind, severity
    return None, 'Low'


def ingest(db, record):
    """Normalized internal record. Official trust is set by adapter, never public input."""
    text = str(record['text']).strip()[:10000]
    published = datetime.fromisoformat(record['published_at'].replace('Z', '+00:00'))
    if published.tzinfo is None or published > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError('invalid publication timestamp')
    lat, lon = record.get('lat'), record.get('lon')
    if (lat is None) != (lon is None) or (lat is not None and (not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180)):
        raise ValueError('invalid coordinates')
    mode = record.get('mode', 'live')
    if mode not in ('live', 'delayed', 'sample', 'simulated'):
        raise ValueError('invalid provenance')
    if mode == 'live' and datetime.now(timezone.utc) - published > timedelta(hours=6):
        mode = 'delayed'
    external_id = record.get('external_id') or hashlib.sha256(text.casefold().encode()).hexdigest()
    collected = now()
    cursor = db.execute('INSERT OR IGNORE INTO reports(source,external_id,url,text,location,lat,lon,published_at,collected_at,mode,official,raw,user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (record['source'], external_id, record.get('url'), text, record.get('location', 'Unknown'), lat, lon, published.isoformat(), collected, mode, int(record.get('official', False)), json.dumps(record), record.get('user_id')))
    if not cursor.rowcount:
        existing = db.execute('SELECT * FROM reports WHERE source=? AND external_id=?', (record['source'], external_id)).fetchone()
        changed = existing['text'] != text or existing['lat'] != lat or existing['lon'] != lon
        if not changed or not record.get('official'):
            return None
        db.execute('UPDATE reports SET text=?,lat=?,lon=?,raw=?,collected_at=? WHERE id=?', (text,lat,lon,json.dumps(record),collected,existing['id']))
        incident = db.execute('SELECT i.* FROM incidents i JOIN incident_reports ir ON ir.incident_id=i.id WHERE ir.report_id=?', (existing['id'],)).fetchone()
        if incident:
            kind, updated_severity = classify(text, record.get('magnitude'))
            # Preserve human review decisions while updating the supporting evidence.
            reviewed = db.execute('SELECT 1 FROM audit WHERE incident_id=?', (incident['id'],)).fetchone()
            db.execute('UPDATE incidents SET summary=?,lat=?,lon=?,severity=?,last_updated=?,version=version+1 WHERE id=?',
                (' '.join(text.split())[:400],lat,lon,incident['severity'] if reviewed else updated_severity,collected,incident['id']))
            db.execute('INSERT INTO timeline(incident_id,created_at,status,note) VALUES (?,?,?,?)', (incident['id'],collected,incident['verification'],'Official source revised the observation.'))
            return incident['id']
        return None
    report_id = cursor.lastrowid
    kind, severity = classify(text, record.get('magnitude') if record.get('official') else None)
    if not kind:
        return None
    verification = 'Officially Confirmed' if record.get('official') and mode not in ('sample', 'simulated') else 'Unverified'
    confidence = 'Official source' if verification == 'Officially Confirmed' else 'Limited evidence'
    # Never merge distinct official event IDs (nearby aftershocks are separate).
    match = None
    if lat is not None and not record.get('official'):
        candidates = db.execute("SELECT * FROM incidents WHERE type=? AND mode=? AND verification IN ('Unverified','Developing') ORDER BY id DESC LIMIT 100", (kind, mode)).fetchall()
        for candidate in candidates:
            if candidate['lat'] is not None and abs((published - datetime.fromisoformat(candidate['published_at'])).total_seconds()) < 3600 and distance(lat, lon, candidate['lat'], candidate['lon']) < 10:
                match = candidate
                break
    if match:
        incident_id = match['id']
        # New reports alone don't raise trust or severity and don't trigger a new alert version.
        db.execute('UPDATE incidents SET last_updated=? WHERE id=?', (collected, incident_id))
    else:
        cursor = db.execute('INSERT INTO incidents(type,location,lat,lon,severity,verification,confidence,mode,summary,first_detected,last_updated,published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (kind, record.get('location', 'Unknown'), lat, lon, severity, verification, confidence, mode, ' '.join(text.split())[:400], collected, collected, published.isoformat()))
        incident_id = cursor.lastrowid
        db.execute('INSERT INTO timeline(incident_id,created_at,status,note) VALUES (?,?,?,?)', (incident_id, collected, verification, 'Detected from '+record['source']+'. Severity is a screening estimate.'))
    db.execute('INSERT INTO incident_reports VALUES (?,?)', (incident_id, report_id))
    return incident_id


def qualify_notifications(db):
    current = datetime.now(timezone.utc)
    incidents = db.execute("SELECT * FROM incidents WHERE mode IN ('live','delayed') ORDER BY CASE severity WHEN 'Critical' THEN 0 WHEN 'High' THEN 1 ELSE 2 END").fetchall()
    for i in incidents:
        previous = {r[0] for r in db.execute('SELECT DISTINCT user_id FROM notifications WHERE incident_id=?', (i['id'],))}
        fresh = timedelta(0) <= current - datetime.fromisoformat(i['published_at']) <= timedelta(hours=6)
        eligible = fresh and i['lat'] is not None and i['verification'] in ('Corroborated', 'Officially Confirmed')
        if not eligible and not (previous and i['verification'] in ('Resolved', 'Disputed')):
            continue
        for user in db.execute('SELECT id FROM users').fetchall():
            locations = db.execute('SELECT * FROM locations WHERE user_id=?', (user['id'],)).fetchall()
            matches = [l for l in locations if eligible and LEVELS[i['severity']] >= LEVELS[l['min_severity']] and distance(i['lat'], i['lon'], l['lat'], l['lon']) <= l['radius']]
            if not matches and not (user['id'] in previous and (eligible or i['verification'] in ('Resolved', 'Disputed'))):
                continue
            # 20 noncritical notifications/hour/user. Critical and corrections bypass digest throttling.
            count = db.execute('SELECT count(*) FROM notifications WHERE user_id=? AND created_at>?', (user['id'], (current-timedelta(hours=1)).isoformat())).fetchone()[0]
            if count >= 20 and i['severity'] != 'Critical' and i['verification'] not in ('Resolved', 'Disputed'):
                continue
            stamp = now()
            message = f"{i['severity']} {i['type']} · {i['location']} · {i['verification']}. {GUIDANCE}"
            cur = db.execute('INSERT OR IGNORE INTO notifications(user_id,incident_id,version,message,created_at) VALUES (?,?,?,?,?)', (user['id'], i['id'], i['version'], message, stamp))
            if cur.rowcount:
                db.execute("INSERT INTO deliveries(notification_id,channel,status,next_attempt,sent_at) VALUES (?,'in-app','delivered',?,?)", (cur.lastrowid, stamp, stamp))
                if any(l['email_enabled'] for l in (matches or locations)):
                    db.execute("INSERT INTO deliveries(notification_id,channel,status,next_attempt) VALUES (?,'email','pending',?)", (cur.lastrowid, stamp))
