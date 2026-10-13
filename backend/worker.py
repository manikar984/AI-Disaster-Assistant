"""Run exactly one worker. Feed failures never load sample data."""
import json
import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from datetime import datetime, timezone, timedelta
import httpx
from .db import connect, migrate, now
from .engine import ingest, qualify_notifications

log = logging.getLogger('worker')
USGS_URL = 'https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson'


def normalize_usgs(feature):
    p = feature['properties']
    coordinates = feature['geometry']['coordinates']
    if p.get('type') != 'earthquake' or p.get('mag') is None:
        raise ValueError('not a measured earthquake')
    magnitude = float(p['mag'])
    if not -2 <= magnitude <= 10:
        raise ValueError('invalid magnitude')
    url = p.get('url', '')
    if not url.startswith('https://earthquake.usgs.gov/'):
        raise ValueError('invalid evidence URL')
    return dict(source='USGS', external_id=feature['id'], url=url,
        text=f"Magnitude {magnitude:g} earthquake — {p.get('place') or 'Location not named'}",
        location=p.get('place') or 'Location not named', lat=float(coordinates[1]), lon=float(coordinates[0]),
        published_at=datetime.fromtimestamp(p['time']/1000, timezone.utc).isoformat(),
        mode='live', official=True, magnitude=magnitude)


def poll_usgs():
    with connect() as db:
        run_id = db.execute("INSERT INTO ingestion_runs(source_id,started_at) VALUES ('usgs',?)", (now(),)).lastrowid
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            response = client.get(USGS_URL, headers={'User-Agent': 'DisasterAssistant/0.1 (public-safety research)'})
            response.raise_for_status()
            if len(response.content) > 10_000_000:
                raise ValueError('feed exceeds size limit')
            features = response.json()['features']
        if not isinstance(features, list):
            raise ValueError('malformed feature collection')
        count, errors = 0, 0
        with connect() as db:
            for f in features[:5000]:
                try:
                    db.execute('SAVEPOINT record')
                    ingest(db, normalize_usgs(f))
                    db.execute('RELEASE record')
                    count += 1
                except (ValueError, KeyError, TypeError, IndexError, OverflowError):
                    db.execute('ROLLBACK TO record')
                    db.execute('RELEASE record')
                    errors += 1
            db.execute("UPDATE sources SET status=?,last_success=?,last_error=? WHERE id='usgs'", ('degraded' if errors else 'healthy', now(), f'{errors} malformed records' if errors else None))
            db.execute('UPDATE ingestion_runs SET finished_at=?,records=?,errors=? WHERE id=?', (now(), count, errors, run_id))
        return True
    except Exception as exc:
        # Never log provider response bodies or credentials.
        error = type(exc).__name__
        with connect() as db:
            db.execute("UPDATE sources SET status='error',last_error=? WHERE id='usgs'", (error,))
            db.execute('UPDATE ingestion_runs SET finished_at=?,error=? WHERE id=?', (now(), error, run_id))
        log.warning(json.dumps({'event': 'ingestion_failed', 'source': 'usgs', 'error': error}))
        return False


def deliver_email():
    with connect() as db:
        items = db.execute("SELECT d.*,n.message,n.user_id,n.version AS alert_version,i.version AS current_version,i.verification,i.published_at,u.email FROM deliveries d JOIN notifications n ON n.id=d.notification_id JOIN users u ON u.id=n.user_id JOIN incidents i ON i.id=n.incident_id WHERE d.channel='email' AND d.status IN ('pending','retry') AND d.next_attempt<=? ORDER BY CASE i.severity WHEN 'Critical' THEN 0 ELSE 1 END,d.id LIMIT 50", (now(),)).fetchall()
    for item in items:
        with connect() as db:
            consent = db.execute('SELECT 1 FROM locations WHERE user_id=? AND email_enabled=1', (item['user_id'],)).fetchone()
            stale = datetime.now(timezone.utc)-datetime.fromisoformat(item['published_at']) > timedelta(hours=6) and item['verification'] not in ('Resolved', 'Disputed')
            if not consent or stale or item['alert_version'] != item['current_version']:
                db.execute("UPDATE deliveries SET status='cancelled' WHERE id=?", (item['id'],))
                continue
        try:
            if not os.getenv('SMTP_HOST') or not os.getenv('SMTP_FROM'):
                raise RuntimeError('SMTP not configured')
            msg = EmailMessage()
            msg['Subject'] = 'Disaster Assistant — incident update (not a government warning)'
            msg['From'] = os.environ['SMTP_FROM']
            msg['To'] = item['email']
            msg['Message-ID'] = f"<notification-{item['notification_id']}@disaster-assistant.local>"
            msg.set_content(item['message'])
            with smtplib.SMTP(os.environ['SMTP_HOST'], int(os.getenv('SMTP_PORT', '587')), timeout=20) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if os.getenv('SMTP_USER'):
                    smtp.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
                smtp.send_message(msg)
            status, error, sent = 'accepted', None, now()
        except Exception as exc:
            status = 'failed' if item['attempts'] >= 4 else 'retry'
            error, sent = type(exc).__name__, None
        with connect() as db:
            db.execute('UPDATE deliveries SET status=?,attempts=attempts+1,last_error=?,sent_at=?,next_attempt=? WHERE id=?',
                (status, error, sent, (datetime.now(timezone.utc)+timedelta(seconds=min(3600, 60*2**item['attempts']))).isoformat(), item['id']))


def main():
    logging.basicConfig(level=logging.INFO)
    migrate()
    failures, next_poll = 0, 0
    while True:
        if os.getenv('USGS_ENABLED', 'false').lower() == 'true' and time.monotonic() >= next_poll:
            failures = 0 if poll_usgs() else failures + 1
            next_poll = time.monotonic() + min(3600, max(60, int(os.getenv('POLL_SECONDS', '60'))) * 2**min(failures, 5))
        try:
            with connect() as db:
                qualify_notifications(db)
            deliver_email()
        except Exception as exc:
            log.error(json.dumps({'event': 'worker_cycle_failed', 'error': type(exc).__name__}))
        time.sleep(5)


if __name__ == '__main__':
    main()
