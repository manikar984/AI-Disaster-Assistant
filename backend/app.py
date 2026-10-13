import logging
import os
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, ConfigDict
from .db import HEARTBEAT_KEY, connect, get_runtime_state, migrate, now
from .security import SESSION_DAYS, current_user, admin_user, password_hash, password_ok, session_hash
from .engine import CATEGORIES, GUIDANCE, LEVELS, VERIFICATIONS, ingest, qualify_notifications
from .poller import source_rows, source_state_summary
from . import mailer
from .tokens import consume as consume_token, issue as issue_token
from .verification import RULE as CORROBORATION_RULE, assess as assess_evidence


@asynccontextmanager
async def lifespan(app):
    migrate()
    yield


app = FastAPI(title='Disaster Assistant', version='0.1.0', lifespan=lifespan)
limits = defaultdict(deque)


@app.middleware('http')
async def boundary(request: Request, call_next):
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        origin = request.headers.get('origin')
        allowed = os.getenv('APP_ORIGIN', 'http://localhost:5173')
        same_origin = origin in (allowed, str(request.base_url).rstrip('/'))
        # TLS may terminate at the preview/production reverse proxy.
        if origin:
            parsed = urlsplit(origin)
            same_origin = same_origin or (parsed.scheme in ('http', 'https') and parsed.netloc == request.headers.get('host') and parsed.path in ('', '/'))
        if origin and not same_origin:
            return JSONResponse({'detail': 'Origin not allowed'}, status_code=403)
        if request.headers.get('x-requested-with') != 'DisasterAssistant':
            return JSONResponse({'detail': 'Required request header missing'}, status_code=403)
        try:
            size = int(request.headers.get('content-length', '0'))
        except ValueError:
            return JSONResponse({'detail': 'Invalid content length'}, status_code=400)
        if size > 20000:
            return JSONResponse({'detail': 'Request too large'}, status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 20000:
                return JSONResponse({'detail': 'Request too large'}, status_code=413)
        request._body = bytes(body)
        key = (request.client.host if request.client else 'unknown', 'auth' if '/auth/' in request.url.path else 'write')
        stamp = time.monotonic()
        # Single-process pilot rate limiter; do not trust caller-supplied forwarded IP headers.
        for stale in list(limits):
            if not limits[stale] or stamp-limits[stale][-1] > 60:
                del limits[stale]
        entries = limits[key]
        while entries and stamp-entries[0] > 60:
            entries.popleft()
        if len(entries) >= (10 if key[1] == 'auth' else 60):
            return JSONResponse({'detail': 'Too many requests; retry in a minute'}, status_code=429, headers={'Retry-After': '60'})
        entries.append(stamp)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/api/') else 'no-cache'
    response.headers['Permissions-Policy'] = 'geolocation=(self)'
    return response


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Credentials(StrictModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class TokenOnly(StrictModel):
    token: str = Field(min_length=20, max_length=200)


class EmailOnly(StrictModel):
    email: EmailStr


class PasswordReset(StrictModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=12, max_length=128)


class Location(StrictModel):
    name: str = Field(min_length=2, max_length=100)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius: float = Field(ge=1, le=500)
    min_severity: Literal['Low', 'Medium', 'High', 'Critical'] = 'High'
    email_enabled: bool = False


class Report(StrictModel):
    text: str = Field(min_length=15, max_length=5000)
    location: str = Field(min_length=2, max_length=150)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


class Review(StrictModel):
    verification: Literal['Unverified', 'Developing', 'Corroborated', 'Disputed', 'Resolved']
    severity: Literal['Low', 'Medium', 'High', 'Critical']
    note: str = Field(min_length=15, max_length=2000)


def create_session(db, response, user_id):
    token = secrets.token_urlsafe(32)
    stamp = datetime.now(timezone.utc)
    db.execute('DELETE FROM sessions WHERE expires_at<?', (now(),))
    db.execute(
        'INSERT INTO sessions(token,user_id,expires_at,created_at,last_seen_at) VALUES (?,?,?,?,?)',
        (session_hash(token), user_id, (stamp + timedelta(days=SESSION_DAYS)).isoformat(), stamp.isoformat(), stamp.isoformat()),
    )
    response.set_cookie(
        'session', token, httponly=True,
        secure=os.getenv('COOKIE_SECURE', 'false').lower() == 'true',
        samesite='strict', max_age=int(timedelta(days=SESSION_DAYS).total_seconds()), path='/',
    )


def account_link(kind, token):
    """Absolute link for an emailed token; the browser page redeems it."""
    base = (os.getenv('PUBLIC_BASE_URL') or os.getenv('APP_ORIGIN') or 'http://localhost:5173').rstrip('/')
    return f'{base}/?{kind}={token}'


def deliver_token_email(kind, recipient, token):
    """Send a verification or recovery link. Never raises: email may be off."""
    try:
        if kind == 'verify':
            mailer.send_account_verification(recipient, account_link('verify', token))
        else:
            mailer.send_password_recovery(recipient, account_link('reset', token))
    except Exception:
        logging.getLogger('disaster-assistant').warning('account email not delivered: kind=%s reason=unavailable', kind)
        return 'unavailable'
    return 'sent'


def generic_reply():
    """Identical answer whether or not the address has an account."""
    return {'ok': True, 'message': 'If that address has an account, a message has been sent to it.'}


@app.get('/api/health')
def health():
    with connect() as db:
        db.execute('SELECT 1')
    return {'status': 'ok', 'version': '0.1.0'}


@app.post('/api/auth/register', status_code=201)
def register(data: Credentials, response: Response):
    email = str(data.email).lower()
    with connect() as db:
        try:
            cursor = db.execute('INSERT INTO users(email,password,created_at) VALUES (?,?,?)', (email, password_hash(data.password), now()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'Unable to register with these details')
        user_id = cursor.lastrowid
        create_session(db, response, user_id)
        db.execute('INSERT INTO audit(actor_id,action,note,created_at) VALUES (?,?,?,?)', (user_id, 'register', 'Account created; email awaiting confirmation', now()))
        token = issue_token(db, user_id, 'verify')
    delivery = deliver_token_email('verify', email, token)
    return {'ok': True, 'email_verified': False, 'verification_email': delivery}


@app.post('/api/auth/verify')
def verify_email(data: TokenOnly):
    """Redeem an emailed confirmation token. Single use, 24-hour validity."""
    with connect() as db:
        user_id = consume_token(db, 'verify', data.token)
        if user_id is None:
            raise HTTPException(400, 'That confirmation link is invalid or has expired')
        db.execute('UPDATE users SET email_verified=1, email_verified_at=? WHERE id=?', (now(), user_id))
        email = db.execute('SELECT email FROM users WHERE id=?', (user_id,)).fetchone()['email']
        db.execute('INSERT INTO audit(actor_id,action,note,created_at) VALUES (?,?,?,?)', (user_id, 'verify-email', 'Address confirmed by the account holder', now()))
    return {'ok': True, 'email': email, 'email_verified': True}


@app.post('/api/auth/resend-verification', status_code=202)
def resend_verification(data: EmailOnly):
    """Re-issue a confirmation link. The answer never reveals whether the account exists."""
    email = str(data.email).lower()
    with connect() as db:
        row = db.execute('SELECT id,email_verified FROM users WHERE email=?', (email,)).fetchone()
        if row and not row['email_verified']:
            token = issue_token(db, row['id'], 'verify')
    if row and not row['email_verified']:
        deliver_token_email('verify', email, token)
    return generic_reply()


@app.post('/api/auth/forgot', status_code=202)
def forgot_password(data: EmailOnly):
    """Start password recovery. Always the same answer, so accounts cannot be enumerated."""
    email = str(data.email).lower()
    with connect() as db:
        row = db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
        if row:
            token = issue_token(db, row['id'], 'reset')
    if row:
        deliver_token_email('reset', email, token)
    return generic_reply()


@app.post('/api/auth/reset')
def reset_password(data: PasswordReset):
    """Redeem a recovery token: single use, one hour, and signs out every device."""
    with connect() as db:
        user_id = consume_token(db, 'reset', data.token)
        if user_id is None:
            raise HTTPException(400, 'That recovery link is invalid or has expired')
        db.execute('UPDATE users SET password=? WHERE id=?', (password_hash(data.password), user_id))
        db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
        db.execute('INSERT INTO audit(actor_id,action,note,created_at) VALUES (?,?,?,?)', (user_id, 'reset-password', 'Password recovered with a single-use link; all sessions revoked', now()))
    return {'ok': True, 'message': 'Password updated. Sign in again on each device.'}


@app.get('/api/sessions')
def sessions(request: Request, user=Depends(current_user)):
    """This account's own sessions. Session tokens are never returned, only a prefix."""
    current = session_hash(request.cookies.get('session', ''))
    with connect() as db:
        rows = db.execute(
            'SELECT token,created_at,last_seen_at,expires_at FROM sessions WHERE user_id=? '
            'ORDER BY COALESCE(last_seen_at,created_at) DESC',
            (user['id'],),
        ).fetchall()
    return [
        {
            'id': row['token'][:12],
            'current': row['token'] == current,
            'created_at': row['created_at'],
            'last_seen_at': row['last_seen_at'],
            'expires_at': row['expires_at'],
        }
        for row in rows
    ]


@app.post('/api/auth/logout-all')
def logout_all(request: Request, response: Response, user=Depends(current_user)):
    """Revoke every session for this account, including the current one."""
    with connect() as db:
        removed = db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],)).rowcount
        db.execute('INSERT INTO audit(actor_id,action,note,created_at) VALUES (?,?,?,?)', (user['id'], 'logout-all', f'{removed} sessions revoked by the account holder', now()))
    response.delete_cookie('session', path='/')
    return {'ok': True, 'sessions_revoked': removed}


