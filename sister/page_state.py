"""Deterministic page-state detection for SISTER portal transitions."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from .models import SelectorDrift


@dataclass(frozen=True)
class SisterPageState:
    name: str
    url: str


async def _count(page, selector: str) -> int:
    try:
        return await page.locator(selector).count()
    except Exception:
        return 0


async def detect_sister_page_state(page) -> SisterPageState:
    """Classify known login, query, result, CAPTCHA, and document page states."""
    url = str(getattr(page, "url", "") or "")
    path = urlsplit(url).path.lower()
    if "error_locked.jsp" in path or "utentegia" in path:
        return SisterPageState("locked_session", url)
    if "sceltaomonimi" in path:
        return SisterPageState("subject_selection", url)
    if "tipovisura" in path or "inoltrarichiesta" in path:
        return SisterPageState("document_request", url)

    body = ""
    try:
        body = (await page.locator("body").inner_text()).upper()
    except Exception:
        pass
    if "NESSUNA CORRISPONDENZA TROVATA" in body:
        return SisterPageState("no_match", url)
    for selector in (
        "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']", ".g-recaptcha", ".h-captcha",
        "input[name='inCaptchaChars']",
    ):
        if await _count(page, selector):
            return SisterPageState("captcha", url)
    if await _count(page, "select[name='listacom']"):
        return SisterPageState("office_selection", url)
    if await _count(page, "select[name='denomComune']") and await _count(page, "input[name='foglio']"):
        return SisterPageState("property_search_form", url)
    if await _count(page, "input[name='confAssSub'][value='Conferma']"):
        return SisterPageState("subalterno_confirmation", url)
    if await _count(page, "input[visImmSel], input[name='visImmSel'], table.listaIsp4"):
        return SisterPageState("property_results", url)
    try:
        if await page.get_by_role("link", name="Immobile").count():
            return SisterPageState("service_menu", url)
    except Exception:
        pass
    return SisterPageState("unknown", url)


async def require_sister_page_state(page, expected: set[str], *, page_logger=None, step: str) -> SisterPageState:
    """Require one known state and capture unexpected pages before stopping."""
    state = await detect_sister_page_state(page)
    if state.name not in expected:
        if page_logger is not None:
            await page_logger.log(page, f"selector_drift_{step}_{state.name}")
        expected_text = ", ".join(sorted(expected))
        raise SelectorDrift(
            f"Unexpected SISTER page state '{state.name}' at {state.url!r}; expected one of: {expected_text}"
        )
    return state
