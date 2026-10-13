"""Adapter, poller and corroboration tests.

Every feed here is served by a real local HTTP server, so the whole path runs
over the network stack: fetch → parse → normalize → ingest → source health.
The fixtures are clearly synthetic; no external host is contacted and nothing
is ever presented as a live incident.
"""

import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from backend.app import app, limits
from backend.db import HEARTBEAT_KEY, connect, migrate, set_runtime_state
from backend.engine import ingest
from backend.poller import poll_all, poll_target, source_rows, source_state_summary
from backend.sources import feed_targets, parse_named_feeds
from backend.sources.base import http_client
from backend.verification import assess

HEADERS = {'X-Requested-With': 'DisasterAssistant'}


class FixtureFeed:
    """Minimal HTTP server so adapter and poller tests use real sockets."""

    def __init__(self):
        self.routes = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                entry = owner.routes.get(self.path)
                if entry is None:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'not found')
                    return
                content_type, body = entry
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def add(self, path, body, content_type='application/xml'):
        self.routes[path] = (content_type, body.encode('utf-8') if isinstance(body, str) else body)
        return self

    def url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'sources.db'))
    monkeypatch.setenv('USGS_ENABLED', 'false')
    for name in ('NEWS_FEEDS', 'CAP_FEEDS', 'GDACS_FEED_URL', 'USGS_FEED_URL', 'GDACS_ENABLED'):
        monkeypatch.delenv(name, raising=False)
    limits.clear()
    migrate()
    return str(tmp_path / 'sources.db')


def recent(minutes=5):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def rfc822(minutes=5):
    return recent(minutes).strftime('%a, %d %b %Y %H:%M:%S GMT')


def usgs_feature(identifier='us7000test', magnitude=5.9, lat=36.2, lon=138.2, minutes=5):
    return {
        'id': identifier,
        'properties': {
            'type': 'earthquake',
            'mag': magnitude,
            'time': recent(minutes).timestamp() * 1000,
            'place': 'Test region, Japan',
            'url': f'https://earthquake.usgs.gov/earthquakes/eventpage/{identifier}',
            'status': 'reviewed',
        },
        'geometry': {'coordinates': [lon, lat, 10.0]},
    }


def usgs_collection(features):
    return json_dumps({'type': 'FeatureCollection', 'features': features})


def json_dumps(payload):
    import json

    return json.dumps(payload)


def gdacs_feed(entries):
    items = ''.join(
        f"""<item><title>{entry['title']}</title>
<description>{entry.get('description', 'Synthetic test alert')}</description>
<pubDate>{entry.get('pubdate', rfc822(10))}</pubDate>
<link>{entry.get('link', 'https://www.gdacs.org/report.aspx?eventid=1')}</link>
<geo:lat>{entry['lat']}</geo:lat><geo:long>{entry['lon']}</geo:long>
<gdacs:eventtype>{entry['type']}</gdacs:eventtype>
<gdacs:eventid>{entry['id']}</gdacs:eventid>
<gdacs:alertlevel>{entry['level']}</gdacs:alertlevel>
<gdacs:country>{entry.get('country', 'Testland')}</gdacs:country>{entry.get('severity', '')}</item>"""
        for entry in entries
    )
    return (
        '<?xml version="1.0"?><rss version="2.0" '
        f'xmlns:geo="http://www.georss.org/georss" xmlns:gdacs="http://www.gdacs.org">'
        f'<channel><title>GDACS synthetic fixture</title>{items}</channel></rss>'
    )