@app.post('/api/auth/login')
def login(data: Credentials, response: Response):
    with connect() as db:
        row = db.execute('SELECT * FROM users WHERE email=?', (str(data.email).lower(),)).fetchone()
        # Do equivalent expensive work for unknown users.
        stored = row['password'] if row else password_hash('dummy-password-not-a-user')
        valid = password_ok(data.password, stored)
        if not row or not valid:
            raise HTTPException(401, 'Invalid email or password')
        create_session(db, response, row['id'])
    return {'ok': True}


@app.post('/api/auth/logout')
def logout(request: Request, response: Response):
    with connect() as db:
        db.execute('DELETE FROM sessions WHERE token=?', (session_hash(request.cookies.get('session', '')),))
    response.delete_cookie('session', path='/')
    return {'ok': True}


@app.get('/api/me')
def me(user=Depends(current_user)):
    return user


@app.delete('/api/me')
def delete_me(response: Response, user=Depends(current_user)):
    with connect() as db:
        # Raw community payloads must not retain account identifiers after deletion.
        db.execute("UPDATE reports SET raw='{}' WHERE user_id=?", (user['id'],))
        db.execute('DELETE FROM users WHERE id=?', (user['id'],))
    response.delete_cookie('session', path='/')
    return {'ok': True}


@app.get('/api/locations')
def locations(user=Depends(current_user)):
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM locations WHERE user_id=?', (user['id'],))]


