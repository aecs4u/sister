from sister.security import strip_unsafe_eval


def test_unsafe_eval_is_removed_everywhere():
    csp = (
        "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: https://cdn.jsdelivr.net; "
        "img-src 'self'"
    )
    out = strip_unsafe_eval(csp)
    assert "'unsafe-eval'" not in out
    assert "script-src 'self' 'unsafe-inline' blob: https://cdn.jsdelivr.net" in out
    assert out.startswith("default-src 'self'")


def test_other_directives_are_untouched():
    assert strip_unsafe_eval("frame-ancestors 'self'; base-uri 'self'") == "frame-ancestors 'self'; base-uri 'self'"


from sister.security import CspNonceMiddleware, apply_nonce_policy  # noqa: E402

PACKAGE_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: https://cdn.jsdelivr.net https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' data: https://fonts.gstatic.com; "
    "connect-src 'self' https://api.clerk.dev; img-src 'self' data: https:; frame-ancestors 'self'"
)


def test_nonce_policy_requires_the_nonce_for_scripts():
    out = apply_nonce_policy(PACKAGE_CSP, "abc123")
    script = next(d for d in out.split("; ") if d.startswith("script-src"))
    assert script == "script-src 'self' 'nonce-abc123' blob:"
    assert "'unsafe-inline'" not in script and "'unsafe-eval'" not in script and "cdn" not in script


def test_nonce_policy_drops_third_party_hosts_but_keeps_inline_styles():
    out = apply_nonce_policy(PACKAGE_CSP, "n")
    assert "style-src 'self' 'unsafe-inline'" in out
    assert "font-src 'self' data:" in out
    assert "connect-src 'self'" in out
    assert "googleapis" not in out and "gstatic" not in out and "clerk" not in out


def test_nonce_policy_keeps_unrelated_directives_and_adds_missing_ones():
    out = apply_nonce_policy("default-src 'self'; frame-ancestors 'self'", "n")
    assert "frame-ancestors 'self'" in out and "default-src 'self'" in out
    assert "script-src 'self' 'nonce-n' blob:" in out


def test_middleware_sets_a_fresh_nonce_per_request_and_skips_docs():
    from fastapi import FastAPI, Request
    from starlette.responses import PlainTextResponse
    from starlette.testclient import TestClient

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/page")
    async def page(request: Request):
        return PlainTextResponse(request.state.csp_nonce, headers={"Content-Security-Policy": PACKAGE_CSP})

    @app.get("/docs")
    async def docs():
        return PlainTextResponse("docs", headers={"Content-Security-Policy": PACKAGE_CSP})

    app.add_middleware(CspNonceMiddleware)
    client = TestClient(app)
    first, second = client.get("/page"), client.get("/page")
    assert first.text != second.text
    assert f"'nonce-{first.text}'" in first.headers["content-security-policy"]
    assert client.get("/docs").headers["content-security-policy"] == PACKAGE_CSP


def test_swagger_ui_adds_italian_accessible_shell_and_local_theme_assets():
    import asyncio

    from sister.main import app

    route = next(route for route in app.routes if getattr(route, "path", None) == "/docs")
    response = asyncio.run(route.endpoint(_user=object()))
    html = response.body.decode("utf-8")

    assert '<html lang="it">' in html
    assert '<nav aria-label="Navigazione SISTER">' in html
    assert '<main id="main-content"' in html
    assert 'href="/static/css/swagger_ui.css?v=20261030c"' in html
    assert 'src="/static/js/swagger_ui.js?v=20261030c"' in html
    assert 'id="sister-swagger-theme" type="button" aria-pressed="false"' in html
    assert '</main>\n    <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js">' in html

    from pathlib import Path

    css = (Path(__file__).parents[1] / "sister/static/css/swagger_ui.css").read_text()
    assert '#swagger-ui [class^="json-schema-2020-12"]' in css
    assert "#swagger-ui .json-schema-2020-12-expand-deep-button" in css
    assert "#swagger-ui .info a .url" in css


