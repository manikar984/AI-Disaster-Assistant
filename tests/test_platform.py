import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient
from backend.app import app, limits
from backend.db import connect, migrate, now
from backend import db as backend_db
from backend.engine import classify, distance, ingest, qualify_notifications
from backend.security import password_hash, password_ok
from backend.worker import normalize_usgs, poll_usgs, deliver_email

HEADERS = {'X-Requested-With': 'DisasterAssistant'}

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path/'test.db'))
    monkeypatch.setenv('USGS_ENABLED', 'false')
    monkeypatch.delenv('SMTP_HOST', raising=False)
    limits.clear()
    with TestClient(app, headers=HEADERS) as c:
        yield c


def signup(c, email='person@example.com'):
    return c.post('/api/auth/register', json={'email': email, 'password': 'Strong-test-password-47'})


def record(**kwargs):
    return dict(dict(source='USGS', external_id='event-1', text='Magnitude 6 earthquake nearby', location='Test region', lat=10., lon=76., published_at=now(), mode='live', official=True, magnitude=6, url='https://earthquake.usgs.gov/earthquakes/eventpage/test'), **kwargs)


def subscribe(c, **kwargs):
    return c.post('/api/locations', json=dict(dict(name='Test home', lat=10., lon=76., radius=50, min_severity='High', email_enabled=False), **kwargs))


def test_health_migration_idempotency(client):
    migrate()
    assert client.get('/api/health').json()['status'] == 'ok'
    assert client.get('/api/incidents').json() == []


def test_registration_login_logout(client):
    response = signup(client)
    assert response.status_code == 201
    assert 'httponly' in response.headers['set-cookie'].lower()
    assert 'samesite=strict' in response.headers['set-cookie'].lower()
    assert client.get('/api/me').json()['role'] == 'user'
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/me').status_code == 401
    assert client.post('/api/auth/login', json={'email':'person@example.com','password':'Strong-test-password-47'}).status_code == 200


def test_password_hashing():
    a, b = password_hash('safe-password'), password_hash('safe-password')
    assert a != b and password_ok('safe-password', a) and not password_ok('wrong', a)


def test_bad_login_and_duplicate(client):
    signup(client)
    assert signup(client).status_code == 409
    assert client.post('/api/auth/login', json={'email':'missing@example.com','password':'invalid-long-password'}).status_code == 401


def test_authorization(client):
    assert client.get('/api/locations').status_code == 401
    signup(client)
    assert client.get('/api/admin/overview').status_code == 403
    assert client.patch('/api/admin/incidents/1',json={'verification':'Corroborated','severity':'High','note':'unsupported test review'}).status_code == 403


def test_origin_and_custom_header(client):
    assert client.post('/api/auth/logout', headers={'Origin':'https://evil.example'}).status_code == 403
    with TestClient(app) as other:
        assert other.post('/api/auth/logout').status_code == 403


def test_input_validation(client):
    assert client.post('/api/auth/register',json={'email':'bad','password':'short'}).status_code == 422
    signup(client)
    assert subscribe(client, lat=100).status_code == 422
    assert subscribe(client, radius=0).status_code == 422
    assert client.post('/api/reports',json={'text':'A major flood nearby','location':'Test','lat':10}).status_code == 422
    assert client.post('/api/reports',json={'text':'A major flood nearby','location':'Test','official':True}).status_code == 422


def test_private_location_isolation(client):
    signup(client)
    subscribe(client)
    first = client.get('/api/locations').json()[0]['id']
    client.post('/api/auth/logout')
    signup(client,'second@example.com')
    assert client.get('/api/locations').json() == []
    assert client.delete('/api/locations/'+str(first)).status_code == 404


def test_sample_separation_and_dedup(client):
    with connect() as db:
        first = ingest(db,record(mode='sample'))
        assert ingest(db,record(mode='sample')) is None
        assert db.execute('SELECT count(*) FROM reports').fetchone()[0] == 1
    assert client.get('/api/incidents').json() == []
    samples = client.get('/api/incidents?mode=sample').json()
    assert len(samples) == 1 and samples[0]['verification'] == 'Unverified'
    assert client.get(f'/api/incidents/{first}').json()['reports'][0]['mode'] == 'sample'


@pytest.mark.parametrize('text', ['I love this fire track', 'Mike Flood is a politician', 'Earthquake drill tomorrow', 'No flood in our district', 'Fire this coach'])
def test_false_positive_baseline(text):
    assert classify(text)[0] is None


@pytest.mark.parametrize('text,kind', [('A flash flood affected the town','Flood'),('Major wildfire destroyed homes','Wildfire'),('A tsunami was reported','Tsunami'),('Extreme heat continues','Heatwave'),('Chemical leak at the factory','Industrial')])
def test_classification(text,kind):
    assert classify(text)[0] == kind


def test_geography():
    assert distance(10,76,10,76) == 0
    assert 110 < distance(0,0,1,0) < 112
    assert distance(0,179.9,0,-179.9) < 23