@app.post('/api/locations', status_code=201)
def add_location(data: Location, user=Depends(current_user)):
    with connect() as db:
        if db.execute('SELECT count(*) FROM locations WHERE user_id=?', (user['id'],)).fetchone()[0] >= 10:
            raise HTTPException(400, 'Maximum 10 saved locations')
        db.execute('INSERT INTO locations(user_id,name,lat,lon,radius,min_severity,email_enabled) VALUES (?,?,?,?,?,?,?)', (user['id'], data.name, data.lat, data.lon, data.radius, data.min_severity, data.email_enabled))
        qualify_notifications(db)
    return {'ok': True}


@app.delete('/api/locations/{location_id}')
def remove_location(location_id: int, user=Depends(current_user)):
    with connect() as db:
        if not db.execute('DELETE FROM locations WHERE id=? AND user_id=?', (location_id, user['id'])).rowcount:
            raise HTTPException(404, 'Location not found')
    return {'ok': True}


def _with_precision(row):
    """Never let a coordinate-less record claim a precise geographic position.

    Rows written before the geo_precision column existed carry the column default;
    they are corrected in migration 003 and guarded here so an API response can
    never label a place we do not actually have coordinates for.
    """
    item = dict(row)
    if item.get('lat') is None or item.get('lon') is None:
        item['geo_precision'] = 'unknown'
    return item


