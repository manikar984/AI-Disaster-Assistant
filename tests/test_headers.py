"""Security response headers: the policy, its exceptions and its coverage.

The point of these tests is not "a header exists" but that the policy is the one
the product can actually run under: scripts only from this origin, no framing, no
geolocation, and the map still allowed to fetch its tiles. Two of them read the
frontend source, so a change there that the policy does not cover fails here
instead of breaking the map in production.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import app, limits
from backend.security_headers import HEADERS, POLICY

REPO = Path(__file__).resolve().parent.parent
#: The header every state-changing request must carry (see the boundary middleware).
BROWSER_HEADERS = {'X-Requested-With': 'DisasterAssistant'}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'headers.db'))
    monkeypatch.setenv('USGS_ENABLED', 'false')
    limits.clear()
    with TestClient(app) as c:
        yield c


def directives(policy=POLICY):
    """Parse a CSP string into {directive: [source, ...]}."""
    parsed = {}
    for part in policy.split(';'):
        part = part.strip()
        if not part:
            continue
        name, _, sources = part.partition(' ')
        parsed[name] = sources.split()
    return parsed


def test_every_response_carries_the_headers(client):
    for path in ('/', '/api/health', '/api/policy', '/assets/index-missing.js', '/does-not-exist'):
        response = client.get(path)
        for name, value in HEADERS.items():
            assert response.headers.get(name) == value, f'{path} is missing {name}'


def test_the_headers_are_present_on_error_responses_too(client):
    """The middleware stack order, pinned: refusals are responses like any other.

    `boundary` returns 403/413/429 without ever reaching a route, so these are
    exactly the responses that lose their headers if the security-header
    middleware is registered on the wrong side of it.
    """
    bad_origin = client.post(
        '/api/auth/login', json={'email': 'a@example.com', 'password': 'x' * 12},
        headers={'Origin': 'https://evil.example', 'X-Requested-With': 'DisasterAssistant'},
    )
    assert bad_origin.status_code == 403
    missing_header = client.post('/api/auth/login', json={'email': 'a@example.com', 'password': 'x' * 12})
    assert missing_header.status_code == 403
    too_large = client.post('/api/auth/login', content=b'x' * 30_000, headers=BROWSER_HEADERS)
    assert too_large.status_code == 413
    unauthorized = client.get('/api/me', headers=BROWSER_HEADERS)
    assert unauthorized.status_code == 401
    for response in (bad_origin, missing_header, too_large, unauthorized):
        assert response.headers['Content-Security-Policy'] == POLICY
        assert response.headers['X-Frame-Options'] == 'DENY'


def test_rate_limited_responses_carry_the_headers(client):
    limits.clear()
    for _ in range(10):
        client.post('/api/auth/login', json={'email': 'a@example.com', 'password': 'x' * 12},
                    headers=BROWSER_HEADERS)
    limited = client.post('/api/auth/login', json={'email': 'a@example.com', 'password': 'x' * 12},
                          headers=BROWSER_HEADERS)
    assert limited.status_code == 429
    assert limited.headers['Content-Security-Policy'] == POLICY


def test_scripts_and_framing_are_locked_down():
    parsed = directives()
    assert parsed['default-src'] == ["'self'"]
    assert parsed['script-src'] == ["'self'"], 'the bundle is external, so no inline script is needed'
    assert "'unsafe-inline'" not in parsed['script-src']
    assert "'unsafe-eval'" not in parsed['script-src']
    assert "'unsafe-inline'" not in parsed['default-src']
    assert parsed['frame-ancestors'] == ["'none'"]
    assert parsed['object-src'] == ["'none'"]
    assert parsed['base-uri'] == ["'none'"]
    assert parsed['form-action'] == ["'self'"]
    assert parsed['connect-src'] == ["'self'"], 'the frontend only talks to its own origin'
    assert "'unsafe-inline'" not in parsed['connect-src']


def test_the_single_style_exception_is_documented_and_narrow():
    parsed = directives()
    # Stylesheets stay same-origin; only style *attributes* are allowed inline,
    # which is what Leaflet needs to place the map.
    assert parsed['style-src'] == ["'self'"]
    assert parsed['style-src-attr'] == ["'unsafe-inline'"]
    text = (REPO / 'backend' / 'security_headers.py').read_text()
    assert 'Leaflet' in text, 'the exception must be explained where the policy lives'


def test_no_page_permission_is_granted():
    """The app never asks the browser for a location, so nothing is granted."""
    permissions = HEADERS['Permissions-Policy']
    for feature in ('geolocation', 'camera', 'microphone', 'payment', 'usb'):
        assert f'{feature}=()' in permissions, f'{feature} must be denied, not merely absent'
        assert f'{feature}=(self' not in permissions
    source = (REPO / 'web' / 'src' / 'main.tsx').read_text()
    assert 'navigator.geolocation' not in source, 'the frontend would need geolocation in the policy'


def test_the_map_tiles_are_allowed_by_the_policy(client):
    """Read the tile host out of the frontend: a change there must fail here."""
    frontend = (REPO / 'web' / 'src' / 'main.tsx').read_text()
    hosts = set(re.findall(r"https://([a-z0-9.-]*tile\.openstreetmap\.org)", frontend))
    assert hosts, 'the frontend no longer references OpenStreetMap tiles'
    allowed = directives()['img-src']
    for host in hosts:
        assert f'https://{host}' in allowed, f'the tile host {host} is not allowed by img-src'
    assert 'data:' in allowed, 'Leaflet inlines its marker sprites as data URIs'
    # Nothing else is allowed to serve images to the page.
    external = [source for source in allowed if source.startswith('http')]
    assert external == [f'https://{host}' for host in sorted(hosts)], external


def test_the_built_page_has_no_inline_script_or_style_that_the_policy_would_block():
    """`script-src 'self'` is only safe because the bundle is external."""
    for candidate in (REPO / 'web' / 'dist' / 'index.html', REPO / 'web' / 'index.html'):
        if not candidate.exists():
            continue  # dist/ is a build output and absent on a fresh checkout
        html = candidate.read_text()
        assert '<script' in html, f'{candidate} no longer loads a bundle'
        for script in re.findall(r'<script\b[^>]*>', html):
            assert 'src=' in script, f'inline script in {candidate} would be blocked by CSP: {script}'
        assert '<style' not in html, f'inline style element in {candidate} would be blocked by CSP'


def test_the_proxy_carries_the_same_policy():
    """The proxy is what serves production traffic, so it must not drift."""
    caddy = (REPO / 'deploy' / 'Caddyfile').read_text()
    assert f'Content-Security-Policy "{POLICY}"' in caddy
    assert 'X-Content-Type-Options "nosniff"' in caddy
    assert 'Strict-Transport-Security' in caddy, 'HSTS is set by the proxy, which knows about TLS'
    assert 'frame-ancestors' in POLICY


def test_the_application_does_not_advertise_the_policy_as_report_only(client):
    response = client.get('/api/health')
    assert 'Content-Security-Policy-Report-Only' not in response.headers
    assert response.headers['Content-Security-Policy'] == POLICY