def test_geographic_notification_and_suppression(client):
    signup(client)
    subscribe(client)
    with connect() as db:
        ingest(db,record())
        qualify_notifications(db)
        qualify_notifications(db)
    ns=client.get('/api/notifications').json()
    assert len(ns)==1 and ns[0]['deliveries'][0]['status']=='delivered'
    assert client.post(f"/api/notifications/{ns[0]['id']}/read").status_code==200
    assert client.get('/api/notifications').json()[0]['read_at']


@pytest.mark.parametrize('overrides', [dict(mode='sample'),dict(mode='simulated'),dict(official=False),dict(lat=None,lon=None),dict(lat=60),dict(magnitude=2),dict(published_at=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat())])
def test_alert_safety_gates(client,overrides):
    signup(client)
    subscribe(client)
    with connect() as db:
        ingest(db,record(**overrides));qualify_notifications(db)
    assert client.get('/api/notifications').json()==[]


def test_official_aftershocks_not_merged(client):
    with connect() as db:
        assert ingest(db,record()) != ingest(db,record(external_id='aftershock'))


def test_community_grouping_without_trust_upgrade(client):
    with connect() as db:
        a=ingest(db,record(source='Community',official=False,external_id='a'))
        b=ingest(db,record(source='Other',official=False,external_id='b'))
        assert a==b
        assert db.execute('SELECT verification FROM incidents WHERE id=?',(a,)).fetchone()[0]=='Unverified'


def test_admin_review_audit_and_resolution(client):
    signup(client);subscribe(client)
    with connect() as db:
        db.execute("UPDATE users SET role='admin'")
        id=ingest(db,record(official=False,source='Community'))
    response=client.patch(f'/api/admin/incidents/{id}',json={'verification':'Corroborated','severity':'High','note':'Reviewed independent sources in test fixture.'})
    assert response.status_code==200
    assert len(client.get('/api/notifications').json())==1
    client.patch(f'/api/admin/incidents/{id}',json={'verification':'Resolved','severity':'Low','note':'Resolution verified in controlled test fixture.'})
    assert len(client.get('/api/notifications').json())==2
    overview=client.get('/api/admin/overview').json()
    assert len(overview['audit'])==2


def test_account_deletion_cascades(client):
    signup(client);subscribe(client)
    client.post('/api/reports',json={'text':'Major flood near the town','location':'Test region'})
    assert client.delete('/api/me').status_code==200
    assert client.get('/api/me').status_code==401
    with connect() as db:
        assert db.execute('SELECT count(*) FROM users').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM locations').fetchone()[0]==0
        assert db.execute('SELECT user_id,raw FROM reports').fetchone()[:] == (None,'{}')


def test_normalization():
    r=normalize_usgs({'id':'abc','properties':{'type':'earthquake','mag':5.8,'time':datetime.now(timezone.utc).timestamp()*1000,'place':'Test','url':'https://earthquake.usgs.gov/event'},'geometry':{'coordinates':[76,10,5]}})
    assert r['official'] and r['lat']==10 and r['magnitude']==5.8
    with pytest.raises(ValueError):
        normalize_usgs({'id':'abc','properties':{'type':'earthquake','mag':4,'url':'https://evil.example'},'geometry':{'coordinates':[1,2]}})


def test_ingestion_failure_visible_no_fallback(client,monkeypatch):
    def fail(*a,**kw): raise TimeoutError('test timeout')
    monkeypatch.setattr('httpx.Client.get',fail)
    assert poll_usgs() is False
    with connect() as db:
        assert db.execute('SELECT status FROM sources').fetchone()[0]=='error'
        assert db.execute('SELECT count(*) FROM incidents').fetchone()[0]==0
        assert db.execute('SELECT error FROM ingestion_runs').fetchone()[0]=='TimeoutError'


def test_email_retry_not_fake_delivery(client):
    signup(client);subscribe(client,email_enabled=True)
    with connect() as db:
        ingest(db,record());qualify_notifications(db)
    deliver_email()
    with connect() as db:
        d=db.execute("SELECT * FROM deliveries WHERE channel='email'").fetchone()
        assert d['status']=='retry' and d['attempts']==1 and d['sent_at'] is None


def test_filter_and_unknown_location(client):
    with connect() as db:
        ingest(db,record(mode='sample',lat=None,lon=None))
    assert len(client.get('/api/incidents?mode=sample&type=Earthquake&q=Test&severity=High').json())==1
    assert client.get('/api/incidents?mode=sample&type=Flood').json()==[]
    assert client.get('/api/incidents/999').status_code==404


def test_auth_rate_limit(client):
    for _ in range(10):
        assert client.post('/api/auth/logout').status_code==200
    assert client.post('/api/auth/logout').status_code==429


def test_official_revision_and_alert_update(client):
    signup(client);subscribe(client)
    with connect() as db:
        id=ingest(db,record())
        qualify_notifications(db)
        assert ingest(db,record(text='Magnitude 7.2 earthquake revised',magnitude=7.2))==id
        qualify_notifications(db)
    assert len(client.get('/api/notifications').json())==2
    detail=client.get(f'/api/incidents/{id}').json()
    assert detail['severity']=='Critical' and detail['version']==2
    assert len(detail['timeline'])==2


