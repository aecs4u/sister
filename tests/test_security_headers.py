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