def test_root_security_metadata_and_app_icons_are_served(main_module):
    import json

    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)

    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert robots.text == "User-agent: *\nDisallow: /\n"

    security = client.get("/.well-known/security.txt")
    assert security.status_code == 200
    fields = dict(line.split(": ", 1) for line in security.text.splitlines())
    assert fields["Contact"] == "http://testserver/contact"
    assert fields["Preferred-Languages"] == "it, en"
    assert fields["Expires"].endswith("Z")
    assert security.headers["cache-control"] == "public, max-age=86400"

    manifest = client.get("/static/site.webmanifest")
    assert manifest.status_code == 200
    data = json.loads(manifest.text)
    assert data["name"].startswith("SISTER")
    assert data["icons"][0]["src"] == "/static/icons/apple-touch-icon.png"
    icon = client.get(data["icons"][0]["src"])
    assert icon.status_code == 200 and icon.content.startswith(b"\x89PNG\r\n\x1a\n")

    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 200 and favicon.content[:4] == b"\x00\x00\x01\x00"
    for path in ("/contact", "/landing", "/auth/login"):
        page = client.get(path)
        assert page.status_code == 200
        assert 'rel="manifest" href="/static/site.webmanifest"' in page.text
        assert 'rel="apple-touch-icon" sizes="180x180"' in page.text
        assert 'rel="shortcut icon" type="image/x-icon" href="/static/icons/favicon.ico"' in page.text
        assert "sister_font_preload.js?v=20261030a" in page.text


def test_static_cache_headers_and_gzip_follow_asset_versioning(tmp_path):
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.gzip import GZipMiddleware
    from starlette.testclient import TestClient

    from sister.main import StaticCacheMiddleware

    static = tmp_path / "static"
    assets = {
        "vendor/bootstrap-5.3.2/css/bootstrap.min.css": "body{color:#123456}\n" * 180,
        "vendor/fonts/fonts.css": "@font-face{font-family:test}\n" * 80,
        "aecs4u-theme/css/theme.css": "body{color:#123456}\n" * 80,
        "js/sister_actions.js": "window.sisterActions = {};\n" * 120,
    }
    for relative_path, content in assets.items():
        path = static / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    app = FastAPI()
    app.mount("/static", StaticFiles(directory=static), name="static")
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(StaticCacheMiddleware)
    client = TestClient(app)

    vendor = client.get("/static/vendor/bootstrap-5.3.2/css/bootstrap.min.css")
    assert vendor.status_code == 200
    assert vendor.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert vendor.headers["content-encoding"] == "gzip"
    assert vendor.text == assets["vendor/bootstrap-5.3.2/css/bootstrap.min.css"]

    theme = client.get("/static/aecs4u-theme/css/theme.css?v=20261030a")
    assert theme.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert theme.headers["content-encoding"] == "gzip"

    unversioned_vendor = client.get("/static/vendor/fonts/fonts.css")
    assert unversioned_vendor.headers["cache-control"] == "no-cache"

    sister_asset = client.get("/static/js/sister_actions.js?v=20261030a")
    assert sister_asset.headers["cache-control"] == "no-cache"
    assert sister_asset.headers.get("etag")
    revalidated = client.get(
        "/static/js/sister_actions.js?v=20261030a",
        headers={"If-None-Match": sister_asset.headers["etag"]},
    )
    assert revalidated.status_code == 304
    assert revalidated.headers["cache-control"] == "no-cache"


def test_server_errors_are_generic_and_include_a_request_id():
    from fastapi import FastAPI, HTTPException
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.responses import HTMLResponse
    from starlette.testclient import TestClient

    from sister.main import _http_exception_handler, _unhandled_exception_handler

    class _Theme:
        def render(self, template_name, _request, **context):
            return HTMLResponse(
                f"{template_name}:{context['status_code']}:{context.get('request_id') or ''}"
            )

    app = FastAPI()
    app.state.theme_setup = _Theme()
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)

    @app.get("/unexpected")
    async def unexpected():
        raise RuntimeError("sensitive internal detail")

    @app.get("/api/explicit")
    async def explicit_api_error():
        raise HTTPException(status_code=500, detail="sensitive explicit detail")

    client = TestClient(app, raise_server_exceptions=False)
    html = client.get("/unexpected", headers={"Accept": "text/html"})
    assert html.status_code == 500
    assert html.text.startswith("error.html:500:")
    assert html.headers["x-request-id"] in html.text
    assert "sensitive internal detail" not in html.text
    assert html.headers["cache-control"] == "no-store"

    api = client.get("/api/explicit", headers={"Accept": "application/json"})
    assert api.status_code == 500
    assert api.json() == {"detail": "Internal Server Error", "request_id": api.headers["x-request-id"]}
    assert "sensitive explicit detail" not in api.text