def news_feed(entries):
    items = ''.join(
        f"""<item><title>{entry['title']}</title>
<description>{entry.get('summary', 'Synthetic test summary')}</description>
<link>{entry['link']}</link><guid>{entry.get('guid', entry['link'])}</guid>
<pubDate>{entry.get('pubdate', rfc822(10))}</pubDate></item>"""
        for entry in entries
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Fixture</title>{items}</channel></rss>'


def cap_feed(alerts):
    return '<?xml version="1.0"?><alerts>' + ''.join(alerts) + '</alerts>'


def cap_alert(identifier='cap-1', event='Flood Warning', severity='Severe', certainty='Likely',
              instruction='Move to higher ground immediately.', circle='10.5,76.2 25', minutes=5,
              expires='2026-12-31T00:00:00+00:00'):
    return f"""<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
<identifier>{identifier}</identifier><sender>Test Alerting Authority</sender>
<sent>{recent(minutes).isoformat()}</sent><status>Actual</status><msgType>Alert</msgType>
<scope>Public</scope><info>
<category>Met</category><event>{event}</event><urgency>Immediate</urgency>
<severity>{severity}</severity><certainty>{certainty}</certainty>
<headline>{event} for the test district</headline>
<description>Synthetic CAP fixture used by the test suite.</description>
<instruction>{instruction}</instruction>
<area><areaDesc>Test district</areaDesc><circle>{circle}</circle></area>
</info><expires>{expires}</expires></alert>"""


def test_usgs_poll_end_to_end(database, monkeypatch):
    with FixtureFeed() as feed:
        feed.add('/usgs.json', usgs_collection([usgs_feature('a'), usgs_feature('b', lat=1.0, lon=103.0), {}]), 'application/json')
        monkeypatch.setenv('USGS_ENABLED', 'true')
        monkeypatch.setenv('USGS_FEED_URL', feed.url('/usgs.json'))
        results = poll_all()
    assert len(results) == 1
    result = results[0]
    assert result['ok'] and result['status'] == 'degraded'
    assert (result['fetched'], result['inserted'], result['skipped'], result['rejected']) == (3, 2, 0, 1)
    with connect() as db:
        incidents = db.execute('SELECT * FROM incidents ORDER BY id').fetchall()
        assert len(incidents) == 2
        assert all(row['type'] == 'Earthquake' for row in incidents)
        assert all(row['severity'] == 'High' for row in incidents)
        assert all(row['verification'] == 'Officially Confirmed' for row in incidents)
        assert all(row['geo_precision'] == 'point' for row in incidents)
        assert db.execute('SELECT count(*) FROM reports').fetchone()[0] == 2
        run = db.execute('SELECT * FROM ingestion_runs ORDER BY id DESC LIMIT 1').fetchone()
        assert (run['records'], run['errors'], run['skipped']) == (2, 1, 0)
        assert run['duration_ms'] is not None and run['finished_at']
        source = db.execute("SELECT * FROM sources WHERE id='usgs'").fetchone()
        assert (source['status'], source['records_total'], source['records_rejected']) == ('degraded', 2, 1)
        assert source['last_success'] and source['last_attempt'] and source['next_poll']
    rows = {row['id']: row for row in source_rows()}
    assert rows['usgs']['status'] == 'degraded'
    assert rows['usgs']['description'] and rows['usgs']['attribution']


def test_repoll_is_idempotent_and_revisions_update_the_incident(database, monkeypatch):
    with FixtureFeed() as feed:
        feed.add('/usgs.json', usgs_collection([usgs_feature('a')]), 'application/json')
        monkeypatch.setenv('USGS_ENABLED', 'true')
        monkeypatch.setenv('USGS_FEED_URL', feed.url('/usgs.json'))
        first = poll_all()[0]
        second = poll_all()[0]
        feed.add('/usgs.json', usgs_collection([usgs_feature('a', magnitude=7.4)]), 'application/json')
        third = poll_all()[0]
    assert (first['inserted'], second['inserted'], second['skipped']) == (1, 0, 1)
    assert (third['inserted'], third['skipped']) == (1, 0)
    with connect() as db:
        incidents = db.execute('SELECT * FROM incidents').fetchall()
        assert len(incidents) == 1
        assert incidents[0]['severity'] == 'Critical' and incidents[0]['version'] == 2
        assert db.execute('SELECT count(*) FROM reports').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM timeline').fetchone()[0] == 2


def test_gdacs_mapping_and_link_allowlist(database, monkeypatch):
    feed_body = gdacs_feed([
        {'title': 'Red tropical cyclone alert in Testland', 'type': 'TC', 'id': '11', 'level': 'Red',
         'lat': 15.0, 'lon': 80.0, 'link': 'https://www.gdacs.org/report.aspx?eventid=11'},
        {'title': 'Green flood alert in Testland', 'type': 'FL', 'id': '12', 'level': 'Green', 'lat': 16.0, 'lon': 81.0},
        {'title': 'Green drought alert in Testland', 'type': 'DR', 'id': '13', 'level': 'Green', 'lat': 17.0, 'lon': 82.0,
         'link': 'https://malicious.example/track'},
    ])
    with FixtureFeed() as feed:
        feed.add('/gdacs.xml', feed_body)
        monkeypatch.setenv('GDACS_ENABLED', 'true')
        monkeypatch.setenv('GDACS_FEED_URL', feed.url('/gdacs.xml'))
        results = poll_all()
    assert results[0]['inserted'] == 2 and results[0]['rejected'] == 1
    with connect() as db:
        storms = db.execute("SELECT * FROM incidents WHERE type='Storm'").fetchone()
        floods = db.execute("SELECT * FROM incidents WHERE type='Flood'").fetchone()
        assert storms['severity'] == 'Critical' and floods['severity'] == 'Medium'
        assert storms['verification'] == 'Officially Confirmed'
        # A foreign evidence link is dropped rather than stored or followed.
        third = db.execute("SELECT url FROM reports WHERE external_id LIKE 'DR-%'").fetchone()
        assert third is None
        cyclone = db.execute("SELECT url FROM reports WHERE external_id='TC-11'").fetchone()
        assert cyclone['url'].startswith('https://www.gdacs.org/')


def test_news_syndication_is_stored_once(database, monkeypatch):
    shared = 'Severe flooding reported across the same test valley'
    with FixtureFeed() as feed:
        feed.add('/news-a.xml', news_feed([{'title': shared, 'link': 'https://publisher.example/story-1'}]))
        feed.add('/news-b.xml', news_feed([{'title': shared, 'link': 'https://relay.example/copy'}]))
        feed.add('/news-c.xml', news_feed([{'title': 'Wildfire contained near Test Hill', 'link': 'https://publisher.example/story-2'}]))
        monkeypatch.setenv('NEWS_FEEDS', f"Publisher A|{feed.url('/news-a.xml')},Relay B|{feed.url('/news-b.xml')},Publisher C|{feed.url('/news-c.xml')}")
        results = poll_all()
    assert len(results) == 3
    assert sum(result['inserted'] for result in results) == 2
    assert sum(result['skipped'] for result in results) == 1
    with connect() as db:
        titles = [row['text'] for row in db.execute('SELECT text FROM reports ORDER BY id')]
        assert len(titles) == 2
        assert sum(shared in title for title in titles) == 1
        # News never arrives as an official record, and location is honest.
        official = db.execute('SELECT sum(official) FROM reports').fetchone()[0]
        assert official == 0
        incident = db.execute('SELECT * FROM incidents').fetchone()
        assert incident['verification'] == 'Unverified'
        assert incident['lat'] is None
        assert 'Not established' in incident['location']


def test_cap_certainty_severity_and_instruction(database, monkeypatch):
    with FixtureFeed() as feed:
        feed.add('/cap.xml', cap_feed([
            cap_alert('cap-1', 'Flood Warning', 'Severe', 'Likely', 'Move to higher ground immediately.'),
            cap_alert('cap-2', 'Heat Advisory', 'Moderate', 'Possible', 'Check on vulnerable neighbours.', circle='20.0,70.0 50'),
        ]))
        monkeypatch.setenv('CAP_FEEDS', f"Test Alerting Authority|{feed.url('/cap.xml')}")
        results = poll_all()
    assert results[0]['ok'] and results[0]['inserted'] == 2
    with connect() as db:
        confirmed = db.execute("SELECT * FROM incidents WHERE type='Flood'").fetchone()
        tentative = db.execute("SELECT * FROM incidents WHERE type='Heatwave'").fetchone()
        assert confirmed['verification'] == 'Officially Confirmed'
        assert confirmed['severity'] == 'High'
        assert tentative['verification'] == 'Developing'
        assert tentative['severity'] == 'Medium'
        assert tentative['geo_precision'] == 'alert-area-centre'
        assert (tentative['lat'], tentative['lon']) == (20.0, 70.0)
        evidence = db.execute("SELECT text FROM reports WHERE external_id='cap-test-alerting-authority:cap-1'").fetchone()
        assert 'Official instruction: Move to higher ground immediately.' in evidence['text']


def test_malformed_feed_fails_loudly_without_fallback(database, monkeypatch):
    with FixtureFeed() as feed:
        feed.add('/broken.xml', '<rss><channel><item><title>unterminated')
        monkeypatch.setenv('GDACS_ENABLED', 'true')
        monkeypatch.setenv('GDACS_FEED_URL', feed.url('/broken.xml'))
        result = poll_all()[0]
    assert result['ok'] is False and result['status'] == 'error'
    with connect() as db:
        assert db.execute('SELECT count(*) FROM incidents').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM reports').fetchone()[0] == 0
        source = db.execute("SELECT * FROM sources WHERE id='gdacs'").fetchone()
        assert source['status'] == 'error' and source['last_error'] == 'SourceError'
        run = db.execute("SELECT * FROM ingestion_runs WHERE source_id='gdacs'").fetchone()
        assert run['error'] == 'SourceError' and run['records'] == 0
    client = TestClient(app, headers=HEADERS)
    with client:
        assert client.get('/api/incidents').json() == []


def test_missing_feed_returns_http_error_status(database, monkeypatch):
    with FixtureFeed() as feed:
        monkeypatch.setenv('USGS_ENABLED', 'true')
        monkeypatch.setenv('USGS_FEED_URL', feed.url('/absent.json'))
        result = poll_all()[0]
    assert result['ok'] is False
    with connect() as db:
        assert db.execute("SELECT last_error FROM sources WHERE id='usgs'").fetchone()[0] == 'SourceError'


def test_corroboration_requires_independent_domains(database):
    def record(source, url, **kwargs):
        return dict(
            source=source,
            source_name=source,
            source_kind='news',
            external_id=f'{source}-1',
            url=url,
            text='Severe flooding reported in the test valley',
            location='Test valley',
            lat=10.0,
            lon=76.0,
            published_at=recent(20).isoformat(),
            mode='live',
            official=False,
            **kwargs,
        )

    with connect() as db:
        first = ingest(db, record('news-a', 'https://publisher-one.example/story'))
        # A relayed copy of the same publisher story is not independent evidence.
        assert ingest(db, record('news-b', 'https://publisher-one.example/copy')) == first
        independent, reason, domains = assess(db, first)
        assert independent is False and domains == ['publisher-one.example']
        assert db.execute('SELECT verification FROM incidents WHERE id=?', (first,)).fetchone()[0] == 'Unverified'
        # A second publisher is still not enough on its own.
        ingest(db, record('news-c', 'https://publisher-two.example/story'))
        assert assess(db, first)[0] is False
        assert db.execute('SELECT verification FROM incidents WHERE id=?', (first,)).fetchone()[0] == 'Unverified'
        # Three distinct evidence domains meet the documented rule.
        ingest(db, record('news-d', 'https://publisher-three.example/story'))
        incident = db.execute('SELECT * FROM incidents WHERE id=?', (first,)).fetchone()
        assert incident['verification'] == 'Corroborated'
        assert 'Independent evidence across 3 domains' in incident['corroboration_note']
        assert incident['version'] == 2
        latest = db.execute('SELECT status,note FROM timeline WHERE incident_id=? ORDER BY id DESC LIMIT 1', (first,)).fetchone()
        assert latest['status'] == 'Corroborated' and 'independent evidence' in latest['note'].lower()


def test_official_tentative_alert_can_be_corroborated(database):
    with connect() as db:
        alert = ingest(db, dict(
            source='cap-test', source_name='CAP test authority', source_kind='official',
            external_id='cap-test:1', url='https://alerts.authority.example/alerts/1',
            text='Flood warning for the test district', location='Test district',
            lat=10.0, lon=76.0, published_at=recent(20).isoformat(), mode='live',
            official=True, category='Flood', verification_hint='Developing',
        ))
        assert db.execute('SELECT verification FROM incidents WHERE id=?', (alert,)).fetchone()[0] == 'Developing'
        ingest(db, dict(
            source='news-a', source_name='News A', source_kind='news', external_id='news-a-9',
            url='https://publisher.example/story', text='Flood warning for the test district',
            location='Test district', lat=10.05, lon=76.05, published_at=recent(18).isoformat(),
            mode='live', official=False,
        ))
        incident = db.execute('SELECT * FROM incidents WHERE id=?', (alert,)).fetchone()
        assert incident['verification'] == 'Corroborated'
    # A single report, however emphatic, never corroborates itself.
    with connect() as db:
        lone = ingest(db, dict(
            source='news-b', source_name='News B', source_kind='news', external_id='news-b-1',
            url='https://solo.example/story', text='Massive flood destroys everything',
            location='Elsewhere', lat=1.0, lon=1.0, published_at=recent(10).isoformat(),
            mode='live', official=False,
        ))
        assert db.execute('SELECT verification FROM incidents WHERE id=?', (lone,)).fetchone()[0] == 'Unverified'


def test_source_health_marks_stale_and_disabled(database, monkeypatch):
    with FixtureFeed() as feed:
        feed.add('/usgs.json', usgs_collection([usgs_feature('a')]), 'application/json')
        monkeypatch.setenv('USGS_ENABLED', 'true')
        monkeypatch.setenv('USGS_FEED_URL', feed.url('/usgs.json'))
        poll_all()
    with connect() as db:
        db.execute("UPDATE sources SET last_success=? WHERE id='usgs'", ((recent(120)).isoformat(),))
    rows = {row['id']: row for row in source_rows()}
    assert rows['usgs']['status'] == 'stale'
    assert rows['gdacs']['status'] == 'disabled' and rows['gdacs']['enabled'] is False
    summary = source_state_summary()
    assert summary['stale'] == 1 and summary['disabled'] >= 1 and summary['enabled'] == 1


def test_readiness_reports_worker_and_sources(database, monkeypatch):
    monkeypatch.setenv('USGS_ENABLED', 'true')
    with FixtureFeed() as feed:
        feed.add('/usgs.json', usgs_collection([usgs_feature('a')]), 'application/json')
        monkeypatch.setenv('USGS_FEED_URL', feed.url('/usgs.json'))
        poll_all()
    client = TestClient(app, headers=HEADERS)
    with client:
        before = client.get('/api/ready').json()
        assert before['status'] == 'degraded'
        assert any('heartbeat' in problem for problem in before['problems'])
        assert before['checks']['database'] is True
        with connect() as db:
            set_runtime_state(db, HEARTBEAT_KEY, {'pid': 1, 'status': 'alive'})
        after = client.get('/api/ready').json()
        assert after['status'] == 'ready', after['problems']
        assert after['sources']['enabled'] == 1
        assert after['checks']['worker'] == 'alive'
        listing = client.get('/api/sources').json()
        assert {row['id'] for row in listing} >= {'usgs', 'gdacs'}
        usgs_row = next(row for row in listing if row['id'] == 'usgs')
        assert usgs_row['kind'] == 'official' and usgs_row['records_total'] == 1
        assert 'feed_host' not in usgs_row  # configuration URLs are not exposed


def test_policy_endpoint_documents_decisions(database):
    client = TestClient(app, headers=HEADERS)
    with client:
        policy = client.get('/api/policy').json()
    assert 'Corroborated' in policy['verification_states']
    assert 'distinct evidence domains' in policy['corroboration_rule']
    assert any('6 hours' in gate for gate in policy['notification_gates'])
    assert 'Volcano' in policy['categories']


def test_registry_configuration(monkeypatch):
    for name in ('USGS_ENABLED', 'GDACS_ENABLED', 'NEWS_ENABLED', 'CAP_ENABLED'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv('NEWS_FEEDS', raising=False)
    monkeypatch.delenv('CAP_FEEDS', raising=False)
    assert feed_targets() == []
    monkeypatch.setenv('USGS_ENABLED', 'true')
    monkeypatch.setenv('GDACS_ENABLED', 'yes')
    monkeypatch.setenv('NEWS_FEEDS', 'Publisher|https://news.example/feed.xml,broken entry')
    monkeypatch.setenv('CAP_ENABLED', 'false')
    monkeypatch.setenv('CAP_FEEDS', 'Authority|https://alerts.example/cap.xml')
    ids = [target.id for target in feed_targets()]
    assert ids == ['usgs', 'gdacs', 'news-publisher']
    targets = {target.id: target for target in feed_targets()}
    assert targets['news-publisher'].kind == 'news' and targets['news-publisher'].official is False
    assert targets['usgs'].official is True
    assert parse_named_feeds('A|https://a.example/f,B|not-a-url,C') == [('A', 'https://a.example/f')]