@app.get('/api/incidents')
def incidents(mode: Literal['live', 'sample', 'all'] = 'live', severity: str = '', type: str = '', q: str = ''):
    where, params = [], []
    if mode != 'all':
        where.append("i.mode IN ('live','delayed')" if mode == 'live' else "i.mode IN ('sample','simulated')")
    for column, value in [('severity', severity), ('type', type)]:
        if value:
            where.append(f'i.{column}=?')
            params.append(value)
    if q:
        where.append('(i.location LIKE ? OR i.summary LIKE ?)')
        params.extend(['%'+q[:150]+'%']*2)
    sql = 'SELECT i.*, (SELECT count(DISTINCT r.source) FROM incident_reports ir JOIN reports r ON r.id=ir.report_id WHERE ir.incident_id=i.id) source_count FROM incidents i'
    with connect() as db:
        return [_with_precision(r) for r in db.execute(sql + (' WHERE '+' AND '.join(where) if where else '')+' ORDER BY i.last_updated DESC LIMIT 200', params)]


@app.get('/api/incidents/{incident_id}')
def incident(incident_id: int):
    with connect() as db:
        row = db.execute('SELECT * FROM incidents WHERE id=?', (incident_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Incident not found')
        result = _with_precision(row)
        result['reports'] = [_with_precision(r) for r in db.execute('SELECT r.id,r.source,r.url,r.text,r.published_at,r.collected_at,r.mode,r.official,r.lat,r.lon,r.geo_precision FROM reports r JOIN incident_reports ir ON ir.report_id=r.id WHERE ir.incident_id=?', (incident_id,))]
        independent, reason, domains = assess_evidence(db, incident_id)
        result['evidence'] = {'independent': independent, 'reason': reason, 'domains': domains, 'rule': CORROBORATION_RULE}
        result['timeline'] = [dict(r) for r in db.execute('SELECT created_at,status,note FROM timeline WHERE incident_id=? ORDER BY id DESC', (incident_id,))]
        result['guidance'] = GUIDANCE
        result['guidance_url'] = 'https://www.ready.gov/'+('earthquakes' if row['type'] == 'Earthquake' else 'be-informed')
        return result


@app.get('/api/sources')
def sources():
    return source_rows()


@app.get('/api/ready')
def ready():
    """Readiness: database, worker liveness and source health (no secrets)."""
    problems = []
    try:
        with connect() as db:
            db.execute('SELECT 1')
        database = True
    except sqlite3.Error:
        database = False
        problems.append('database unavailable')
    heartbeat = get_runtime_state(HEARTBEAT_KEY)
    age = None
    if heartbeat:
        age = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat[1])).total_seconds()))
    worker = 'unknown' if age is None else ('alive' if age <= 90 else 'stalled')
    if worker == 'unknown':
        problems.append('worker has never reported a heartbeat')
    elif worker == 'stalled':
        problems.append(f'worker heartbeat is {age} seconds old')
    summary = source_state_summary()
    if not summary['enabled']:
        problems.append('no live source is enabled')
    if summary['error']:
        problems.append(f"{summary['error']} source(s) failing")
    if summary['stale']:
        problems.append(f"{summary['stale']} source(s) stale")
    return {
        'status': 'ready' if not problems else 'degraded',
        'version': '0.2.0',
        'problems': problems,
        'checks': {'database': database, 'worker': worker, 'worker_heartbeat_age_seconds': age},
        'sources': summary,
    }


