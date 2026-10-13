"""Security response headers, defined once and applied to every response.

The policy is deliberately strict, and every relaxation is listed here with the
reason for it. `tests/test_headers.py` asserts the policy, the exceptions and the
fact that the proxy carries the same string.

What the browser can and cannot do with this policy:

* **Scripts** come only from this origin. The built frontend has no inline
  scripts (`web/index.html` loads one module bundle), so `script-src 'self'`
  works without a nonce or a hash, and no `'unsafe-eval'` is granted.
* **Styles** come only from this origin, with one exception: `style-src-attr
  'unsafe-inline'`. Leaflet positions map panes, markers and controls with inline
  `style` attributes, so blocking them would break the map. `style-src` itself
  stays `'self'`, so a `<style>` element injected by a hostile string is still
  refused. (Browsers that predate `style-src-attr` fall back to `style-src` and
  would refuse those attributes: the map would lay out incorrectly there. That
  is the accepted trade-off, and it is a browser-version issue, not a data one.)
* **Images** come from this origin, `data:` (Leaflet's marker sprites are
  inlined as data URIs in the built CSS) and the single map-tile host the
  frontend uses. `tests/test_headers.py` reads that host out of the frontend
  source, so changing the tile template without changing this policy fails the
  build rather than silently breaking the map.
* **Connections** are same-origin only: the frontend talks to `/api` on the same
  origin, and `connect-src 'self'` means a script that lands here cannot exfiltrate
  to another host.
* **Framing** is refused twice: `frame-ancestors 'none'` (CSP) and
  `X-Frame-Options: DENY` for the browsers that never implemented the former.
* **Forms** can only post to this origin, `base-uri` is `'none'` so an injected
  `<base>` cannot retarget relative URLs, and `object-src` is `'none'`.
* **No geolocation, camera, microphone, payment or USB** is exposed to the page.
  This application never asks the browser for a location: coordinates are typed
  or picked on the map.

Two things this module deliberately does **not** do:

* `Strict-Transport-Security` is set by the reverse proxy (`deploy/Caddyfile`),
  because only the proxy knows that the original request arrived over TLS.
* Unhandled-exception responses are produced by Starlette's outermost error
  middleware, outside anything a FastAPI application can wrap. A 500 from that
  path therefore carries the proxy's headers but not these; every response the
  application itself produces (including 401/403/404/413/429) does.
"""

#: The Content-Security-Policy value. Kept on one line so the same string can be
#: used verbatim in `deploy/Caddyfile`, where a test compares it byte for byte.
POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; "
    "img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; font-src 'self' data:; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

HEADERS = {
    'Content-Security-Policy': POLICY,
    # The policy above already refuses framing; this is for browsers that only
    # understand the header, not the directive.
    'X-Frame-Options': 'DENY',
    # The API answers JSON and never sniffs: a JSON body served as text/html
    # would otherwise be a script-injection surface.
    'X-Content-Type-Options': 'nosniff',
    # Cross-origin requests to this app carry the origin, not the full path.
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    # Nothing in the product needs these; denying them costs nothing and removes
    # a prompt-based phishing surface.
    'Permissions-Policy': 'geolocation=(), camera=(), microphone=(), payment=(), usb=(), magnetometer=(), gyroscope=()',
    # Our responses are for this origin only: no other site may embed them.
    'Cross-Origin-Resource-Policy': 'same-origin',
}


def apply(response):
    """Attach the headers to a response. Returns it, so it can wrap a return."""
    for name, value in HEADERS.items():
        response.headers[name] = value
    return response
