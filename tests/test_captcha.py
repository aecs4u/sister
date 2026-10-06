"""Unit tests for CAPTCHA wait outcomes; no live browser or portal is used."""

import pytest

from sister.models import CaptchaRequired
from sister.utils import _wait_for_captcha


class _Locator:
    def __init__(self, count=0):
        self._count = count

    async def count(self):
        return self._count

    def first(self):
        return self

    async def wait_for(self, **_kwargs):
        raise TimeoutError("captcha remains visible")


class _Page:
    url = "https://sister3.agenziaentrate.gov.it/Visure/TipoVisura.do"

    def locator(self, selector):
        return _Locator(1 if selector == "input[name='inCaptchaChars']" else 0)

    async def wait_for_url(self, *_args, **_kwargs):
        raise TimeoutError("captcha was not submitted")


@pytest.mark.asyncio
async def test_native_captcha_timeout_is_a_human_action_state():
    with pytest.raises(CaptchaRequired, match="CAPTCHA SISTER non completato"):
        await _wait_for_captcha(_Page(), timeout=0)


@pytest.mark.asyncio
async def test_no_captcha_returns_false():
    class _Empty(_Page):
        def locator(self, selector):
            return _Locator(0)

    assert await _wait_for_captcha(_Empty(), timeout=0) is False


def test_captcha_required_message_keeps_a_stable_marker():
    assert str(CaptchaRequired("x")).startswith(CaptchaRequired.MARKER)
    assert str(CaptchaRequired()) == CaptchaRequired.MARKER


def test_response_status_maps_captcha_to_needs_human():
    from sister.models import VisuraResponse
    from sister.routes import _response_status

    def resp(**kw):
        return VisuraResponse(request_id="r", cadastre_type="F", **kw)

    assert _response_status(resp(success=True, data={"immobili": []})) == "completed"
    assert _response_status(resp(success=False, error="boom")) == "error"
    assert _response_status(resp(success=False, error=str(CaptchaRequired("t")))) == "needs_human"
    assert _response_status(resp(success=True, data={"needs_human": "CAPTCHA"})) == "needs_human"