def test_large_body_rejected(client):
    assert client.post('/api/reports',content='x'*20001).status_code==413


def test_notification_private_isolation(client):
    signup(client);subscribe(client)
    with connect() as db:
        ingest(db,record());qualify_notifications(db)
    id=client.get('/api/notifications').json()[0]['id']
    client.post('/api/auth/logout');signup(client,'other@example.com')
    assert client.get('/api/notifications').json()==[]
    assert client.post(f'/api/notifications/{id}/read').status_code==404


def test_email_cancelled_after_consent_removed(client):
    signup(client);subscribe(client,email_enabled=True)
    with connect() as db:
        ingest(db,record());qualify_notifications(db)
        db.execute('DELETE FROM locations')
    deliver_email()
    with connect() as db:
        assert db.execute("SELECT status FROM deliveries WHERE channel='email'").fetchone()[0]=='cancelled'


def test_mock_smtp_acceptance(client,monkeypatch):
    signup(client);subscribe(client,email_enabled=True)
    with connect() as db:
        ingest(db,record());qualify_notifications(db)
    monkeypatch.setenv('SMTP_HOST','smtp.example.com')
    monkeypatch.setenv('SMTP_FROM','alerts@example.com')
    sent=[]
    class FakeSMTP:
        # Exercises the mailer's SMTP control flow without a socket. The real
        # protocol conversation (STARTTLS, AUTH, DATA) is covered end to end in
        # tests/test_email.py against a real SMTP server.
        def __init__(self,*a,**k): pass
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def starttls(self,**k): pass
        def ehlo(self,*a,**k): return (250,b'ok')
        def noop(self): return (250,b'ok')
        def send_message(self,msg): sent.append(msg)
    monkeypatch.setattr('smtplib.SMTP',FakeSMTP)
    deliver_email()
    assert len(sent)==1
    with connect() as db:
        assert db.execute("SELECT status FROM deliveries WHERE channel='email'").fetchone()[0]=='accepted'


def test_successful_feed_skips_malformed(client,monkeypatch):
    import httpx
    feature={'id':'abc','properties':{'type':'earthquake','mag':5.8,'time':datetime.now(timezone.utc).timestamp()*1000,'place':'Test','url':'https://earthquake.usgs.gov/event'},'geometry':{'coordinates':[76,10,5]}}
    monkeypatch.setattr('httpx.Client.get',lambda *a,**k:httpx.Response(200,json={'features':[feature,{}]},request=httpx.Request('GET','https://earthquake.usgs.gov/')))
    assert poll_usgs()
    with connect() as db:
        assert db.execute('SELECT count(*) FROM incidents').fetchone()[0]==1
        assert db.execute('SELECT errors FROM ingestion_runs').fetchone()[0]==1
        assert db.execute('SELECT status FROM sources').fetchone()[0]=='degraded'


def test_legacy_rows_without_coordinates_are_corrected_on_upgrade(tmp_path, monkeypatch):
    """A database created before the precision column must not keep claiming 'point'."""
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path/'legacy.db'))
    monkeypatch.setenv('USGS_ENABLED', 'false')
    migrations = Path(backend_db.__file__).with_name('migrations')
    with connect() as db:
        db.executescript((migrations/'001_initial.sql').read_text())
        # A pre-M4 database records exactly this: schema 001 applied, nothing else.
        db.execute('CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY)')
        db.execute("INSERT INTO migrations VALUES ('001_initial.sql')")
        db.execute("INSERT INTO incidents(type,location,lat,lon,severity,verification,confidence,mode,summary,first_detected,last_updated,published_at) VALUES ('Earthquake','Unknown',NULL,NULL,'Medium','Unverified','Limited evidence','live','legacy row',?,?,?)", (now(), now(), now()))
        db.execute("INSERT INTO reports(source,external_id,text,location,lat,lon,published_at,collected_at,mode,official,raw) VALUES ('USGS','legacy-1','legacy text','Unknown',NULL,NULL,?,?,'live',1,'{}')", (now(), now()))
    migrate()
    with connect() as db:
        assert db.execute('SELECT geo_precision FROM incidents').fetchone()['geo_precision'] == 'unknown'
        assert db.execute('SELECT geo_precision FROM reports').fetchone()['geo_precision'] == 'unknown'


def test_incident_api_never_claims_precision_without_coordinates(client):
    """A stored 'point' with no coordinates must be reported as unknown, not as a place."""
    with connect() as db:
        incident_id = ingest(db, record(lat=None, lon=None, external_id='no-coords'))
        db.execute("UPDATE incidents SET geo_precision='point' WHERE id=?", (incident_id,))
        db.execute("UPDATE reports SET geo_precision='point' WHERE external_id='no-coords'")
    listed = client.get('/api/incidents').json()
    assert [row['geo_precision'] for row in listed] == ['unknown']
    detail = client.get(f'/api/incidents/{incident_id}').json()
    assert detail['geo_precision'] == 'unknown'
    assert detail['reports'][0]['geo_precision'] == 'unknown'