def _cookie_app(**kwargs):
    from fastapi import FastAPI
    from starlette.responses import PlainTextResponse

    from sister.security import SecureCookieMiddleware

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/login")
    async def login():
        response = PlainTextResponse("ok")
        response.set_cookie("aecs4u_session", "jwt", httponly=True, samesite="lax")
        response.set_cookie("cookie_consent", "all")
        return response

    app.add_middleware(SecureCookieMiddleware, cookie_names=("aecs4u_session",), **kwargs)
    return app


def _set_cookies(response):
    return {c.split("=", 1)[0]: c for c in response.headers.get_list("set-cookie")}


def test_session_cookie_is_secure_over_https():
    from starlette.testclient import TestClient

    cookies = _set_cookies(TestClient(_cookie_app(), base_url="https://example.test").get("/login"))
    assert "Secure" in cookies["aecs4u_session"] and "HttpOnly" in cookies["aecs4u_session"]
    assert "Secure" not in cookies["cookie_consent"]  # only the session cookie is touched


def test_session_cookie_is_not_secure_over_plain_http():
    from starlette.testclient import TestClient

    cookies = _set_cookies(TestClient(_cookie_app(), base_url="http://localhost").get("/login"))
    assert "Secure" not in cookies["aecs4u_session"]


def test_force_adds_secure_even_over_http_and_never_twice():
    from starlette.testclient import TestClient

    cookies = _set_cookies(TestClient(_cookie_app(force=True), base_url="http://localhost").get("/login"))
    assert cookies["aecs4u_session"].lower().count("secure") == 1


def test_google_3d_sources_are_added_only_on_the_3d_page():
    from fastapi import FastAPI
    from starlette.responses import PlainTextResponse
    from starlette.testclient import TestClient

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/3d")
    async def three_d():
        return PlainTextResponse("3d", headers={"Content-Security-Policy": PACKAGE_CSP})

    @app.get("/other")
    async def other():
        return PlainTextResponse("x", headers={"Content-Security-Policy": PACKAGE_CSP})

    app.add_middleware(CspNonceMiddleware)
    client = TestClient(app)
    page = client.get("/3d").headers["content-security-policy"]
    assert "https://maps.googleapis.com" in next(d for d in page.split("; ") if d.startswith("script-src"))
    assert "'unsafe-inline'" not in next(d for d in page.split("; ") if d.startswith("script-src"))
    assert "connect-src 'self' https://*.googleapis.com" in page and "worker-src blob:" in page
    assert "googleapis" not in client.get("/other").headers["content-security-policy"]


def test_clerk_sources_are_added_only_on_the_clerk_auth_pages():
    from fastapi import FastAPI
    from starlette.responses import PlainTextResponse
    from starlette.testclient import TestClient

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    for path in ("/auth/login", "/auth/clerk/callback", "/auth/forgot-password", "/other"):

        def make(body=path):
            async def view():
                return PlainTextResponse(body, headers={"Content-Security-Policy": PACKAGE_CSP})

            return view

        app.get(path)(make())

    app.add_middleware(CspNonceMiddleware)
    client = TestClient(app)
    for path in ("/auth/login", "/auth/clerk/callback"):
        csp = client.get(path).headers["content-security-policy"]
        script = next(d for d in csp.split("; ") if d.startswith("script-src"))
        assert "https://cdn.jsdelivr.net" in script and "'unsafe-inline'" not in script
        assert "https://*.clerk.accounts.dev" in next(d for d in csp.split("; ") if d.startswith("connect-src"))
    for path in ("/auth/forgot-password", "/other"):
        assert "jsdelivr" not in client.get(path).headers["content-security-policy"]