@app.get('/api/policy')
def policy():
    """The documented decision policy, so it can be reviewed by users."""
    return {
        'categories': list(CATEGORIES),
        'severity_levels': list(LEVELS),
        'verification_states': list(VERIFICATIONS),
        'corroboration_rule': CORROBORATION_RULE,
        'severity_notes': [
            'Severity is a screening estimate, not a government warning level.',
            'The reporting source decides severity when it publishes one (CAP severity, GDACS alert level, USGS magnitude).',
            'Wording alone never exceeds High; report volume never raises severity.',
            'Only independent evidence changes the verification state.',
        ],
        'notification_gates': [
            'Live or delayed provenance (never sample/simulated)',
            'Published within the last 6 hours',
            'An established location',
            'Corroborated or officially confirmed verification',
            'Within the saved radius and above the chosen minimum severity',
            'Once per (user, incident, version); non-critical alerts capped per hour',
        ],
        'guidance': GUIDANCE,
        'disclaimer': (
            'Disaster Assistant is not an official emergency warning service. Always follow local '
            'emergency authorities and contact emergency services when in immediate danger.'
        ),
    }


@app.post('/api/reports', status_code=201)
def submit_report(data: Report, user=Depends(current_user)):
    if (data.lat is None) != (data.lon is None):
        raise HTTPException(422, 'Provide both coordinates or neither')
    with connect() as db:
        incident_id = ingest(db, dict(data.model_dump(), source='Community', published_at=now(), mode='live', official=False, user_id=user['id']))
    return {'incident_id': incident_id, 'message': 'Report recorded for review. It is not a verified alert.'}


@app.get('/api/notifications')
def notifications(user=Depends(current_user)):
    with connect() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100', (user['id'],))]
        for row in rows:
            row['deliveries'] = [dict(r) for r in db.execute('SELECT channel,status,attempts,sent_at,last_error FROM deliveries WHERE notification_id=?', (row['id'],))]
        return rows


@app.post('/api/notifications/{notification_id}/read')
def read_notification(notification_id: int, user=Depends(current_user)):
    with connect() as db:
        if not db.execute('UPDATE notifications SET read_at=? WHERE id=? AND user_id=?', (now(), notification_id, user['id'])).rowcount:
            raise HTTPException(404, 'Notification not found')
    return {'ok': True}


@app.get('/api/admin/overview')
def overview(user=Depends(admin_user)):
    with connect() as db:
        return {
            'runs': [dict(r) for r in db.execute('SELECT * FROM ingestion_runs ORDER BY id DESC LIMIT 50')],
            'deliveries': [dict(r) for r in db.execute('SELECT * FROM deliveries ORDER BY id DESC LIMIT 100')],
            'audit': [dict(r) for r in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 100')],
            'unclassified_reports': [dict(r) for r in db.execute('SELECT id,source,text,collected_at FROM reports WHERE id NOT IN (SELECT report_id FROM incident_reports) ORDER BY id DESC LIMIT 50')],
            'metrics': dict(db.execute("SELECT count(*) reports, avg((julianday(collected_at)-julianday(published_at))*86400) mean_source_latency_seconds FROM reports WHERE mode IN ('live','delayed')").fetchone()),
        }


@app.patch('/api/admin/incidents/{incident_id}')
def review(incident_id: int, data: Review, user=Depends(admin_user)):
    with connect() as db:
        cur = db.execute('UPDATE incidents SET verification=?,severity=?,confidence=?,last_updated=?,version=version+1 WHERE id=?', (data.verification, data.severity, 'Human reviewed (not a probability)', now(), incident_id))
        if not cur.rowcount:
            raise HTTPException(404, 'Incident not found')
        db.execute('INSERT INTO timeline(incident_id,created_at,status,note) VALUES (?,?,?,?)', (incident_id, now(), data.verification, data.note))
        db.execute('INSERT INTO audit(actor_id,action,incident_id,note,created_at) VALUES (?,?,?,?,?)', (user['id'], 'review', incident_id, data.note, now()))
        qualify_notifications(db)
    return {'ok': True}


DIST = Path(__file__).resolve().parent.parent / 'web' / 'dist'
if DIST.exists():
    app.mount('/assets', StaticFiles(directory=DIST/'assets'), name='assets')

    @app.get('/{path:path}')
    def frontend(path: str):
        if path.startswith('api/'):
            raise HTTPException(404, 'Not found')
        return FileResponse(DIST/'index.html')
