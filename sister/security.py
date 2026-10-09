"""Content-Security-Policy handling for the web UI (per-request script nonce).

``aecs4u-auth`` emits one static, permissive CSP (``'unsafe-inline'`` and ``'unsafe-eval'`` for scripts, many CDN
hosts).
For application pages this middleware generates a nonce per request, exposes it as ``request.state.csp_nonce`` (the
theme and SISTER's templates put it on their inline ``<script>`` tags) and rewrites the policy so that scripts need that
nonce. Interactive API docs (Swagger UI / ReDoc, third-party bundles loaded from CDNs) keep the package policy.
"""

from __future__ import annotations

import secrets

# Swagger UI / ReDoc are third-party bundles that need the relaxed policy.
_DOCS_PREFIXES = ("/docs", "/redoc", "/openapi.json")

# Everything the application pages need is served from 'self' (assets are vendored, see sister/static/vendor).
# style-src keeps 'unsafe-inline': templates use style="" attributes (low risk, unlike scripts).
_STRICT_DIRECTIVES = {
    "style-src": "'self' 'unsafe-inline'",
    "font-src": "'self' data:",
    "connect-src": "'self'",
}


def strip_unsafe_eval(csp: str) -> str:
    """Remove ``'unsafe-eval'`` from every directive of a Content-Security-Policy value."""
    directives = []
    for directive in csp.split(";"):
        tokens = [token for token in directive.split() if token != "'unsafe-eval'"]
        if tokens:
            directives.append(" ".join(tokens))
    return "; ".join(directives)


def apply_nonce_policy(csp: str, nonce: str) -> str:
    """Rewrite *csp* so scripts need *nonce*: no ``'unsafe-inline'``/``'unsafe-eval'``, no third-party script hosts."""
    replacements = {"script-src": f"'self' 'nonce-{nonce}' blob:", **_STRICT_DIRECTIVES}
    directives = []
    seen = set()
    for directive in csp.split(";"):
        tokens = directive.split()
        if not tokens:
            continue
        name = tokens[0].lower()
        if name in replacements:
            directives.append(f"{tokens[0]} {replacements[name]}")
            seen.add(name)
        else:
            directives.append(" ".join(tokens))
    for name, value in replacements.items():
        if name not in seen:
            directives.append(f"{name} {value}")
    return "; ".join(directives)


class CspNonceMiddleware:
    """ASGI middleware: per-request script nonce + nonce-based CSP for application pages."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith(_DOCS_PREFIXES):
            return await self.app(scope, receive, send)

        nonce = secrets.token_urlsafe(16)
        scope.setdefault("state", {})["csp_nonce"] = nonce

        async def send_with_policy(message):
            if message["type"] == "http.response.start":
                headers = []
                for name, value in message.get("headers", []):
                    if name.lower() == b"content-security-policy":
                        value = apply_nonce_policy(value.decode("latin-1"), nonce).encode("latin-1")
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_policy)


# Backwards-compatible name used by earlier code/tests.
CspTighteningMiddleware = CspNonceMiddleware


class SecureCookieMiddleware:
    """Add the ``Secure`` attribute to the session cookie whenever the request arrived over HTTPS.

    ``Secure`` must not be set on plain-HTTP deployments (browsers would drop the cookie and nobody could sign in), so
    it follows the request: ``scope["scheme"] == "https"`` (uvicorn derives it from ``X-Forwarded-Proto`` when
    ``--proxy-headers`` is enabled and the proxy is trusted). Set ``force=True`` (env ``SISTER_COOKIE_SECURE=always``)
    to add it unconditionally, e.g. behind a TLS-terminating proxy that does not forward the protocol.
    """

    def __init__(self, app, cookie_names: tuple[str, ...], force: bool = False) -> None:
        self.app = app
        self.cookie_names = tuple(name.lower() for name in cookie_names)
        self.force = force

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not (self.force or scope.get("scheme") == "https"):
            return await self.app(scope, receive, send)

        async def send_secure(message):
            if message["type"] == "http.response.start":
                headers = []
                for name, value in message.get("headers", []):
                    if name.lower() == b"set-cookie":
                        text = value.decode("latin-1")
                        cookie_name = text.split("=", 1)[0].strip().lower()
                        attrs = [part.strip().lower() for part in text.split(";")[1:]]
                        if cookie_name in self.cookie_names and "secure" not in attrs:
                            value = (text + "; Secure").encode("latin-1")
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_secure)
