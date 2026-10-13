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
from .db import connect, migrate, now
from .security import current_user, admin_user, password_hash, password_ok, session_hash
from .engine import ingest, qualify_notifications, GUIDANCE


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
    db.execute('DELETE FROM sessions WHERE expires_at<?', (now(),))
    db.execute('INSERT INTO sessions VALUES (?,?,?)', (session_hash(token), user_id, (datetime.now(timezone.utc)+timedelta(days=7)).isoformat()))
    response.set_cookie('session', token, httponly=True, secure=os.getenv('COOKIE_SECURE', 'false').lower() == 'true', samesite='strict', max_age=604800, path='/')


@app.get('/api/health')
def health():
    with connect() as db:
        db.execute('SELECT 1')
    return {'status': 'ok', 'version': '0.1.0'}


@app.post('/api/auth/register', status_code=201)
def register(data: Credentials, response: Response):
    with connect() as db:
        try:
            cursor = db.execute('INSERT INTO users(email,password,created_at) VALUES (?,?,?)', (str(data.email).lower(), password_hash(data.password), now()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'Unable to register with these details')
        create_session(db, response, cursor.lastrowid)
    return {'ok': True}


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
        return [dict(r) for r in db.execute(sql + (' WHERE '+' AND '.join(where) if where else '')+' ORDER BY i.last_updated DESC LIMIT 200', params)]


@app.get('/api/incidents/{incident_id}')
def incident(incident_id: int):
    with connect() as db:
        row = db.execute('SELECT * FROM incidents WHERE id=?', (incident_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Incident not found')
        result = dict(row)
        result['reports'] = [dict(r) for r in db.execute('SELECT r.id,r.source,r.url,r.text,r.published_at,r.collected_at,r.mode,r.official FROM reports r JOIN incident_reports ir ON ir.report_id=r.id WHERE ir.incident_id=?', (incident_id,))]
        result['timeline'] = [dict(r) for r in db.execute('SELECT created_at,status,note FROM timeline WHERE incident_id=? ORDER BY id DESC', (incident_id,))]
        result['guidance'] = GUIDANCE
        result['guidance_url'] = 'https://www.ready.gov/'+('earthquakes' if row['type'] == 'Earthquake' else 'be-informed')
        return result


@app.get('/api/sources')
def sources():
    with connect() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM sources')]
    for row in rows:
        if not row['enabled']:
            row['status'] = 'disabled'
        elif row['last_success'] and datetime.now(timezone.utc)-datetime.fromisoformat(row['last_success']) > timedelta(minutes=10):
            row['status'] = 'stale'
    return rows


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
