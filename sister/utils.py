import asyncio
import contextlib
import contextvars
import logging
import os
import re
import time
from datetime import datetime

from aecs4u_auth.browser import PageLogger as _BasePageLogger
from bs4 import BeautifulSoup
from playwright.async_api import Page

from .models import CaptchaRequired
from .result_parsers import parse_intestato_value
from .query_forms import get_query_form

log = logging.getLogger("sister.utils")


class PageLogger(_BasePageLogger):
    """Extended PageLogger that saves screenshots and collects page visit metadata."""

    def __init__(self, flow_name: str, base_dir: str = "logs/pages") -> None:
        super().__init__(flow_name, base_dir)
        self.page_visits: list[dict] = []

    async def log(self, page: Page, step_name: str) -> None:
        await super().log(page, step_name)
        screenshot_url = None
        try:
            from .database import OUTPUTS_DIR

            if page and not page.is_closed():
                pages_dir = os.path.join(OUTPUTS_DIR, "pages", _BasePageLogger._session_id or "unknown")
                os.makedirs(pages_dir, exist_ok=True)
                safe_name = re.sub(r"[^\w\-]", "_", step_name)
                filename = f"{self.step:02d}_{self.flow_name}_{safe_name}.png"
                filepath = os.path.join(pages_dir, filename)
                await asyncio.wait_for(page.screenshot(path=filepath, full_page=True), timeout=5)
                screenshot_url = f"/outputs/pages/{_BasePageLogger._session_id or 'unknown'}/{filename}"
        except Exception as e:
            log.debug("Screenshot save failed: %s", e)

        # Collect page metadata — only extract form elements on form_compilato steps
        try:
            if page and not page.is_closed():
                form_elements = []
                errors = []
                if "form_compilato" in step_name:
                    try:
                        visit = await asyncio.wait_for(
                            _collect_page_metadata(page, step_name, screenshot_url), timeout=5
                        )
                        form_elements = visit.get("form_elements", [])
                        errors = visit.get("errors", [])
                    except Exception:
                        pass
                self.page_visits.append(
                    {
                        "step": step_name,
                        "url": page.url,
                        "timestamp": datetime.now().isoformat(),
                        "screenshot_url": screenshot_url,
                        "form_elements": form_elements,
                        "errors": errors,
                    }
                )
        except Exception:
            pass


async def _collect_page_metadata(page: Page, step_name: str, screenshot_url: str | None = None) -> dict:
    """Extract form elements and metadata from the current page."""
    url = page.url
    form_elements = []

    try:
        # Extract all visible form inputs, selects, textareas
        elements = await page.evaluate("""() => {
            const els = [];
            const forms = document.querySelectorAll('form');
            for (const form of forms) {
                const formName = form.getAttribute('name') || form.getAttribute('id') || '';
                if (formName === 'formricerca') continue;  // skip search bar

                for (const el of form.elements) {
                    if (el.type === 'hidden') continue;
                    if (el.type === 'radio' && !el.checked) continue;
                    const tag = el.tagName.toLowerCase();
                    const entry = {
                        tag: tag,
                        type: el.type || '',
                        name: el.name || '',
                        label: '',
                        value: '',
                    };
                    // Get value
                    if (tag === 'select') {
                        const opt = el.options[el.selectedIndex];
                        entry.value = opt ? opt.text.trim() + ' (' + opt.value + ')' : '';
                    } else if (el.type === 'radio' || el.type === 'checkbox') {
                        entry.value = el.checked ? el.value + ' [checked]' : el.value;
                    } else if (el.type === 'submit') {
                        entry.value = el.value;
                    } else {
                        entry.value = el.value || '';
                    }
                    // Find associated label
                    const id = el.id || el.name;
                    if (id) {
                        const lbl = document.querySelector('label[for="' + id + '"]');
                        if (lbl) entry.label = lbl.textContent.trim();
                    }
                    if (!entry.label) {
                        const td = el.closest('td');
                        if (td && td.previousElementSibling) {
                            const prevLabel = td.previousElementSibling.querySelector('label');
                            if (prevLabel) entry.label = prevLabel.textContent.trim();
                        }
                    }
                    els.push(entry);
                }
            }
            return els;
        }""")
        form_elements = elements or []
    except Exception as e:
        log.debug("Form element extraction failed: %s", e)

    # Check for error messages on page
    errors = []
    try:
        error_divs = page.locator(".errore_txt, .errore, .alert-danger, .error")
        count = await error_divs.count()
        for i in range(min(count, 5)):
            txt = (await error_divs.nth(i).inner_text()).strip()
            if txt:
                errors.append(txt)
    except Exception:
        pass

    return {
        "step": step_name,
        "url": url,
        "timestamp": datetime.now().isoformat(),
        "screenshot_url": screenshot_url,
        "form_elements": form_elements,
        "errors": errors,
    }


SISTER_SCELTA_SERVIZIO_URL = "https://sister3.agenziaentrate.gov.it/Visure/SceltaServizio.do?tipo=/T/TM/VCVC_"


ADE_AREA_PERSONALE_URL = "https://telematici.agenziaentrate.gov.it/Main/SceltaServizio.do"


async def _navigate_to_scelta_servizio(page: Page, page_logger: PageLogger, max_retries: int = 3) -> None:
    """Navigate to SceltaServizio.do, retrying if we land on login.jsp (session handoff delay).

    If the SISTER session isn't established (login.jsp), falls back to the ADE portal
    service selection flow to re-establish the SSO federation.
    """
    for attempt in range(1, max_retries + 1):
        await page.goto(SISTER_SCELTA_SERVIZIO_URL, timeout=60000)
        await page.wait_for_load_state("networkidle", timeout=30000)

        current_url = page.url
        if "SceltaServizio.do" in current_url:
            provincia_count = await page.locator("select[name='listacom'] option").count()
            if provincia_count > 1:
                await page_logger.log(page, "scelta_servizio")
                log.info("SceltaServizio raggiunta (%d province)", provincia_count - 1)
                return

        if "login.jsp" in current_url:
            log.warning(
                "Sessione SISTER non pronta (login.jsp), tentativo %d/%d — navigando via portale ADE...",
                attempt,
                max_retries,
            )
            await page_logger.log(page, f"login_jsp_tentativo_{attempt}")

            # Navigate through the ADE portal to establish SISTER SSO
            try:
                await page.goto(ADE_AREA_PERSONALE_URL, timeout=60000)
                await page.wait_for_load_state("networkidle", timeout=30000)
                await page_logger.log(page, f"ade_portal_{attempt}")

                # Search for SISTER and click "Vai al servizio"
                search_box = page.get_by_role("textbox", name="Cerca il servizio")
                if await search_box.count() > 0:
                    await search_box.click()
                    await search_box.fill("SISTER")
                    await search_box.press("Enter")
                    await page.wait_for_load_state("networkidle", timeout=15000)

                    vai_link = page.get_by_role("link", name="Vai al servizio").first
                    if await vai_link.count() > 0:
                        await vai_link.click()
                        await page.wait_for_load_state("networkidle", timeout=30000)
                        await page_logger.log(page, f"sister_via_ade_{attempt}")

                        # Check for session lock
                        content = await page.content()
                        if "Utente gia' in sessione" in content:
                            raise Exception("Utente già in sessione su un'altra postazione")

                        # Try navigating through Conferma -> Consultazioni -> Visure -> Conferma Lettura
                        for label, role, name in [
                            ("conferma", "button", "Conferma"),
                            ("consultazioni", "link", "Consultazioni e Certificazioni"),
                            ("visure_catastali", "link", "Visure catastali"),
                            ("conferma_lettura", "link", "Conferma Lettura"),
                        ]:
                            try:
                                locator = page.get_by_role(role, name=name)
                                if await locator.count() > 0:
                                    await locator.click(timeout=10000)
                                    await page.wait_for_load_state("networkidle", timeout=15000)
                                    log.debug("ADE navigation: %s", label)
                            except Exception:
                                log.debug("ADE navigation skip: %s", label)

                        # Verify we landed on SceltaServizio
                        if "SceltaServizio.do" in page.url:
                            provincia_count = await page.locator("select[name='listacom'] option").count()
                            if provincia_count > 1:
                                await page_logger.log(page, "scelta_servizio")
                                log.info("SceltaServizio raggiunta via ADE (%d province)", provincia_count - 1)
                                return

                        # If not, try one more direct navigation (SSO might be active now)
                        await page.goto(SISTER_SCELTA_SERVIZIO_URL, timeout=60000)
                        await page.wait_for_load_state("networkidle", timeout=30000)

                        if "SceltaServizio.do" in page.url:
                            provincia_count = await page.locator("select[name='listacom'] option").count()
                            if provincia_count > 1:
                                await page_logger.log(page, "scelta_servizio")
                                log.info(
                                    "SceltaServizio raggiunta dopo ADE redirect (%d province)", provincia_count - 1
                                )
                                return

            except Exception as e:
                if "Utente già in sessione" in str(e):
                    raise
                log.warning("Navigazione via ADE fallita: %s", e)

            if attempt < max_retries:
                await asyncio.sleep(3)
                continue

        await page_logger.log(page, f"scelta_servizio_fallita_{attempt}")
        raise Exception(f"Sessione scaduta o errore caricamento pagina - URL: {page.url}")


def parse_table(html):
    """Rows of a SISTER result table as dicts keyed by column header.

    The unnamed leading column holds the row's radio button: it is dropped when blank, and the radio's ``value``
    (which identifies the row on the portal, e.g. ``visImmSel`` with the catasto comune code) is kept under the
    radio's ``name``. Short rows are padded with empty cells.
    """
    soup = BeautifulSoup(html, "html.parser")
    headers = [th.get_text(strip=True) for th in soup.find_all("th")]
    rows = []
    for tr in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if cells:
            # Se ci sono meno celle che header, aggiungi celle vuote
            while len(cells) < len(headers):
                cells.append("")
            row = {key: value for key, value in zip(headers, cells) if key or value}
            for control in tr.find_all("input", attrs={"type": ["radio", "checkbox"]}):
                name, value = control.get("name") or control.get("property"), control.get("value")
                if name and value and name not in row:
                    row[name] = value
            rows.append(row)
    return rows


async def _comune_selector(page) -> str:
    """CSS selector of the comune dropdown: current SISTER forms use ``comuneCat``, older ones ``denomComune``."""
    if await page.locator("select[name='comuneCat']").count() > 0:
        return "select[name='comuneCat']"
    return "select[name='denomComune']"


async def find_best_option_match(page, selector, search_text):
    """Trova l'opzione che meglio corrisponde al testo cercato"""
    options = await page.locator(f"{selector} option").all()
    best_match = None
    best_score = 0

    log.debug("Cerco '%s' tra %d opzioni", search_text, len(options))

    for option in options:
        value = await option.get_attribute("value")
        text = await option.inner_text()

        if not value or not text:
            continue

        # Calcola similarity score
        search_upper = search_text.upper()
        text_upper = text.upper()
        value_upper = value.upper()

        # PRIORITÀ 1: Exact match del valore (per sezioni come P, Q, etc.)
        if search_upper == value_upper:
            log.debug("Exact value match: '%s' -> '%s'", text, value)
            return value

        # PRIORITÀ 2: Exact match del testo
        if search_upper == text_upper:
            log.debug("Exact text match: '%s' -> '%s'", text, value)
            return value

        # PRIORITÀ 2b: sigla provincia: il value e' "PALERMO Territorio-PA" (il testo solo "PALERMO Territorio");
        # senza questa "PA" sceglierebbe "PARMA Territorio" (testo piu' corto che inizia con "PA")
        if len(search_upper) == 2 and value_upper.endswith(f" TERRITORIO-{search_upper}"):
            log.debug("Sigla provincia match: '%s' -> '%s'", text, value)
            return value

        # PRIORITÀ 3: Match che inizia con il testo cercato
        if text_upper.startswith(search_upper):
            score = len(search_text) / len(text)
            if score > best_score:
                best_score = score
                best_match = value
                log.debug("Candidato starts_with: '%s' -> '%s' (%.2f)", text, value, score)

        # PRIORITÀ 4: Value che inizia con il testo cercato
        elif value_upper.startswith(search_upper):
            score = len(search_text) / len(value) * 0.9  # Leggera penalità
            if score > best_score:
                best_score = score
                best_match = value
                log.debug("Candidato value_starts_with: '%s' -> '%s' (%.2f)", text, value, score)

        # PRIORITÀ 5: Match che contiene il testo cercato
        elif search_upper in text_upper:
            score = len(search_text) / len(text) * 0.6  # Maggiore penalità per evitare falsi positivi
            if score > best_score:
                best_score = score
                best_match = value
                log.debug("Candidato contains: '%s' -> '%s' (%.2f)", text, value, score)

    if best_match:
        log.debug("Migliore match: '%s' (score: %.2f)", best_match, best_score)
    else:
        log.warning("Nessun match trovato per '%s'", search_text)
    return best_match


_MAX_CAPTCHA_ATTEMPTS = 5


async def _wait_for_captcha(page, timeout: int = 120):
    """Detect and wait for the user to solve a CAPTCHA if present.

    Handles two types:
    1. SISTER-native CAPTCHA: img#imgCaptcha + input[name='inCaptchaChars']
       → waits for the user to fill the code and submit (page navigates away)
    2. Generic reCAPTCHA/hCaptcha iframes
       → waits for the element to disappear

    Returns True once a CAPTCHA was solved, False when there was none. Raises ``CaptchaRequired`` when no human
    solved it in time, so callers never go on as if the request had been submitted. A solved CAPTCHA only means the
    field is gone: whether the portal accepted the request is decided by ``_confirm_request_submitted``.
    """
    # SISTER-native CAPTCHA
    captcha_input = page.locator("input[name='inCaptchaChars']")
    if await captcha_input.count() > 0:
        # After a wrong code SISTER re-renders the form with a NEW captcha (often at another URL), so a page
        # change is not proof of success: loop until the captcha field is gone, up to a few attempts.
        for attempt in range(1, _MAX_CAPTCHA_ATTEMPTS + 1):
            log.warning(
                "CAPTCHA SISTER rilevato (tentativo %d/%d) — in attesa che l'utente inserisca il codice (timeout %ds)...",
                attempt,
                _MAX_CAPTCHA_ATTEMPTS,
                timeout,
            )
            try:
                # Make it ready to type: bring the tab to the front and put the cursor in the code field
                await page.bring_to_front()
                await captcha_input.first.scroll_into_view_if_needed(timeout=3000)
                await captcha_input.first.focus(timeout=3000)
            except Exception as e:
                log.debug("Impossibile selezionare il campo CAPTCHA: %s", e)
            try:
                field = await captcha_input.first.element_handle(timeout=3000)
                deadline = asyncio.get_running_loop().time() + timeout
                while True:
                    try:
                        # False (or an error: context destroyed) once the form was submitted / page replaced
                        if not await field.evaluate("el => el.isConnected"):
                            break
                    except Exception:
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError("captcha non inviato")
                    await asyncio.sleep(0.3)
            except Exception:
                # The field vanished before we could grab it only if the form was submitted: re-check before giving up
                if await captcha_input.count() > 0:
                    log.warning("Timeout attesa CAPTCHA SISTER — richiesta non inoltrata, serve un operatore")
                    raise CaptchaRequired(
                        f"CAPTCHA SISTER non completato entro {timeout}s (tentativo {attempt}/{_MAX_CAPTCHA_ATTEMPTS})"
                    )
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception as e:
                log.debug("networkidle dopo CAPTCHA non raggiunto: %s", e)
            if await captcha_input.count() == 0:
                log.info("CAPTCHA SISTER risolto, pagina navigata a: %s", page.url)
                return True
            log.warning("CAPTCHA SISTER errato — SISTER ha proposto un nuovo codice")
        raise CaptchaRequired(f"CAPTCHA SISTER non completato dopo {_MAX_CAPTCHA_ATTEMPTS} tentativi")

    # Generic CAPTCHA (reCAPTCHA, hCaptcha, etc.)
    generic_selectors = [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        ".g-recaptcha",
        ".h-captcha",
    ]
    for selector in generic_selectors:
        if await page.locator(selector).count() > 0:
            log.warning("CAPTCHA rilevato — in attesa che l'utente lo risolva (timeout %ds)...", timeout)
            try:
                await page.locator(selector).first.wait_for(state="hidden", timeout=timeout * 1000)
                log.info("CAPTCHA risolto, riprendendo...")
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                log.warning("Timeout attesa CAPTCHA — richiesta non inoltrata, serve un operatore")
                raise CaptchaRequired(f"CAPTCHA non completato entro {timeout}s")
            return True
    return False


# What SISTER shows after "Inoltra" (checked against the saved pages in logs/pages: 381 accepted, 1 rejected):
#   accepted -> page "Attesa": ``Codice di Richiesta: C00075022026`` + ``Richiesta inoltrata: Verificare i risultati ...``
#   The "Codice di Richiesta" is the same for every request of an account/year (it is not a request id): the id of a
#   request is the ``id_richiesta`` of its row in "Richieste", which the download step reads.
#   rejected -> the "Tipo di visura" form again, ``Digitare correttamente il codice di sicurezza``, CAPTCHA still there
_REQUEST_ACCEPTED = re.compile(r"Richiesta\s+inoltrata", re.IGNORECASE)
_PORTAL_CODE = re.compile(r"Codice\s+di\s+Richiesta\s*:?\s*([A-Z0-9]{6,})", re.IGNORECASE)
_REQUEST_REJECTED = re.compile(r"Digitare\s+correttamente\s+il\s+codice\s+di\s+sicurezza", re.IGNORECASE)


def classify_submit_page(text: str, has_captcha_input: bool) -> dict:
    """Did SISTER accept the document request? (pure: the text of the page after Inoltra)

    ``confirmed`` needs the portal's own acknowledgement ("Richiesta inoltrata"), never just the absence of the
    CAPTCHA field: an error page can lack the field too. ``reason`` is ``captcha_rejected`` (the form came back) or
    ``no_confirmation`` (some other page).
    """
    code = _PORTAL_CODE.search(text or "")
    if _REQUEST_ACCEPTED.search(text or "") and not has_captcha_input:
        return {"confirmed": True, "portal_code": code.group(1).upper() if code else "", "reason": ""}
    if has_captcha_input or _REQUEST_REJECTED.search(text or ""):
        return {"confirmed": False, "portal_code": "", "reason": "captcha_rejected"}
    return {"confirmed": False, "portal_code": "", "reason": "no_confirmation"}


async def _confirm_request_submitted(page, wait: float = 6.0) -> dict:
    """Read the page after a submit and decide with ``classify_submit_page`` (waits a little for the navigation)."""
    result = {"confirmed": False, "portal_code": "", "reason": "unreadable"}
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        try:
            text = await page.inner_text("body")
            has_captcha = await page.locator("input[name='inCaptchaChars']").count() > 0
            result = classify_submit_page(text, has_captcha)
        except Exception as e:  # page navigating / context destroyed: look again
            log.debug("Conferma richiesta: pagina non leggibile: %s", e)
        if result["confirmed"] or result["reason"] == "captcha_rejected":
            return result
        if asyncio.get_running_loop().time() >= deadline:
            return result
        await asyncio.sleep(0.5)


async def _select_sezione(page, comune: str, sezione=None):
    """Select the sezione dropdown only when explicitly requested.

    Only interacts with the sezione dropdown if a sezione value was provided.
    Does NOT click 'scegli la sezione' or modify the dropdown otherwise.

    Returns the selected sezione value, or None.
    """
    if not sezione:
        return None

    sezione_select = page.locator("select[name='sezione']")
    sezione_options = await sezione_select.locator("option").all()

    # If dropdown is empty, click "scegli la sezione" to load options
    if not sezione_options:
        try:
            sel_btn = page.locator("input[name='selSezione'][value='scegli la sezione']")
            if await sel_btn.count() > 0:
                await sel_btn.click()
                await page.wait_for_load_state("networkidle", timeout=30000)
                sezione_options = await sezione_select.locator("option").all()
        except Exception:
            pass

    if not sezione_options:
        log.warning("Sezione '%s' richiesta ma nessuna sezione disponibile per '%s'", sezione, comune)
        return None

    sezione_value = await find_best_option_match(page, "select[name='sezione']", sezione)
    if sezione_value:
        log.info("Sezione: [cyan]%s[/cyan]", sezione_value)
        await sezione_select.select_option(sezione_value)
        return sezione_value

    available = []
    for option in sezione_options:
        value = await option.get_attribute("value")
        text_content = await option.inner_text()
        if value and text_content:
            available.append(f"{text_content.strip()} ({value})")
    log.warning("Sezione '%s' non trovata. Disponibili: %s", sezione, ", ".join(available))
    return None


IMMOBILI_RADIOS = "input[type='radio'][property='visImmSel'], input[type='radio'][name='visImmSel']"
_FALSE_VALUES = {"0", "false", "no", "n", "off", "f", "non"}


async def _classify_immobili_radios(page, radio_count: int) -> tuple[list[int], set[int], int]:
    """Split the immobili list into the rows to visit, the 'Bene comune non censibile' rows and the soppressi count."""
    radios = page.locator(IMMOBILI_RADIOS)
    active: list[int] = []
    bene_comune: set[int] = set()
    soppressi = 0
    for i in range(radio_count):
        val = await radios.nth(i).get_attribute("value") or ""
        if "Soppress" in val:
            soppressi += 1
            continue
        active.append(i)
        if "Bene comune" in val:
            bene_comune.add(i)
    return active, bene_comune, soppressi


async def _extract_intestati_with_identity(page) -> list[dict]:
    """The Intestati table, completed with what the owner radios carry (sesso, luogo e data di nascita, sede).

    The table gives name, codice fiscale, titolarita' and quota; the ``intestatoSelezionato`` radios of the same page
    give the sex and the birth data as separate fields. Rows and radios are paired by position, and only when
    their number matches and the codici fiscali agree.
    """
    rows = await _extract_intestati_playwright(page)
    radios = page.locator("input[type='radio'][name='intestatoSelezionato']")
    count = await radios.count()
    if not rows or count != len(rows):
        return rows
    for i, row in enumerate(rows):
        identity = parse_intestato_value(await radios.nth(i).get_attribute("value") or "")
        row_id = (row.get("Codice fiscale") or "").strip().upper()
        if row_id and identity["codice_fiscale"] and row_id != identity["codice_fiscale"]:
            continue
        if not row_id and identity["codice_fiscale"]:
            row["Codice fiscale"] = identity["codice_fiscale"]
        for key in ("sesso", "luogo_nascita", "data_nascita", "sede"):
            if identity.get(key):
                row.setdefault(key, identity[key])
    return rows


async def _restore_immobili_list(
    page, page_logger, provincia, comune, sezione, foglio, particella, tipo_catasto, subalterno, sezione_urbana
):
    """Be on the immobili list again: step back from a sub-page, or re-submit the search when SISTER lost the list."""
    await _navigate_back_to_immobili_list(page)
    if await page.locator(IMMOBILI_RADIOS).count() == 0:
        await _resubmit_search_for_immobili_list(
            page, page_logger, provincia, comune, sezione, foglio, particella, tipo_catasto, subalterno,
            sezione_urbana,
        )
    await _fill_richiedente_motivo(page, sezione_urbana=sezione_urbana)
    return page.locator(IMMOBILI_RADIOS)


async def _read_immobili_intestati(page, page_logger, immobili: list[dict], search: tuple):
    """Phase 1 of ``run_visura``: one pass over the immobili list, reading the Intestati page of each immobile.

    Every page involved is plain HTML without CAPTCHA, and "Indietro" from the Intestati page leaves the list valid
    (only a submitted request makes SISTER forget it), so the whole list is read before anything is requested. A
    CAPTCHA in phase 2 can then no longer cost us the owners of the immobili after the one it stopped at.

    Returns (all intestati, one result per immobile visited, soppressi skipped, bene comune indices).
    """
    log.info("Fase 1 (HTML): intestati di ogni immobile...")
    all_intestati: list[dict] = []
    results_list: list[dict] = []
    sezione_urbana = search[7]

    # Re-fill richiedente/motivo/sezUrb on results page (SISTER clears them after submit)
    await _fill_richiedente_motivo(page, sezione_urbana=sezione_urbana)

    radio_count = await page.locator(IMMOBILI_RADIOS).count()
    active_indices, bene_comune_indices, skipped_soppresso = await _classify_immobili_radios(page, radio_count)
    if skipped_soppresso:
        log.info("Saltati %d immobili soppressi su %d totali", skipped_soppresso, radio_count)
    if bene_comune_indices:
        log.info("Trovati %d 'Bene comune non censibile' — intestati saltati per questi", len(bene_comune_indices))
    total_active = len(active_indices)
    log.info("Iterando per %d immobili attivi", total_active)

    for item_num, radio_idx in enumerate(active_indices, 1):
        imm_data = immobili[radio_idx] if radio_idx < len(immobili) else {}
        step_result = {"result_index": radio_idx + 1, "immobile": imm_data, "intestati": [], "visura": None}
        results_list.append(step_result)  # kept even if this immobile fails: the others are still read
        if radio_idx in bene_comune_indices:
            continue
        try:
            await page.locator(IMMOBILI_RADIOS).nth(radio_idx).click()
            log.info(
                "[%d/%d] Immobile radio %d — Sub.%s %s",
                item_num,
                total_active,
                radio_idx + 1,
                imm_data.get("Sub", "?"),
                imm_data.get("Indirizzo", "")[:30],
            )
            intestati_btn = page.locator("input[name='intestati'][value='Intestati']")
            if await intestati_btn.count() == 0:
                continue
            await intestati_btn.click()
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page_logger.log(page, f"intestati_{radio_idx + 1}")
            step_intestati = await _extract_intestati_with_identity(page)
            log.info("[green]%d intestati[/green] per immobile (radio %d)", len(step_intestati), radio_idx + 1)
            all_intestati.extend(step_intestati)
            step_result["intestati"] = step_intestati
            await _navigate_back_to_immobili_list(page)
            await _fill_richiedente_motivo(page, sezione_urbana=sezione_urbana)
        except Exception as e:
            step_result["error"] = str(e)[:200]
            log.warning("Intestati non letti per immobile %d/%d: %s", item_num, total_active, e)
            await _restore_immobili_list(page, page_logger, *search)

    if not results_list and immobili:
        results_list = [{"result_index": 1, "immobile": immobili[0], "intestati": all_intestati}]
    return all_intestati, results_list, skipped_soppresso, bene_comune_indices


def _owner_identifier(row: dict) -> str:
    return (row.get("Codice fiscale") or row.get("Codice Fiscale") or "").strip().upper()


def _plan_soggetto_requests(results_list: list[dict], bene_comune_indices: set[int]) -> dict[int, int | None]:
    """Which owner (row of the Intestati table) gets a Visura per Soggetto for each immobile.

    One owner per immobile, as before, but never an owner already requested for an earlier immobile of the same run:
    the document is the same, and every request costs a CAPTCHA. ``None`` = nothing to request for that immobile.
    """
    requested: set[str] = set()
    plan: dict[int, int | None] = {}
    for step in results_list:
        index = step["result_index"]
        owners = step.get("intestati") or []
        if (index - 1) in bene_comune_indices or not owners:
            plan[index] = None
            continue
        pick = next((i for i, row in enumerate(owners) if _owner_identifier(row) not in requested), None)
        if pick is None:
            plan[index] = None
            step.setdefault("documents", {})["soggetto"] = "duplicate"
            continue
        if _owner_identifier(owners[pick]):
            requested.add(_owner_identifier(owners[pick]))
        plan[index] = pick
    return plan


def _record_submit(step: dict, docs: dict, kind: str, confirmation: dict) -> None:
    """Store the outcome of one document request: ``requested`` only when the portal acknowledged it."""
    if confirmation.get("confirmed"):
        docs[kind] = "requested"
        if confirmation.get("portal_code"):
            step.setdefault("portal_codes", {})[kind] = confirmation["portal_code"]
        return
    docs[kind] = "unconfirmed"
    step.setdefault("submit_problems", {})[kind] = confirmation.get("reason") or "no_confirmation"
    log.warning("Richiesta %s non confermata dal portale (%s)", kind, step["submit_problems"][kind])


def _unconfirmed_documents(results_list: list[dict]) -> list[dict]:
    """The requests that were submitted but never acknowledged by the portal (to be re-checked in Richieste)."""
    return [
        {"result_index": step["result_index"], "kind": kind, "reason": (step.get("submit_problems") or {}).get(kind, "")}
        for step in results_list
        for kind, state in (step.get("documents") or {}).items()
        if state == "unconfirmed"
    ]


def _pending_documents(results_list: list[dict]) -> list[dict]:
    """The document requests that did not go out (a CAPTCHA stopped the run): what a human has to finish."""
    pending = []
    for step in results_list:
        for kind, state in (step.get("documents") or {}).items():
            if state == "pending":
                pending.append({"result_index": step["result_index"], "kind": kind})
    return pending


async def _submit_visura_soggetto(page, page_logger, radio_idx: int, step_result: dict) -> bool:
    """From the Intestati page, press "Visura per Soggetto" and walk to the form that is submitted (CAPTCHA).

    Returns True when the request was submitted. May pass through RicercaPF / SceltaOmonimi first.
    """
    visura_sogg_btn = page.locator("input[name='visura'][value='Visura per Soggetto']")
    await visura_sogg_btn.click()
    await page.wait_for_load_state("networkidle", timeout=30000)

    submitted = False
    for _nav in range(5):
        current_url = page.url

        # TipoVisura.do — the visura options form. After picking an owner SISTER keeps the URL at
        # SceltaIntestatiIMM.do while already showing this form (with its own CAPTCHA), so detect it
        # from the page content as well as from the URL.
        if "TipoVisura" in current_url or await page.locator("form[name='TipoVisuraForm']").count() > 0:
            await _set_visura_form_defaults(page)
            await page_logger.log(page, f"visura_soggetto_{radio_idx + 1}")
            step_result["visura_soggetto"] = await _extract_visura_immobile_playwright(page)

            has_captcha = await _wait_for_captcha(page)
            if not has_captcha:
                inoltra_btn = page.locator(
                    "input[name='inoltra'][value='Inoltra'], input[type='submit'][value='Inoltra']"
                )
                if await inoltra_btn.count() > 0:
                    await inoltra_btn.click()
                    await page.wait_for_load_state("networkidle", timeout=30000)
            confirmation = await _confirm_request_submitted(page)
            step_result["_soggetto_confirmation"] = confirmation
            submitted = confirmation["confirmed"]
            break

        # RicercaPF.do — persona fisica search: click Ricerca to proceed
        if "RicercaPF" in current_url:
            log.info("Pagina RicercaPF — procedendo con ricerca")
            await page_logger.log(page, f"ricerca_pf_{radio_idx + 1}")
            ricerca_btn = page.locator("input[type='submit'][value='Ricerca'], input[name='ricerca'][value='Ricerca']")
            if await ricerca_btn.count() > 0:
                await ricerca_btn.first.click()
                await page.wait_for_load_state("networkidle", timeout=30000)
                continue

        # SceltaOmonimiPF.do — homonym selection: select first and proceed
        if "SceltaOmonimi" in current_url:
            log.info("Pagina SceltaOmonimi — selezionando primo soggetto")
            await page_logger.log(page, f"scelta_omonimi_{radio_idx + 1}")
            first_radio = page.locator("input[type='radio']").first
            if await first_radio.count() > 0:
                await first_radio.click()
            submit_btn = page.locator(
                "input[type='submit'][value='Conferma'], input[type='submit'][value='Prosegui'], input[type='submit']"
            ).first
            if await submit_btn.count() > 0:
                await submit_btn.click()
                await page.wait_for_load_state("networkidle", timeout=30000)
                continue

        # InoltraRichiestaVis.do — already submitted: confirm from the page, not from the URL
        if "InoltraRichiesta" in current_url:
            confirmation = await _confirm_request_submitted(page)
            step_result["_soggetto_confirmation"] = confirmation
            submitted = confirmation["confirmed"]
            break

        # Unknown page — log and break
        log.warning("Pagina inattesa durante Visura per Soggetto: %s", current_url)
        await page_logger.log(page, f"visura_soggetto_unexpected_{radio_idx + 1}")
        break

    if submitted:
        await page_logger.log(page, f"visura_soggetto_inoltrata_{radio_idx + 1}")
        log.info("Visura per Soggetto inoltrata per radio %d", radio_idx + 1)
    return submitted


async def _request_visura_documents(
    page, page_logger, results_list, bene_comune_indices, tipo_visura, visura_soggetto, search
) -> None:
    """Phase 2 of ``run_visura``: submit the document requests, one Visura per Immobile + one per Soggetto.

    Each request ends at the Tipo di visura form, which is where SISTER shows the CAPTCHA (a human types it; if nobody
    does, ``CaptchaRequired`` stops the run and the steps still marked ``pending`` are what is left to request).
    """
    plan = _plan_soggetto_requests(results_list, bene_comune_indices) if visura_soggetto else {}
    for step in results_list:
        docs = step.setdefault("documents", {})
        docs.setdefault("immobile", "pending")
        if visura_soggetto and plan.get(step["result_index"]) is not None:
            docs.setdefault("soggetto", "pending")

    for step in results_list:
        radio_idx = step["result_index"] - 1
        docs = step["documents"]

        # --- Visura Per Immobile ---
        radios = await _restore_immobili_list(page, page_logger, *search)
        await radios.nth(radio_idx).click()
        visura_btn = page.locator("input[name='visuraImm'][value='Visura Per Immobile']")
        if await visura_btn.count() > 0:
            await visura_btn.click()
            await page.wait_for_load_state("networkidle", timeout=30000)

            # Set default options: requested tipo visura, XML, differita
            await _set_visura_form_defaults(page, tipo_visura)
            await page_logger.log(page, f"visura_immobile_{radio_idx + 1}")

            # Extract visura data from the form page before submitting
            step["visura"] = await _extract_visura_immobile_playwright(page)

            # Wait for user to solve CAPTCHA (fills inCaptchaChars → form auto-submits)
            if not await _wait_for_captcha(page):
                # No CAPTCHA — click Inoltra manually
                inoltra_btn = page.locator(
                    "input[name='inoltra'][value='Inoltra'], input[type='submit'][value='Inoltra']"
                )
                if await inoltra_btn.count() > 0:
                    await inoltra_btn.click()
                    await page.wait_for_load_state("networkidle", timeout=30000)
            confirmation = await _confirm_request_submitted(page)
            await page_logger.log(page, f"visura_inoltrata_{radio_idx + 1}")
            _record_submit(step, docs, "immobile", confirmation)
            log.info(
                "Visura Per Immobile radio %d: %s%s",
                radio_idx + 1,
                docs["immobile"],
                f" (codice {confirmation['portal_code']})" if confirmation["portal_code"] else "",
            )

            # SISTER loses the session state after Inoltra
            await _resubmit_search_for_immobili_list(page, page_logger, *search)
        else:
            docs["immobile"] = "unavailable"

        # --- Visura per Soggetto (one owner, none already requested in this run) ---
        pick = plan.get(step["result_index"])
        if pick is None:
            continue
        radios = await _restore_immobili_list(page, page_logger, *search)
        await radios.nth(radio_idx).click()
        intestati_btn = page.locator("input[name='intestati'][value='Intestati']")
        if await intestati_btn.count() == 0:
            docs["soggetto"] = "unavailable"
            continue
        await intestati_btn.click()
        await page.wait_for_load_state("networkidle", timeout=30000)
        if await page.locator("input[name='visura'][value='Visura per Soggetto']").count() == 0:
            docs["soggetto"] = "unavailable"
            continue
        # With several intestati the portal lists one unchecked radio per owner and rejects the submit
        # ("Selezionare un Omonimo") until one is chosen.
        owner_radios = page.locator("input[type='radio'][name='intestatoSelezionato']")
        owners = await owner_radios.count()
        if owners:
            await owner_radios.nth(min(pick, owners - 1)).check()
            if owners > 1:
                log.info("Piu' intestati (%d) — Visura per Soggetto per l'intestato %d", owners, pick + 1)
        if await _submit_visura_soggetto(page, page_logger, radio_idx, step):
            _record_submit(step, docs, "soggetto", step.pop("_soggetto_confirmation", {"confirmed": True}))
        elif "_soggetto_confirmation" in step:
            _record_submit(step, docs, "soggetto", step.pop("_soggetto_confirmation"))
        await _resubmit_search_for_immobili_list(page, page_logger, *search)


async def run_visura(
    page,
    provincia="Trieste",
    comune="Trieste",
    sezione=None,
    foglio="9",
    particella="166",
    tipo_catasto="T",
    extract_intestati=True,
    subalterno=None,
    sezione_urbana=None,
    tipo_visura="completa",
    visura_soggetto=True,
    request_documents=None,
):
    """Search a property and request its visura (and, optionally, each owner's Visura per Soggetto).

    Two phases: first every HTML page is read (the immobili list and the Intestati page of each immobile: no
    CAPTCHA), then the document requests are submitted (Tipo di visura form: CAPTCHA).

    tipo_visura: 'completa', 'storica_analitica' or 'storica_sintetica' (Tipo visura on the request form).
    visura_soggetto: also request the Visura per Soggetto for the owners found (one owner per immobile, and never
        the same owner twice in one run; extra captcha per request).
    request_documents: False stops after phase 1 (HTML only, no CAPTCHA, nothing is requested or downloaded).
        Default: the ``richiedi_documenti`` form field, else True.
    """
    if request_documents is None:
        request_documents = str(current_form_fields().get("richiedi_documenti", "true")).strip().lower() not in _FALSE_VALUES
    # what _resubmit_search_for_immobili_list needs to bring the list back
    search = (provincia, comune, sezione, foglio, particella, tipo_catasto, subalterno, sezione_urbana)
    time0 = time.time()
    page_logger = PageLogger("visura")
    sezione_info = f", sezione={sezione}" if sezione else ""
    subalterno_info = f", sub={subalterno}" if subalterno else ""
    log.info(
        "[bold]Visura[/bold] %s/%s F.%s P.%s%s%s tipo=%s",
        provincia,
        comune,
        foglio,
        particella,
        sezione_info,
        subalterno_info,
        tipo_catasto,
    )

    # STEP 1: Selezione Ufficio Provinciale
    await _navigate_to_scelta_servizio(page, page_logger)

    # Trova e seleziona la provincia corretta
    provincia_options = await page.locator("select[name='listacom'] option").all()
    available_provinces = []
    for option in provincia_options:
        value = await option.get_attribute("value")
        text = await option.inner_text()
        if value and text:
            available_provinces.append(f"{text} ({value})")

    if len(available_provinces) == 0:
        raise Exception("Nessuna provincia disponibile - sessione scaduta")

    log.debug("Province disponibili: %d", len(available_provinces))

    provincia_value = await find_best_option_match(page, "select[name='listacom']", provincia)

    if not provincia_value:
        raise Exception(f"Provincia '{provincia}' non trovata. Disponibili: {', '.join(available_provinces[:10])}")

    log.info("Provincia: [cyan]%s[/cyan]", provincia_value)
    try:
        await page.locator("select[name='listacom']").select_option(provincia_value)
    except Exception as e:
        raise Exception(f"Errore selezione provincia '{provincia_value}': {e}")

    await page.locator("input[type='submit'][value='Applica']").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "provincia_applicata")

    # STEP 2: Ricerca per immobili
    log.info("Ricerca per immobile...")
    await page.get_by_role("link", name="Immobile").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "immobile")

    # STEP 2.1: Seleziona tipo catasto (T=Terreni, F=Fabbricati)
    tipo_label = "Terreni" if tipo_catasto == "T" else "Fabbricati"
    log.info("Tipo catasto: [cyan]%s[/cyan] (%s)", tipo_catasto, tipo_label)
    try:
        await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)
    except Exception as e:
        log.warning("Errore selezione tipo catasto: %s", e)

    # Trova e seleziona il comune corretto
    comune_options = await page.locator(f"{await _comune_selector(page)} option").all()
    available_comuni = []
    for option in comune_options:
        value = await option.get_attribute("value")
        text = await option.inner_text()
        if value and text:
            available_comuni.append(f"{text} ({value})")

    log.debug("Comuni disponibili: %d", len(available_comuni))

    comune_value = await find_best_option_match(page, await _comune_selector(page), comune)

    if not comune_value:
        raise Exception(
            f"Comune '{comune}' non trovato per provincia '{provincia}'. Disponibili: {', '.join(available_comuni[:10])}"
        )

    log.info("Comune: [cyan]%s[/cyan]", comune_value)
    try:
        await page.locator(await _comune_selector(page)).select_option(comune_value)
    except Exception as e:
        raise Exception(f"Errore selezione comune '{comune_value}': {e}")

    await _select_sezione(page, comune, sezione)

    # Fill "Sezione urbana" — only when explicitly provided (separate from dropdown sezione)
    if sezione_urbana:
        sez_urb_field = page.locator("input[name='sezUrb']")
        if await sez_urb_field.count() > 0:
            await sez_urb_field.fill(str(sezione_urbana).upper())
            log.info("Sezione urbana: [cyan]%s[/cyan]", sezione_urbana)

    # Inserisci foglio, particella, subalterno
    log.info(
        "Foglio: [cyan]%s[/cyan]  Particella: [cyan]%s[/cyan]%s",
        foglio,
        particella,
        f"  Sub: [cyan]{subalterno}[/cyan]" if subalterno else "",
    )
    await page.locator("input[name='foglio']").click()
    await page.locator("input[name='foglio']").fill(str(foglio))
    await page.locator("input[name='particella1']").click()
    await page.locator("input[name='particella1']").fill(str(particella))
    if subalterno:
        await page.locator("input[name='subalterno1']").fill(str(subalterno))

    await _fill_richiedente_motivo(page, sezione_urbana=sezione_urbana)
    await page_logger.log(page, "form_compilato")

    # Clicca Ricerca
    log.info("Esecuzione ricerca...")
    await page.locator("input[name='scelta'][value='Ricerca']").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await _wait_for_captcha(page)
    await page_logger.log(page, "ricerca")

    # STEP 3: Gestisci conferma assenza subalterno (se necessario)
    try:
        conferma_button = page.locator("input[name='confAssSub'][value='Conferma']")
        if await conferma_button.count() > 0:
            log.warning("Confermi Assenza Subalterno — confermando automaticamente")
            await conferma_button.click()
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page_logger.log(page, "conferma_subalterno")
    except Exception as e:
        log.debug("Conferma subalterno non necessaria: %s", e)

    # STEP 3.1: Controlla se la ricerca ha restituito risultati
    page_text = await page.inner_text("body")
    if "NESSUNA CORRISPONDENZA TROVATA" in page_text:
        elapsed = time.time() - time0
        log.warning("Nessuna corrispondenza trovata (%.1fs)", elapsed)
        return {
            "immobili": [],
            "results": [],
            "total_results": 0,
            "intestati": [],
            "error": "NESSUNA CORRISPONDENZA TROVATA",
            "page_visits": page_logger.page_visits,
        }

    # STEP 4: Estrazione tabella Elenco Immobili
    log.info("Estraendo immobili...")
    try:
        immobili = []
        selectors = [
            "table.listaIsp4",
            "table[class*='lista']",
            "table:has(th:text('Foglio'))",
            "table",
        ]

        for selector in selectors:
            try:
                immobili_table = page.locator(selector)
                count = await immobili_table.count()
                log.debug("Selettore '%s': %d tabelle", selector, count)

                if count > 0:
                    for i in range(count):
                        try:
                            table_elem = immobili_table.nth(i)
                            immobili_html = await table_elem.inner_html(timeout=10000)
                            if "Foglio" in immobili_html or "Particella" in immobili_html:
                                immobili = parse_table(immobili_html)
                                log.info("[green]%d immobili[/green] estratti (%s)", len(immobili), selector)
                                break
                        except Exception as e:
                            log.debug("Errore tabella %d: %s", i, e)
                            continue

                    if immobili:
                        break

            except Exception as e:
                log.debug("Errore selettore '%s': %s", selector, e)
                continue

        if not immobili:
            log.warning("Tabella immobili non trovata")
            await page_logger.log(page, "immobili_non_trovati")
            immobili = []
    except Exception as e:
        log.error("Errore estrazione immobili: %s", e)
        immobili = []

    # Check if we're on the AssenzaSubalterno page with radio buttons
    radio_buttons_check = page.locator(
        "input[type='radio'][property='visImmSel'], input[type='radio'][name='visImmSel']"
    )
    has_radio_list = await radio_buttons_check.count() > 0

    # Se non servono intestati E non siamo sulla pagina con lista immobili
    if not extract_intestati and not has_radio_list:
        elapsed = time.time() - time0
        log.info("[green]Visura completata[/green] in %.1fs — %d immobili", elapsed, len(immobili))
        return {
            "immobili": immobili,
            "results": [],
            "total_results": len(immobili),
            "intestati": [],
            "page_visits": page_logger.page_visits,
        }

    # STEP 5 — phase 1, HTML sweep (see _read_immobili_intestati)
    all_intestati, results_list, skipped_soppresso, bene_comune_indices = await _read_immobili_intestati(
        page, page_logger, immobili, search
    )
    needs_human = None

    # STEP 6 — phase 2, document requests (the Tipo di visura form: the only place with a CAPTCHA).
    documents_pending: list[dict] = []
    documents_unconfirmed: list[dict] = []
    if request_documents and results_list:
        log.info("Fase 2 (documenti): richieste visura%s", " + soggetto" if visura_soggetto else "")
        try:
            await _request_visura_documents(
                page, page_logger, results_list, bene_comune_indices, tipo_visura, visura_soggetto, search
            )
        except CaptchaRequired as e:
            # Keep what was extracted from the pages and flag the pending requests for a human, instead of reporting a
            # plain success.
            needs_human = str(e)
            log.warning("Richiesta documento in attesa di un operatore (CAPTCHA): %s", e)
        except Exception as e:
            log.error("Errore richiesta documenti: %s", e)
        documents_pending = _pending_documents(results_list)
        documents_unconfirmed = _unconfirmed_documents(results_list)
    elif not request_documents:
        log.info("Fase 2 saltata (richiedi_documenti=false): solo pagine HTML")

    # --- Download PDFs from Richieste page ---
    downloaded_pdfs = []
    if extract_intestati and request_documents and results_list:
        try:
            downloaded_pdfs = await _download_richieste_documents(page, page_logger)
        except Exception as e:
            log.warning("Errore download PDF da Richieste: %s", e)

    elapsed = time.time() - time0
    log.info(
        "[green]Visura completata[/green] in %.1fs — %d immobili, %d intestati, %d soppressi saltati, %d PDF scaricati",
        elapsed,
        len(immobili),
        len(all_intestati),
        skipped_soppresso,
        len(downloaded_pdfs),
    )

    result = {
        "immobili": immobili,
        "results": results_list,
        "total_results": len(immobili),
        "intestati": all_intestati,
        "skipped_soppresso": skipped_soppresso,
        "downloaded_pdfs": downloaded_pdfs,
        "page_visits": page_logger.page_visits,
        **({"documents_pending": documents_pending} if documents_pending else {}),
        **({"documents_unconfirmed": documents_unconfirmed} if documents_unconfirmed else {}),
        **({"needs_human": needs_human} if needs_human else {}),
    }

    return result


async def _resubmit_search_for_immobili_list(
    page,
    page_logger,
    provincia,
    comune,
    sezione,
    foglio,
    particella,
    tipo_catasto,
    subalterno,
    sezione_urbana,
):
    """Re-submit the property search to restore the immobili list.

    After submitting "Visura Per Immobile" or "Visura per Soggetto", SISTER
    loses the session state for the immobili list. We need to re-navigate
    from SceltaServizio and re-submit the search to get the radio buttons back.

    Returns a fresh radio_buttons locator.
    """
    log.info("Ri-eseguendo ricerca per ripristinare lista immobili...")

    # the subject step may have left another office selected: set the property's province again
    await _set_office(page, page_logger, provincia)

    await page.get_by_role("link", name="Immobile").click()
    await page.wait_for_load_state("networkidle", timeout=30000)

    await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)

    comune_value = await find_best_option_match(page, await _comune_selector(page), comune)
    if comune_value:
        await page.locator(await _comune_selector(page)).select_option(comune_value)

    await _select_sezione(page, comune, sezione)

    if sezione_urbana:
        sez_urb = page.locator("input[name='sezUrb']")
        if await sez_urb.count() > 0:
            await sez_urb.fill(str(sezione_urbana).upper())

    await page.locator("input[name='foglio']").fill(str(foglio))
    await page.locator("input[name='particella1']").fill(str(particella))
    if subalterno:
        await page.locator("input[name='subalterno1']").fill(str(subalterno))

    await _fill_richiedente_motivo(page, sezione_urbana=sezione_urbana)

    await page.locator("input[name='scelta'][value='Ricerca']").click()
    await page.wait_for_load_state("networkidle", timeout=30000)

    # Handle "Conferma Assenza Subalterno" if it appears
    try:
        conferma = page.locator("input[name='confAssSub'][value='Conferma']")
        if await conferma.count() > 0:
            await conferma.click()
            await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass

    radio_buttons = page.locator("input[type='radio'][property='visImmSel'], input[type='radio'][name='visImmSel']")
    count = await radio_buttons.count()
    log.info("Lista immobili ripristinata: %d radio buttons", count)
    return radio_buttons


async def _navigate_back_to_immobili_list(page):
    """Navigate back to the immobili list (AssenzaSubalterno.do) from any sub-page.

    Stops as soon as the page has radio buttons (visImmSel) — that's the immobili list.
    """
    for _attempt in range(5):
        # Check if we're on the immobili list by looking for radio buttons
        radios = page.locator("input[type='radio'][property='visImmSel'], input[type='radio'][name='visImmSel']")
        if await radios.count() > 0:
            log.debug("Tornati alla lista immobili (%d radio) — %s", await radios.count(), page.url)
            return

        current = page.url

        # Try the annullaConf button first (specific to InoltraRichiestaVis.do)
        annulla_btn = page.locator("input[name='annullaConf'][value='Indietro']")
        if await annulla_btn.count() > 0:
            log.debug("Cliccando annullaConf Indietro da %s", current)
            await annulla_btn.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            continue

        # Try standard Indietro from IndietroVisImmSogg (intestati page back)
        indietro_vis = page.locator("form[action*='IndietroVisImmSogg'] input[type='submit']")
        if await indietro_vis.count() > 0:
            log.debug("Cliccando IndietroVisImmSogg da %s", current)
            await indietro_vis.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            continue

        # Generic Indietro — but NOT the one that goes to IndietroDatiImm (search form)
        back_btn = page.locator("input[name='indietro'][value='Indietro']")
        if await back_btn.count() > 0:
            # Check if this Indietro leads to the search form — don't click it
            parent_form = page.locator("form[action*='IndietroDatiImm'] input[name='indietro']")
            if await parent_form.count() > 0:
                log.debug("Indietro verso search form (IndietroDatiImm) — non cliccare, siamo sulla lista")
                return
            log.debug("Cliccando Indietro generico da %s", current)
            await back_btn.first.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            continue

        log.warning("Nessun bottone Indietro trovato su %s", current)
        break

    log.warning("Navigazione indietro completata — URL corrente: %s", page.url)


_TIPO_VISURA_VALUES = {"completa": "0", "storica_analitica": "3", "storica_sintetica": "4"}


async def _set_visura_form_defaults(page, tipo_visura="completa"):
    """Set default options on the SceltaVisuraImmSogg form.

    Defaults:
      - Con intestati: selected (required for XML format)
      - Tipo visura: Completa (tipoVisura=0), or Storica Analitica (3) / Storica Sintetica (4) when requested
      - Formato documento: XML (tipoDocFornitura=XML)
      - richiesta in differita: checked (differita=1)

    Uses JS evaluate for radio buttons that may be hidden by SISTER's
    dynamic form logic (XML option is only visible with certain combinations).
    """
    value = _TIPO_VISURA_VALUES.get(tipo_visura)
    if value is None:
        raise ValueError(f"tipo_visura sconosciuto: {tipo_visura!r} (usa {', '.join(_TIPO_VISURA_VALUES)})")
    await page.evaluate(
        """(value) => {
        // Select "Con intestati" first (required for XML format to be visible)
        const conIntestati = document.querySelector('input[name="intestati"][value="1"]');
        if (conIntestati && !conIntestati.checked) {
            conIntestati.click();
        }

        // Tipo visura (Completa=0, Storica Analitica=3, Storica Sintetica=4)
        const tipo = document.querySelector('input[name="tipoVisura"][value="' + value + '"]');
        if (tipo) {
            tipo.click();
        }

        // Trigger the JS that shows/hides format options
        if (typeof checkPdfXml === 'function') checkPdfXml(true);
        if (typeof tipoVisuradisplayPdf === 'function' && tipo) tipoVisuradisplayPdf(tipo.value);

        // Formato documento → XML
        const xml = document.querySelector('input[name="tipoDocFornitura"][value="XML"]');
        if (xml) {
            xml.parentElement.style.display = '';
            xml.checked = true;
        }

        // richiesta in differita → checked
        const differita = document.querySelector('input[name="differita"]');
        if (differita && !differita.checked) {
            differita.checked = true;
        }
    }""",
        value,
    )
    log.info("Form defaults: Con intestati, %s, XML, Differita", tipo_visura)


async def _download_richieste_documents(page, page_logger) -> list[dict]:
    """Navigate to the Richieste page, extract metadata, download and rename files.

    1. Scrapes the Richieste table for metadata (Oggetto, timestamp, idRichiesta)
    2. Downloads each document from the "salva" column
    3. Extracts P7M → XML via openssl
    4. Parses XML for structured data
    5. Renames files using the shared cadastral filename convention
    6. Persists to visura_documents table

    Returns a list of dicts with download info.
    """
    from .database import OUTPUTS_DIR

    # Navigate to Richieste page
    richieste_link = page.locator("a:has-text('Richieste')")
    if await richieste_link.count() == 0:
        await _navigate_to_scelta_servizio(page, page_logger)
        richieste_link = page.locator("a:has-text('Richieste')")

    if await richieste_link.count() == 0:
        log.warning("Link Richieste non trovato")
        return []

    href = await richieste_link.get_attribute("href")
    if not href:
        return []

    if not href.startswith("http"):
        href = "https://sister3.agenziaentrate.gov.it" + href
    await page.goto(href, timeout=30000)
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "richieste")

    # --- Step 1: Extract table metadata using BS4 ---
    html_content = await page.content()
    richieste_meta = _parse_richieste_table(html_content)
    log.info("Richieste trovate: %d", len(richieste_meta))

    if not richieste_meta:
        return []

    # Documents live under SISTER_FILES_BASE (what the web UI reads); fall back to <outputs>/documents
    docs_dir = os.getenv("SISTER_FILES_BASE") or os.path.join(OUTPUTS_DIR, "documents")
    os.makedirs(docs_dir, exist_ok=True)
    links_dir = os.getenv("SISTER_DOCUMENT_LINKS_DIR") or os.path.join(OUTPUTS_DIR, "document_links")

    # --- Step 2: Download each document ---
    downloaded = []

    for meta in richieste_meta:
        salva_href = meta.get("salva_href", "")
        if not salva_href:
            continue

        id_richiesta = meta.get("id_richiesta", "")
        oggetto = meta.get("oggetto", "")
        richiesta_del = meta.get("richiesta_del", "")
        formato = meta.get("formato", "")

        orig_filename = ""
        temp_path = ""
        extracted_temp_path = None
        try:
            # Find and click the salva link for this idRichiesta
            link = page.locator(f"a[href*='idRichiesta={id_richiesta}'][href*='salva']")
            if await link.count() == 0:
                link = page.locator(f"a[href*='{id_richiesta}']")
            if await link.count() == 0:
                log.warning("Link salva non trovato per idRichiesta=%s", id_richiesta)
                continue

            async with page.expect_download(timeout=60000) as download_info:
                await link.first.click()
            download = await download_info.value
            orig_filename = download.suggested_filename or f"DOC_{id_richiesta}.dat"

            # Determine extension from the original filename or the portal's
            # format label. An empty label must not create a bare "." suffix.
            file_ext = os.path.splitext(orig_filename)[1].lower()
            if not file_ext:
                format_match = re.search(r"\b(p7m|xml|pdf|zip)\b", formato.lower())
                file_ext = f".{format_match.group(1)}" if format_match else ".dat"
            file_format = file_ext.lstrip(".").upper()

            # Save temporarily so the content can determine its canonical name.
            temp_stem = f".sister_download_{id_richiesta}_{time.time_ns()}"
            temp_path = os.path.join(docs_dir, f"{temp_stem}{file_ext}")
            await download.save_as(temp_path)

            parsed = None
            if file_format == "P7M":
                extracted_temp_path = _extract_p7m(temp_path)
                if extracted_temp_path:
                    parsed = _parse_visura_xml(extracted_temp_path)
            elif file_format == "XML":
                parsed = _parse_visura_xml(temp_path)
            elif file_format == "PDF":
                parsed = _parse_visura_pdf(temp_path)

            canonical_stem = _descriptive_filename(parsed or {}, oggetto=oggetto, request_id=id_richiesta)
            final_stem = _unique_document_stem(
                docs_dir,
                canonical_stem,
                file_ext,
                include_xml=(file_format == "P7M"),
                parsed=parsed or {},
                request_id=id_richiesta,
            )
            final_filename = f"{final_stem}{file_ext}"
            save_path = os.path.join(docs_dir, final_filename)
            os.replace(temp_path, save_path)
            temp_path = ""

            extracted_path = None
            if extracted_temp_path and os.path.exists(extracted_temp_path):
                extracted_path = os.path.join(docs_dir, f"{final_stem}.xml")
                os.replace(extracted_temp_path, extracted_path)
                extracted_temp_path = None

            source_filename = os.path.basename(orig_filename) or f"DOC_{id_richiesta}{file_ext}"
            try:
                source_link = _link_source_filename(source_filename, save_path, links_dir, id_richiesta)
            except OSError as link_error:
                # A link directory problem should not discard a valid download.
                source_link = None
                log.warning("Impossibile creare il collegamento sorgente %s: %s", source_filename, link_error)
            file_size = os.path.getsize(save_path)

            log.info(
                "[%d/%d] Scaricato: %s (%s, %d bytes) — %s",
                len(downloaded) + 1,
                len(richieste_meta),
                final_filename,
                file_format,
                file_size,
                oggetto[:50],
            )

            doc_info = {
                "filename": final_filename,
                "original_filename": orig_filename,
                "path": save_path,
                "file_format": file_format,
                "file_size": file_size,
                "oggetto": oggetto,
                "richiesta_del": richiesta_del,
                "id_richiesta": id_richiesta,
                "source_link": source_link,
                "parsed_data": parsed,
            }

            if extracted_path:
                doc_info["extracted_path"] = extracted_path
                extracted_source_name = f"{os.path.splitext(source_filename)[0]}.xml"
                try:
                    _link_source_filename(extracted_source_name, extracted_path, links_dir, id_richiesta)
                except OSError as link_error:
                    log.warning("Impossibile creare il collegamento XML %s: %s", extracted_source_name, link_error)

            if parsed:
                log.info(
                    "  Dati: %s F.%s P.%s Sub.%s — %d intestati",
                    parsed.get("tipo", ""),
                    parsed.get("foglio", ""),
                    parsed.get("particella", ""),
                    parsed.get("subalterno", ""),
                    len(parsed.get("intestati", [])),
                )

            downloaded.append(doc_info)

        except Exception as e:
            for temp_file in (temp_path, extracted_temp_path):
                if temp_file and os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except OSError:
                        pass
            log.warning("Errore download %s (id=%s): %s", oggetto[:40], id_richiesta, e)

    # --- Step 3: Persist to database ---
    try:
        await _save_documents_to_db(downloaded)
    except Exception as e:
        log.warning("Errore salvataggio documenti in DB: %s", e)

    log.info("[green]%d/%d documenti scaricati[/green] da Richieste", len(downloaded), len(richieste_meta))
    return downloaded


def _parse_richieste_table(html_content: str) -> list[dict]:
    """Parse the ConsultazioneRichieste table HTML into structured metadata.

    Returns a list of dicts with keys:
      richiesta_del, oggetto, formato, costo, salva_href, id_richiesta
    """
    soup = BeautifulSoup(html_content, "html.parser")

    # Find the table with "Richiesta del" header
    target_table = None
    for table in soup.find_all("table"):
        ths = table.find_all("th")
        headers = [th.get_text(strip=True) for th in ths]
        if "Richiesta del" in headers:
            target_table = table
            break

    if not target_table:
        return []

    results = []
    for tr in target_table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue

        richiesta_del = tds[0].get_text(strip=True).replace("\xa0", " ")
        oggetto = tds[1].get_text(strip=True)
        formato = tds[2].get_text(strip=True)
        costo = tds[3].get_text(strip=True) if len(tds) > 3 else ""

        # Find "salva" link
        salva_href = ""
        id_richiesta = ""
        for td in tds:
            for a in td.find_all("a"):
                href = a.get("href", "")
                if "salva" in href:
                    salva_href = href
                    # Extract idRichiesta from URL
                    import urllib.parse

                    parsed_url = urllib.parse.urlparse(href)
                    params = urllib.parse.parse_qs(parsed_url.query)
                    id_richiesta = params.get("idRichiesta", [""])[0]
                    break
            if salva_href:
                break

        if not salva_href:
            continue

        results.append(
            {
                "richiesta_del": richiesta_del,
                "oggetto": oggetto,
                "formato": formato,
                "costo": costo,
                "salva_href": salva_href,
                "id_richiesta": id_richiesta,
            }
        )

    return results


def _filename_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]+", "_", str(value or "")).strip("_-")


def _descriptive_filename(parsed: dict, *, oggetto: str = "", request_id: str = "") -> str:
    """Return the canonical SISTER basename for a downloaded document.

    Property example: ``vi_sto_PA_FG134_PT122_SUB21``
    Subject example: ``vs_sin_FRSLSE77B54E730C_CE``
    """
    tipo = (parsed.get("tipo") or "").lower()
    subtype = (parsed.get("visura_subtype") or "").lower()
    cadastre_type = (parsed.get("tipo_catasto") or "").upper()
    province = _filename_token(parsed.get("provincia", "")).upper()
    foglio = _filename_token(parsed.get("foglio", ""))
    particella = _filename_token(parsed.get("particella", ""))
    subalterno = _filename_token(parsed.get("subalterno", ""))
    oggetto_lower = oggetto.lower()

    if "soggetto" in tipo or "soggetto" in oggetto_lower:
        if "storic" in subtype:
            view = "sto"
        elif "sintetic" in subtype:
            view = "sin"
        else:
            view = "att"
        identifier = _filename_token(parsed.get("codice_fiscale") or parsed.get("identificativo", "")).upper()
        if not identifier:
            match = re.search(r"(?<![A-Z0-9])([A-Z0-9]{16}|\d{11})(?![A-Z0-9])", oggetto.upper())
            identifier = match.group(1) if match else ""
        if identifier:
            category = {"E": "CE", "F": "CF", "T": "CT"}.get(cadastre_type)
            parts = [f"vs_{view}", identifier]
            if category:
                parts.append(category)
            return "_".join(parts)

    if tipo in {"visura_fabbricati", "visura_terreni"} or (foglio and particella):
        if "storic" in subtype:
            view = "sto"
        elif "sintetic" in subtype:
            view = "sin"
        else:
            view = "att"
        is_terreni = tipo == "visura_terreni" or cadastre_type == "T"
        parts = [f"vi_{view}" + ("_ter" if is_terreni else "")]
        if province:
            parts.append(province)
        if foglio:
            parts.append(f"FG{foglio}")
        if particella:
            parts.append(f"PT{particella}")
        if subalterno:
            parts.append(f"SUB{subalterno}")
        if len(parts) > 1:
            return "_".join(parts)

    # Preserve a useful SISTER label for document classes without parsed XML
    # identifiers, while keeping every stored download unique and traceable.
    label = _filename_token(oggetto)[:80]
    if not label:
        label = "documento"
    return f"{label}_{_filename_token(request_id)}" if request_id else label


def _unique_document_stem(
    docs_dir: str,
    preferred_stem: str,
    file_ext: str,
    *,
    include_xml: bool,
    parsed: dict,
    request_id: str,
) -> str:
    """Keep canonical names when free and add the SISTER practice id on collision."""

    def occupied(stem: str) -> bool:
        candidates = [os.path.join(docs_dir, f"{stem}{file_ext}")]
        if include_xml:
            candidates.append(os.path.join(docs_dir, f"{stem}.xml"))
        return any(os.path.lexists(path) for path in candidates)

    if not occupied(preferred_stem):
        return preferred_stem

    protocol = _filename_token(parsed.get("protocollo", ""))
    year = _filename_token(parsed.get("anno", ""))
    if not year and parsed.get("situazione_al"):
        year_match = re.search(r"(\d{4})$", str(parsed["situazione_al"]))
        year = year_match.group(1) if year_match else ""
    if protocol:
        suffix = protocol.upper()
        if year:
            suffix += f"_{year}"
    else:
        suffix = f"DOC_{_filename_token(request_id)}" if request_id else "DOC"

    candidate = f"{preferred_stem}_{suffix}"
    ordinal = 2
    while occupied(candidate):
        candidate = f"{preferred_stem}_{suffix}_{ordinal}"
        ordinal += 1
    return candidate


def _link_source_filename(source_filename: str, target_path: str, links_dir: str, request_id: str) -> str:
    """Create a legacy-name symlink pointing to the canonical document file."""
    source_filename = os.path.basename(source_filename)
    if not source_filename:
        source_filename = f"DOC_{request_id}"
    os.makedirs(links_dir, exist_ok=True)
    target_abs = os.path.abspath(target_path)
    stem, ext = os.path.splitext(source_filename)
    request_token = _filename_token(request_id) or "download"
    ordinal = 0

    while True:
        if ordinal == 0:
            link_path = os.path.join(links_dir, source_filename)
        elif ordinal == 1:
            link_path = os.path.join(links_dir, f"{stem}_{request_token}{ext}")
        else:
            link_path = os.path.join(links_dir, f"{stem}_{request_token}_{ordinal}{ext}")

        if not os.path.lexists(link_path):
            try:
                os.symlink(target_abs, link_path)
                return link_path
            except FileExistsError:
                # Another downloader may have created the alias after our check.
                ordinal += 1
                continue

        if os.path.islink(link_path):
            current = os.readlink(link_path)
            current_abs = os.path.abspath(os.path.join(os.path.dirname(link_path), current))
            if current_abs == target_abs:
                return link_path
            if not os.path.exists(link_path):
                # Repair a broken source alias in place.
                os.unlink(link_path)
                try:
                    os.symlink(target_abs, link_path)
                    return link_path
                except FileExistsError:
                    ordinal += 1
                    continue

        # Keep an existing file or link intact and add a deterministic suffix.
        ordinal += 1


def _extract_p7m(file_path: str) -> str | None:
    """Extract the signed content from a P7M file via aecs4u-crypto (no openssl).

    Returns the path to the extracted file, or None on failure.
    """
    from aecs4u_crypto import extract_p7m_payload, P7MError

    out_path = file_path.rsplit(".p7m", 1)[0]
    if not out_path or out_path == file_path:
        out_path = file_path + ".extracted"
    try:
        payload = extract_p7m_payload(open(file_path, "rb").read())
        if not payload:
            return None
        with open(out_path, "wb") as f:
            f.write(payload)
        log.info("P7M estratto: %s → %s (%d bytes)", file_path, out_path, len(payload))
        return out_path
    except P7MError as e:
        log.warning("Estrazione P7M fallita %s: %s", file_path, e)
    except Exception as e:
        log.warning("Errore estrazione P7M %s: %s", file_path, e)
    return None


# Large Visure per Soggetto exceed 50 kB; the column is unbounded text, so only a runaway file is cut.
XML_CONTENT_LIMIT = 5_000_000


def _parse_visura_xml(file_path: str) -> dict | None:
    """Parse a SISTER visura XML file and extract structured data.

    Handles both plain .xml and .p7m (signed XML) files.
    P7M files are first extracted via aecs4u-crypto (CMS payload extraction).
    """
    try:
        xml_path = file_path

        # P7M: extract signed content via openssl
        if file_path.lower().endswith(".p7m"):
            extracted = _extract_p7m(file_path)
            if extracted:
                xml_path = extracted
            else:
                log.warning("Fallback: tentativo parsing diretto P7M %s", file_path)

        content = open(xml_path, "r", encoding="utf-8", errors="ignore").read()

        soup = BeautifulSoup(content, "xml")
        if not soup.find():
            soup = BeautifulSoup(content, "html.parser")

        result = {
            "tipo": "",
            "provincia": "",
            "comune": "",
            "foglio": "",
            "particella": "",
            "subalterno": "",
            "sezione_urbana": "",
            "tipo_catasto": "",
            "intestati": [],
            "immobile": {},
            "classamento": [],
            "indirizzo": "",
            "xml_content": content[:XML_CONTENT_LIMIT],
        }

        # Determine document type and subtype from XML root element and TitoloVisura
        titolo_tag = soup.find("TitoloVisura")
        titolo = (titolo_tag.get("Titolo", "") if titolo_tag else "").lower()
        # SituazioneAl="20260604" → "04/06/2026"
        situazione_raw = titolo_tag.get("SituazioneAl", "") if titolo_tag else ""
        if len(situazione_raw) == 8 and situazione_raw.isdigit():
            result["situazione_al"] = f"{situazione_raw[6:8]}/{situazione_raw[4:6]}/{situazione_raw[0:4]}"
        else:
            result["situazione_al"] = situazione_raw or None

        def _subtype_from_titolo(t: str) -> str:
            if "complet" in t:
                return "storica_completa"
            if "storic" in t and "analitic" in t:
                return "storica_analitica"
            if "storic" in t and "sintetic" in t:
                return "storica_sintetica"
            if "storic" in t:
                return "storica"
            if "attual" in t and "sintetic" in t:
                return "sintetica"
            if "attual" in t:
                return "attuale"
            return ""

        if (
            soup.find("VisuraFabbricatiStorica")
            or soup.find("VisuraFabbricatiAttuale")
            or soup.find("VisuraFabbricati")
        ):
            result["tipo"] = "visura_fabbricati"
            result["tipo_catasto"] = "F"
            result["visura_subtype"] = _subtype_from_titolo(titolo)
        elif (
            soup.find("VisuraTerreniStorica")
            or soup.find("VisuraTerreniAttuale")
            or soup.find("VisuraTerrenoStorica")
            or soup.find("VisuraTerrenoAttuale")
            or soup.find("VisuraTerreno")
        ):
            result["tipo"] = "visura_terreni"
            result["tipo_catasto"] = "T"
            result["visura_subtype"] = _subtype_from_titolo(titolo)
        elif soup.find("VisuraSoggetto") or soup.find("VisuraSoggettoStorica") or soup.find("VisuraSoggettoAttuale"):
            result["tipo"] = "visura_soggetto"
            result["visura_subtype"] = _subtype_from_titolo(titolo)
        else:
            result["tipo"] = "visura"

        # Extract property identifiers from DatiRichiesta attributes
        dati_rich = soup.find("DatiRichiesta")
        if dati_rich:
            result["provincia"] = dati_rich.get("Provincia", "")
            result["comune"] = dati_rich.get("Comune", "")
            result["foglio"] = dati_rich.get("Foglio", "")
            result["particella"] = dati_rich.get("ParticellaNum", "") or dati_rich.get("Particella", "")
            result["subalterno"] = dati_rich.get("Subalterno", "")
            result["sezione_urbana"] = dati_rich.get("SezUrbana", "") or dati_rich.get("SezCensuaria", "")
            result["tipo_catasto"] = dati_rich.get("TipoCatasto", "") or result["tipo_catasto"]
            result["protocollo"] = dati_rich.get("Protocollo", "")
            result["anno"] = dati_rich.get("Anno", "")

        # A subject visura identifies its queried party separately from the
        # owners listed for the returned properties.
        soggetto = soup.find("SoggettoPF")
        if soggetto is None:
            soggetto = soup.find("SoggettoPG")
        if soggetto:
            identifier = (
                soggetto.get("CodiceFiscale")
                or soggetto.get("PartitaIVA")
                or soggetto.get("PIVA")
                or soggetto.get("Identificativo")
                or ""
            ).strip().upper()
            if identifier:
                result["identificativo"] = identifier
                if len(identifier) == 16:
                    result["codice_fiscale"] = identifier

        # Extract immobile data from IdentificativoDefinitivo + DatiClassamento
        for id_def in soup.find_all("IdentificativoDefinitivo"):
            attrs = dict(id_def.attrs)
            if attrs.get("Foglio") and not result["foglio"]:
                result["foglio"] = attrs.get("Foglio", "")
                result["particella"] = attrs.get("ParticellaNum", "") or attrs.get("Particella", "")
                result["subalterno"] = attrs.get("Subalterno", "")
                result["sezione_urbana"] = attrs.get("SezUrbana", "")
            partita_el = id_def.find("Partita")
            if partita_el and partita_el.string:
                attrs["Partita"] = partita_el.string.strip()
            if attrs.get("Foglio"):
                result["immobile"] = attrs
                break

        # Extract classamento (Fabbricati)
        for class_el in soup.find_all("DatiClassamentoF"):
            entry = dict(class_el.attrs)
            partita_el = class_el.find("Partita")
            if partita_el and partita_el.string:
                entry["Partita"] = partita_el.string.strip()
            result["classamento"].append(entry)

        # Extract classamento (Terreni)
        for class_el in soup.find_all("DatiClassamentoT"):
            result["classamento"].append(dict(class_el.attrs))

        # Extract indirizzo
        indirizzo_el = soup.find("IndirizzoImm")
        if indirizzo_el and indirizzo_el.string:
            result["indirizzo"] = indirizzo_el.string.strip()

        # Extract intestati from Intestato elements (attributes + children)
        # (the historical documents also list the former owners under StoriaIntestazione: not the current ones)
        for intestato_el in soup.find_all("Intestato"):
            if intestato_el.find_parent("StoriaIntestazione") is not None:
                continue
            intestato = dict(intestato_el.attrs)
            for child in intestato_el.children:
                if hasattr(child, "name") and child.name:
                    if child.string:
                        intestato[child.name] = child.string.strip()
                    elif child.attrs:
                        intestato[child.name] = dict(child.attrs)
            if intestato:
                result["intestati"].append(intestato)

        # Also check IntestazioneAttuale for Soggetto elements
        for sogg_el in soup.find_all("Soggetto"):
            sogg = dict(sogg_el.attrs)
            for child in sogg_el.children:
                if hasattr(child, "name") and child.name and child.string:
                    sogg[child.name] = child.string.strip()
            if sogg:
                result["intestati"].append(sogg)

        return result

    except Exception as e:
        log.warning("Errore parsing XML %s: %s", file_path, e)
        return None


def _parse_visura_pdf(file_path: str) -> dict | None:
    """Extract document type and metadata from a SISTER visura PDF using pdftotext.

    Returns a dict in the same shape as _parse_visura_xml, or None on failure.
    Only the fields reliably present in PDF output are populated:
    tipo, visura_subtype, tipo_catasto, provincia, comune, foglio, particella,
    subalterno, sezione_urbana, indirizzo, situazione_al.
    """
    import re as _re
    import subprocess

    try:
        text = subprocess.check_output(["pdftotext", file_path, "-"], stderr=subprocess.DEVNULL, text=True, timeout=15)
    except Exception as e:
        log.debug("_parse_visura_pdf: pdftotext failed for %s: %s", file_path, e)
        return None

    t = text.lower()

    # --- document type + subtype ---
    def _subtype(t: str) -> str:
        if "storica" in t and "sintetica" in t:
            return "storica_sintetica"
        if "storica" in t and "analitica" in t:
            return "storica_analitica"
        if "storica" in t and "completa" in t:
            return "storica_completa"
        if "storica" in t:
            return "storica"
        if "attuale" in t and "sintetica" in t:
            return "sintetica"
        if "attuale" in t:
            return "attuale"
        return ""

    tipo: str | None = None
    subtype = ""
    tipo_catasto: str | None = None

    if "per soggetto" in t or "visura soggetto" in t:
        tipo = "visura_soggetto"
        subtype = _subtype(t)
    elif "fabbricat" in t or "urbano" in t:
        tipo = "visura_fabbricati"
        subtype = _subtype(t)
        tipo_catasto = "F"
    elif "terren" in t:
        tipo = "visura_terreni"
        subtype = _subtype(t)
        tipo_catasto = "T"
    else:
        tipo = "visura"

    result: dict = {"tipo": tipo, "visura_subtype": subtype or None, "tipo_catasto": tipo_catasto}

    # --- situazione al (reference date) ---
    m = _re.search(r"situazione[^\d]+(\d{2}/\d{2}/\d{4})", t)
    if m:
        result["situazione_al"] = m.group(1)

    # --- provincia + comune from "Direzione Provinciale di <Comune>" header ---
    m = _re.search(r"direzione provinciale di ([A-Z][A-Za-zÀ-ÿ '\-]+)\b", text)
    if m:
        result["comune"] = m.group(1).strip().upper()

    # --- coordinates (catasto documents only) ---
    if tipo in ("visura_fabbricati", "visura_terreni"):
        # "Comune di RAVENNA (H199) (RA)"
        m = _re.search(r"Comune di ([A-Z][A-Z ]+)\s*\([A-Z0-9]+\)\s*\(([A-Z]{2})\)", text)
        if m:
            result["comune"] = m.group(1).strip()
            result["provincia"] = m.group(2)

        # Foglio / Particella / Subalterno
        m = _re.search(r"Foglio[:\s]+(\d+)", text, _re.IGNORECASE)
        if m:
            result["foglio"] = m.group(1)
        m = _re.search(r"Particella[:\s]+(\d+)", text, _re.IGNORECASE)
        if m:
            result["particella"] = m.group(1)
        m = _re.search(r"Subalterno[:\s]+(\d+)", text, _re.IGNORECASE)
        if m:
            result["subalterno"] = m.group(1)

        m = _re.search(r"Indirizzo[:\s]+([A-Z]{2,}[^\n]{5,60})", text)
        if m:
            result["indirizzo"] = m.group(1).strip()

    return result


def _parse_ispezione_ipotecaria_pdf(file_path: str) -> dict | None:
    """Extract searchable data and complete sections from a SISTER inspection PDF."""
    import re as _re
    import subprocess

    try:
        text = subprocess.check_output(
            ["pdftotext", "-layout", file_path, "-"], stderr=subprocess.DEVNULL, text=True, timeout=30
        )
    except Exception as e:
        log.debug("_parse_ispezione_ipotecaria_pdf: pdftotext failed for %s: %s", file_path, e)
        return None

    text = text.replace("\x00", "").replace("\x0c", "\n")
    lines = [line.rstrip() for line in text.splitlines()]
    searchable = "\n".join(lines)
    if not searchable.strip():
        return None

    def _search(pattern: str, source: str = searchable, flags: int = _re.IGNORECASE) -> str | None:
        match = _re.search(pattern, source, flags)
        return match.group(1).strip() if match else None

    kind = "ispezione"
    if _re.search(r"nota\s+di\s+trascrizione", searchable, _re.IGNORECASE):
        kind = "nota_trascrizione"
    elif _re.search(r"nota\s+di\s+iscrizione", searchable, _re.IGNORECASE):
        kind = "nota_iscrizione"
    elif _re.search(r"nota\s+di\s+annotazione", searchable, _re.IGNORECASE):
        kind = "nota_annotazione"

    result: dict = {
        "tipo": "ispezione_ipotecaria",
        "inspection_document_type": kind,
        "raw_text": searchable.strip(),
        "sections": [],
        "formalities": [],
    }

    result["inspection_number"] = _search(r"\bn\.\s*T1?\s*(\d+)\s+del\s+\d{2}/\d{2}/\d{4}")
    result["inspection_date"] = _search(r"\bn\.\s*T1?\s*\d+\s+del\s+(\d{2}/\d{2}/\d{4})")
    result["office"] = _search(r"Ufficio Provinciale di\s+([^\n-]+)") or _search(
        r"Direzione Provinciale di\s+([^\n]+)"
    )
    result["document_date"] = _search(r"\bData\s+(\d{2}/\d{2}/\d{4})")
    result["document_time"] = _search(r"\bOra\s+(\d{2}:\d{2}:\d{2})")
    result["requester"] = _search(r"\bRichiedente\s+(.+?)(?=\s+Tassa\s+versata\b|\n|$)")
    result["requester_cf"] = _search(r"per conto di\s*\n?\s*([A-Z0-9]{16})")
    result["inspection_mode"] = _search(r"^(Ispezione telematica[^\n]*)", flags=_re.IGNORECASE | _re.MULTILINE)
    result["request_reason"] = _search(r"^\s*Motivazione\s+([^\n]+)", flags=_re.IGNORECASE | _re.MULTILINE)
    result["fee"] = _search(r"Tassa versata\s+([^\n]+)")
    result["period_from"] = _search(r"Periodo informatizzato dal\s+(\d{2}/\d{2}/\d{4})")
    result["period_to"] = _search(r"Periodo informatizzato dal\s+\d{2}/\d{2}/\d{4}\s+al\s+(\d{2}/\d{2}/\d{4})")

    municipality = _re.search(
        r"Comune(?:\s+di)?\s+(?:[A-Z0-9]{4}\s*-\s*)?([A-ZÀ-ÖØ-Ý'’ .-]+?)\s*\(([A-Z]{2})\)",
        searchable,
        _re.IGNORECASE,
    )
    if municipality:
        result["comune"] = municipality.group(1).strip(" .-").upper()
        result["provincia"] = municipality.group(2).upper()

    cadastre = _search(r"Tipo catasto\s*:\s*([^\n]+)") or _search(r"\bCatasto\s+((?:FABBRICATI|TERRENI))")
    if cadastre:
        result["tipo_catasto"] = "T" if "terren" in cadastre.casefold() else "F"
    for field, pattern in (
        ("foglio", r"\bFoglio\s*:?\s*(\d+)"),
        ("particella", r"\bParticella\s*:?\s*(\d+)"),
        ("subalterno", r"\bSubalterno\s*:?\s*(\d+)"),
        ("sezione_urbana", r"\bSezione urbana\s+([A-Z0-9]+)"),
    ):
        value = _search(pattern)
        if value:
            result[field] = value

    for field, pattern in (
        ("registro_generale", r"Registro generale n\.\s*(\d+)"),
        ("registro_particolare", r"Registro particolare n\.\s*(\d+)"),
        ("presentazione", r"Presentazione n\.\s*(\d+)\s+del\s+(\d{2}/\d{2}/\d{4})"),
    ):
        match = _re.search(pattern, searchable, _re.IGNORECASE)
        if match:
            result[field] = " ".join(part.strip() for part in match.groups() if part)

    # Capture the report's formality list, including each line of its description.
    start_re = _re.compile(
        r"^\s*(\d+)\.\s+(ISCRIZIONE|TRASCRIZIONE|ANNOTAZIONE)\s+del\s+(\d{2}/\d{2}/\d{4})"
        r"\s*-\s*Registro Particolare\s+(\d+)\s+Registro Generale\s+(\d+)",
        _re.IGNORECASE,
    )
    starts = [(i, start_re.match(line)) for i, line in enumerate(lines)]
    starts = [(i, match) for i, match in starts if match]
    for idx, (line_no, match) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        block = [line.strip() for line in lines[line_no:end] if line.strip()]
        details = block[1:]
        official = None
        repertory = None
        for line in details:
            official_match = _re.search(r"Pubblico ufficiale\s+(.+?)\s+Repertorio\s+(.+)$", line, _re.IGNORECASE)
            if official_match:
                official = official_match.group(1).strip()
                repertory = official_match.group(2).strip()
                break
        result["formalities"].append(
            {
                "number": match.group(1),
                "type": match.group(2).upper(),
                "date": match.group(3),
                "registro_particolare": match.group(4),
                "registro_generale": match.group(5),
                "public_official": official,
                "repertory": repertory,
                "description": next(
                    (line for line in details if "derivante da" in line.casefold() or " - " in line), ""
                ),
                "details": details,
            }
        )

    # Retain all A-D material and also split aligned label/value pairs for easier use.
    section_re = _re.compile(r"^\s*Sezione\s+([ABCD])\s*[-–]\s*(.+?)\s*$", _re.IGNORECASE)
    section_starts = [(i, section_re.match(line)) for i, line in enumerate(lines)]
    section_starts = [(i, match) for i, match in section_starts if match]
    for idx, (line_no, match) in enumerate(section_starts):
        end = section_starts[idx + 1][0] if idx + 1 < len(section_starts) else len(lines)
        section_lines = [line.rstrip() for line in lines[line_no + 1 : end] if line.strip()]
        fields = []
        for line in section_lines:
            pair = _re.match(r"^\s{1,}(.+?)\s{2,}(\S.*)$", line)
            if pair:
                label = pair.group(1).strip()
                value = pair.group(2).strip()
                if label and value and len(label) <= 90:
                    fields.append({"label": label, "value": value})
        result["sections"].append(
            {
                "code": match.group(1).upper(),
                "title": match.group(2).strip(),
                "content": "\n".join(section_lines),
                "fields": fields,
            }
        )

    return result


async def _persist_flattened_xml(session, document_id: int, content: str | None) -> None:
    """Store XML elements and attributes in the relational document tree, then fill the typed tables from it."""
    if not content:
        return

    from sqlalchemy import delete, select

    from .visura_xml_models import DocumentXmlAttribute, DocumentXmlNode

    payload = content.replace("\x00", "").encode("utf-8", "replace")
    try:
        from lxml import etree

        root = etree.fromstring(
            payload,
            etree.XMLParser(recover=True, resolve_entities=False, no_network=True),
        )
    except (etree.XMLSyntaxError, ValueError):
        return
    if root is None:
        return

    node_ids = select(DocumentXmlNode.id).where(DocumentXmlNode.document_id == document_id)
    await session.execute(delete(DocumentXmlAttribute).where(DocumentXmlAttribute.node_id.in_(node_ids)))
    await session.execute(delete(DocumentXmlNode).where(DocumentXmlNode.document_id == document_id))

    def local_tag(tag: str) -> str:
        return etree.QName(tag).localname if isinstance(tag, str) else str(tag)

    async def visit(element, parent_id: int | None, ordinal: int) -> None:
        node = DocumentXmlNode(
            document_id=document_id,
            parent_id=parent_id,
            ordinal=ordinal,
            tag=local_tag(element.tag),
            text=(element.text or "").strip() or None,
        )
        session.add(node)
        await session.flush()
        for name, value in element.attrib.items():
            session.add(DocumentXmlAttribute(node_id=node.id, name=local_tag(name), value=value))
        for child_ordinal, child in enumerate(element):
            await visit(child, node.id, child_ordinal)

    await visit(root, None, 0)

    # the same XML into the typed tables (properties, owners, ownership acts); a document the typed reader cannot
    # place keeps its node tree, and a failure there must not lose the document
    from .xml_ingest import ingest_visura_xml

    try:
        async with session.begin_nested():
            await ingest_visura_xml(session, document_id, content)
    except Exception as exc:  # noqa: BLE001
        log.warning("Tabelle tipizzate non popolate per il documento %s: %s", document_id, exc)


async def _save_documents_to_db(documents: list[dict]) -> None:
    """Persist downloaded documents to the visura_documents + document_metadata tables, skipping duplicates."""

    from sqlalchemy import text

    from .database import _get_session_factory, get_or_create_location, is_db_writable
    from .db_models import DocumentMetadata, VisuraDocument

    if not is_db_writable():
        return
    session_factory = _get_session_factory()
    saved = 0
    skipped = 0
    async with session_factory() as session:
        for doc in documents:
            parsed = doc.get("parsed_data") or {}
            foglio = parsed.get("foglio", "")
            particella = parsed.get("particella", "")
            subalterno = parsed.get("subalterno", "")
            doc_type = parsed.get("tipo", "")

            # Ispezione PDFs are separate records and notes for the same parcel must
            # not be deduplicated by cadastral coordinates.
            if doc_type == "ispezione_ipotecaria":
                existing = await session.execute(
                    text("SELECT id FROM visura_documents WHERE filename = :name OR file_path = :path LIMIT 1"),
                    {"name": doc.get("filename", ""), "path": doc.get("path") or ""},
                )
                if existing.fetchone():
                    skipped += 1
                    continue
            # Other document types retain the property-level duplicate check: the same file, or the same kind of
            # document (visura subtype and reference date) for the same property. A planimetry or an older visura
            # of the unit is a different document and must not hide a new one.
            elif foglio and particella:
                existing = await session.execute(
                    text(
                        "SELECT vd.id FROM visura_documents vd"
                        " JOIN document_metadata m ON vd.id = m.id"
                        " LEFT JOIN cadastral_locations loc ON m.location_id = loc.id"
                        " WHERE vd.filename = :name OR (:path <> '' AND vd.file_path = :path)"
                        " OR (loc.sheet = :f AND loc.parcel = :p AND loc.subunit = :s AND vd.document_type = :t"
                        "     AND m.view_subtype IS NOT DISTINCT FROM CAST(:v AS VARCHAR)"
                        "     AND m.reference_date IS NOT DISTINCT FROM CAST(:d AS VARCHAR))"
                        " LIMIT 1"
                    ),
                    {
                        "name": doc.get("filename", ""),
                        "path": doc.get("path") or "",
                        "f": foglio,
                        "p": particella,
                        "s": subalterno,
                        "t": doc_type,
                        "v": parsed.get("visura_subtype") or None,
                        "d": parsed.get("situazione_al") or None,
                    },
                )
                if existing.fetchone():
                    log.debug(
                        "Documento duplicato saltato: %s F.%s P.%s Sub.%s", doc_type, foglio, particella, subalterno
                    )
                    skipped += 1
                    continue

            row = VisuraDocument(
                document_type=doc_type,
                file_format=doc.get("file_format", ""),
                filename=doc.get("filename", ""),
                file_path=doc.get("path"),
                file_size=doc.get("file_size"),
                subject=doc.get("oggetto"),
                requested_at=doc.get("richiesta_del"),
            )
            session.add(row)
            await session.flush()  # get row.id before creating child

            xml_content = parsed.get("xml_content")
            location_id = None
            if xml_content or foglio or particella:
                location_id = await get_or_create_location(
                    session,
                    cadastre_type=parsed.get("tipo_catasto") or "",
                    province=parsed.get("provincia") or "",
                    municipality=parsed.get("comune") or "",
                    sheet=foglio,
                    parcel=particella,
                    subunit=subalterno,
                    section=parsed.get("sezione_urbana") or "",
                )
            meta = DocumentMetadata(
                id=row.id,
                location_id=location_id,
                view_subtype=parsed.get("visura_subtype") or None,
                reference_date=parsed.get("situazione_al") or None,
                content=xml_content.replace("\x00", "") if xml_content else None,
            )
            session.add(meta)
            await session.flush()
            await _persist_flattened_xml(session, row.id, xml_content)

            saved += 1
        await session.commit()
    log.info("Documenti: %d salvati, %d duplicati saltati", saved, skipped)


async def _find_intestati_button(page):
    """Locate the Intestati submit button on the page."""
    selectors = [
        "input[name='intestati'][value='Intestati']",
        "input[value='Intestati']",
        "input[name='intestati']",
        "button:has-text('Intestati')",
        "input[type='submit'][value*='ntestat']",
        "*[value='Intestati']",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0:
                return locator.first
        except Exception:
            continue
    return None


def _extract_intestati_from_page(html_content: str) -> list[dict]:
    """Extract intestati table rows from a page's HTML content."""
    soup = BeautifulSoup(html_content, "html.parser")
    intestati_keywords = {"Cognome", "Nome", "Nominativo o denominazione", "Codice fiscale", "Titolarità"}

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if any(kw in headers for kw in intestati_keywords):
            rows = []
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if cells:
                    while len(cells) < len(headers):
                        cells.append("")
                    rows.append(dict(zip(headers, cells)))
            if rows:
                return rows

    # Fallback: try any table that doesn't look like an immobili table
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if headers and "Foglio" not in headers and "Particella" not in headers:
            rows = []
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if cells:
                    while len(cells) < len(headers):
                        cells.append("")
                    rows.append(dict(zip(headers, cells)))
            if rows:
                return rows

    return []


async def _extract_intestati_playwright(page) -> list[dict]:
    """Extract intestati table using Playwright locators (no BS4)."""
    tables = page.locator("table.listaIsp4, table[class*='lista']")
    count = await tables.count()

    for i in range(count):
        table = tables.nth(i)
        headers_els = table.locator("th")
        h_count = await headers_els.count()
        if h_count == 0:
            continue
        headers = []
        for hi in range(h_count):
            headers.append((await headers_els.nth(hi).inner_text()).strip())

        # Check if this is an intestati table
        intestati_keywords = {"Cognome", "Nome", "Nominativo o denominazione", "Codice fiscale", "Titolarità"}
        if not any(kw in headers for kw in intestati_keywords):
            continue

        rows = []
        tr_els = table.locator("tbody tr, tr")
        tr_count = await tr_els.count()
        for ri in range(tr_count):
            td_els = tr_els.nth(ri).locator("td")
            td_count = await td_els.count()
            if td_count == 0:
                continue
            cells = []
            for ci in range(td_count):
                cells.append((await td_els.nth(ci).inner_text()).strip())
            while len(cells) < len(headers):
                cells.append("")
            rows.append(dict(zip(headers, cells)))
        if rows:
            return rows

    return []


async def _extract_visura_immobile_playwright(page) -> dict | None:
    """Extract visura immobile data from the result page using Playwright."""
    result = {}
    try:
        tables = page.locator("table.listaIsp4, table[class*='lista']")
        count = await tables.count()
        for i in range(count):
            table = tables.nth(i)
            html = await table.inner_html(timeout=5000)
            if "Foglio" in html or "Particella" in html or "Categoria" in html or "Rendita" in html:
                parsed = parse_table(html)
                if parsed:
                    result["data"] = parsed
                    break

        # Capture page text for any additional info
        body_text = await page.inner_text("body")
        if "NESSUNA CORRISPONDENZA" in body_text:
            result["error"] = "NESSUNA CORRISPONDENZA TROVATA"
    except Exception as e:
        log.debug("Errore estrazione visura immobile: %s", e)
        result["error"] = str(e)

    return result or None


async def _search_soggetto(
    page,
    codice_fiscale,
    tipo_catasto="E",
    provincia=None,
    comune=None,
    motivo="Esplorazione",
    per_conto_di=None,
):
    import os

    if per_conto_di is None:
        per_conto_di = os.getenv("ADE_USERNAME", "")
    """National search by codice fiscale on the SISTER portal.

    If provincia is None, selects "NAZIONALE" for a nationwide search.
    tipo_catasto: 'E' = both, 'T' = Terreni, 'F' = Fabbricati.
    """
    time0 = time.time()
    page_logger = PageLogger("soggetto")
    prov_label = provincia or "NAZIONALE"
    log.info(
        "[bold]Ricerca soggetto[/bold] CF=%s tipo=%s provincia=%s",
        codice_fiscale,
        tipo_catasto,
        prov_label,
    )

    # STEP 1-2: Cambia Ufficio → NAZIONALE (or the given province)
    await _set_office(page, page_logger, provincia)

    # STEP 3: Click "Persona fisica" in the left menu
    log.info("Navigando a Persona fisica...")
    await page.get_by_role("link", name="Persona fisica").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "persona_fisica")

    # STEP 4: Select tipo catasto
    log.info("Tipo catasto: [cyan]%s[/cyan]", tipo_catasto)
    try:
        await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)
    except Exception as e:
        log.warning("Errore selezione tipo catasto: %s", e)

    # STEP 5: search by codice fiscale, or by cognome/nome/data e luogo di nascita (the other inputs are
    # filled from the request's form fields, see _apply_form_fields)
    if codice_fiscale:
        log.info("Codice fiscale: [cyan]%s[/cyan]", codice_fiscale)

        # Click the Codice Fiscale radio button (field name: selDatiAna, value: CF_PF)
        cf_radio = page.locator("input[name='selDatiAna'][value='CF_PF'], input[name='selDatiAna'][value='CF']")
        if await cf_radio.count() == 0:
            cf_radio = page.locator("input[type='radio']").last
        await cf_radio.first.click()

        # Fill the codice fiscale field (field name: cod_fisc_pf)
        cf_field = page.locator("input[name='cod_fisc_pf']")
        if await cf_field.count() == 0:
            cf_field = page.locator("input[name='codFiscale']")
        if await cf_field.count() == 0:
            cf_field = page.locator("input[name='codiceFiscale']")
        await cf_field.click()
        await cf_field.fill(codice_fiscale.upper())
    else:
        log.info("Ricerca per dati anagrafici")
        await page.locator("input[name='selDatiAna'][value='cognome']").check()

    # STEP 5.1: Fill richiedente and motivo
    if per_conto_di:
        richiedente = page.locator("input[name='richiedente']")
        if await richiedente.count() > 0:
            await richiedente.fill(per_conto_di)

    if motivo:
        motivo_field = page.locator("input[name='motivoText']")
        if await motivo_field.count() == 0:
            motivo_field = page.locator("input[name='motivo']")
        if await motivo_field.count() > 0:
            await motivo_field.fill(motivo)

    await _apply_form_fields(page)

    # STEP 6: Submit search
    await page_logger.log(page, "form_compilato")
    log.info("Esecuzione ricerca soggetto...")
    ricerca_btn = page.locator("input[name='ricerca'][value='Ricerca']")
    if await ricerca_btn.count() == 0:
        ricerca_btn = page.locator("input[type='submit'][value='Ricerca']")
    await ricerca_btn.click()
    await page.wait_for_load_state("networkidle", timeout=60000)
    await page_logger.log(page, "risultati_soggetto")

    # STEP 7: Check for errors
    page_text = await page.inner_text("body")
    if "NESSUNA CORRISPONDENZA TROVATA" in page_text:
        elapsed = time.time() - time0
        log.warning("Nessuna corrispondenza trovata (%.1fs)", elapsed)
        return {
            "soggetto": codice_fiscale,
            "immobili": [],
            "total_results": 0,
            "error": "NESSUNA CORRISPONDENZA TROVATA",
        }

    # STEP 8: Extract results table
    log.info("Estraendo risultati soggetto...")
    immobili = []
    try:
        selectors = [
            "table.listaIsp4",
            "table[class*='lista']",
            "table:has(th:text('Foglio'))",
            "table:has(th:text('Comune'))",
            "table",
        ]

        for selector in selectors:
            try:
                table_locator = page.locator(selector)
                count = await table_locator.count()
                if count > 0:
                    for i in range(count):
                        try:
                            table_elem = table_locator.nth(i)
                            table_html = await table_elem.inner_html(timeout=10000)
                            if any(kw in table_html for kw in ("Foglio", "Particella", "Comune", "Provincia")):
                                immobili = parse_table(table_html)
                                log.info("[green]%d risultati[/green] estratti (%s)", len(immobili), selector)
                                break
                        except Exception as e:
                            log.debug("Errore tabella %d: %s", i, e)
                            continue
                    if immobili:
                        break
            except Exception as e:
                log.debug("Errore selettore '%s': %s", selector, e)
                continue
    except Exception as e:
        log.error("Errore estrazione risultati soggetto: %s", e)

    elapsed = time.time() - time0
    log.info(
        "[green]Ricerca soggetto completata[/green] in %.1fs — %d risultati",
        elapsed,
        len(immobili),
    )

    return {
        "soggetto": codice_fiscale,
        "immobili": immobili,
        "total_results": len(immobili),
    }


async def run_visura_soggetto(
    page,
    codice_fiscale,
    tipo_catasto="E",
    provincia=None,
    motivo="Esplorazione",
    per_conto_di=None,
):
    """National search by codice fiscale: the properties of the subject in every province.

    Walks search → homonym → province → Immobili (see ``run_soggetto_immobili``); ``motivo`` and
    ``per_conto_di`` keep their defaults.
    """
    return await run_soggetto_immobili(page, codice_fiscale, tipo_catasto=tipo_catasto, provincia=provincia)


async def run_visura_persona_giuridica(
    page,
    identificativo,
    tipo_catasto="E",
    provincia=None,
    motivo="Esplorazione",
    per_conto_di=None,
):
    """Search by legal entity (P.IVA or denominazione) on SISTER.

    identificativo: partita IVA (11 digits) or company name (denominazione).
    If provincia is None, selects "NAZIONALE" for nationwide search.
    """
    import os

    if per_conto_di is None:
        per_conto_di = os.getenv("ADE_USERNAME", "")

    time0 = time.time()
    page_logger = PageLogger("persona_giuridica")
    prov_label = provincia or "NAZIONALE"
    log.info(
        "[bold]Ricerca persona giuridica[/bold] id=%s tipo=%s provincia=%s",
        identificativo,
        tipo_catasto,
        prov_label,
    )

    # STEP 1-2: Cambia Ufficio → NAZIONALE (or the given province)
    await _set_office(page, page_logger, provincia)

    # STEP 3: Click "Persona giuridica" in the left menu
    log.info("Navigando a Persona giuridica...")
    await page.get_by_role("link", name="Persona giuridica").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "persona_giuridica")

    # STEP 4: Select tipo catasto
    try:
        await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)
    except Exception as e:
        log.warning("Errore selezione tipo catasto: %s", e)

    # STEP 5: Fill the search field
    # PNF form has denominazione and/or codice fiscale/P.IVA fields
    # If the identifier looks like a P.IVA (11 digits) or CF (16 chars), use the CF field
    is_fiscal_code = len(identificativo.strip()) in (11, 16) and identificativo.strip().isalnum()

    if is_fiscal_code:
        log.info("Codice fiscale/P.IVA: [cyan]%s[/cyan]", identificativo)
        # PNF form: radio name="selCfDn" value="CF_PNF", field name="cod_fisc"
        cf_radio = page.locator("input[name='selCfDn'][value='CF_PNF']")
        if await cf_radio.count() == 0:
            cf_radio = page.locator("input[type='radio'][value='CF_PNF']")
        if await cf_radio.count() == 0:
            cf_radio = page.locator("input[type='radio'][value='CF']")
        if await cf_radio.count() > 0:
            await cf_radio.click()

        cf_field = page.locator("input[name='cod_fisc']")
        if await cf_field.count() == 0:
            cf_field = page.locator("input[name='codFiscale']")
        await cf_field.click()
        await cf_field.fill(identificativo.upper())
    else:
        log.info("Denominazione: [cyan]%s[/cyan]", identificativo)
        # PNF form: radio name="selCfDn" value="denominazione" (default/checked)
        denom_radio = page.locator("input[name='selCfDn'][value='denominazione']")
        if await denom_radio.count() > 0:
            await denom_radio.click()

        denom_field = page.locator("input[name='denominazione']")
        await denom_field.click()
        await denom_field.fill(identificativo.upper())

    # STEP 5.1: Fill richiedente and motivo
    if per_conto_di:
        richiedente = page.locator("input[name='richiedente']")
        if await richiedente.count() > 0:
            await richiedente.fill(per_conto_di)
    if motivo:
        motivo_field = page.locator("input[name='motivoText']")
        if await motivo_field.count() == 0:
            motivo_field = page.locator("input[name='motivo']")
        if await motivo_field.count() > 0:
            await motivo_field.fill(motivo)

    await _apply_form_fields(page)

    # STEP 6: Submit
    await page_logger.log(page, "form_compilato")
    log.info("Esecuzione ricerca persona giuridica...")
    ricerca_btn = page.locator("input[name='ricerca'][value='Ricerca']")
    if await ricerca_btn.count() == 0:
        ricerca_btn = page.locator("input[type='submit'][value='Ricerca']")
    await ricerca_btn.click()
    await page.wait_for_load_state("networkidle", timeout=60000)
    await page_logger.log(page, "risultati_pnf")

    # STEP 7: Check for errors
    page_text = await page.inner_text("body")
    if "NESSUNA CORRISPONDENZA TROVATA" in page_text:
        elapsed = time.time() - time0
        log.warning("Nessuna corrispondenza trovata (%.1fs)", elapsed)
        return {
            "soggetto": identificativo,
            "immobili": [],
            "total_results": 0,
            "error": "NESSUNA CORRISPONDENZA TROVATA",
        }

    # STEP 8: Extract results
    immobili = _extract_result_tables(await page.content())
    log.info("[green]%d risultati[/green] estratti", len(immobili))

    elapsed = time.time() - time0
    log.info("[green]Ricerca PNF completata[/green] in %.1fs — %d risultati", elapsed, len(immobili))

    return {
        "soggetto": identificativo,
        "immobili": immobili,
        "total_results": len(immobili),
    }


async def run_elenco_immobili(
    page,
    provincia,
    comune,
    tipo_catasto="T",
    foglio=None,
    sezione=None,
    motivo="Esplorazione",
    per_conto_di=None,
):
    """List all properties in a comune (optionally filtered by foglio).

    Uses the EIMM (Elenco immobili) service on SISTER.
    """
    import os

    if per_conto_di is None:
        per_conto_di = os.getenv("ADE_USERNAME", "")

    time0 = time.time()
    page_logger = PageLogger("elenco_immobili")
    foglio_info = f" F.{foglio}" if foglio else ""
    log.info(
        "[bold]Elenco immobili[/bold] %s/%s%s tipo=%s",
        provincia,
        comune,
        foglio_info,
        tipo_catasto,
    )

    # STEP 1: Navigate and select province
    await _navigate_to_scelta_servizio(page, page_logger)

    provincia_value = await find_best_option_match(page, "select[name='listacom']", provincia)
    if not provincia_value:
        raise Exception(f"Provincia '{provincia}' non trovata")

    await page.locator("select[name='listacom']").select_option(provincia_value)
    await page.locator("input[type='submit'][value='Applica']").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "provincia_applicata")

    # STEP 2: Click "Elenco immobili" in the left menu
    log.info("Navigando a Elenco immobili...")
    await page.get_by_role("link", name="Elenco immobili").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "elenco_immobili_form")

    # STEP 3: Select tipo catasto
    try:
        await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)
    except Exception as e:
        log.warning("Errore selezione tipo catasto: %s", e)

    # STEP 4: Select comune (EIMM uses comuneCat, not denomComune)
    comune_selector = "select[name='comuneCat']"
    if await page.locator(comune_selector).count() == 0:
        comune_selector = "select[name='denomComune']"
    comune_value = await find_best_option_match(page, comune_selector, comune)
    if not comune_value:
        raise Exception(f"Comune '{comune}' non trovato per provincia '{provincia}'")
    await page.locator(comune_selector).select_option(comune_value)

    # STEP 4.1: Select sezione (auto-detect if mandatory)
    await _select_sezione(page, comune, sezione)

    # STEP 4.2: Optionally fill foglio
    if foglio:
        log.info("Foglio: [cyan]%s[/cyan]", foglio)
        foglio_field = page.locator("input[name='foglio']")
        if await foglio_field.count() > 0:
            await foglio_field.fill(str(foglio))

    # STEP 5: Submit
    await _apply_form_fields(page)
    await page_logger.log(page, "form_compilato")
    log.info("Esecuzione elenco immobili...")
    await _search_button(page).click()
    await page.wait_for_load_state("networkidle", timeout=60000)
    await page_logger.log(page, "risultati_elenco")
    await _raise_on_form_error(page)

    # SISTER wants a second limitation besides the sheet: list each category group (A..F) in turn
    if "Inserire un'altra limitazione" in await page.inner_text("body"):
        log.info("SISTER richiede un'altra limitazione: elenco per gruppo di categorie")
        grouped: list[dict] = []
        for group in ("A%", "B%", "C%", "D%", "E%", "F%"):
            await page.locator("select[name='categoria']").select_option(group)
            await _search_button(page).click()
            await page.wait_for_load_state("networkidle", timeout=60000)
            if "NESSUNA CORRISPONDENZA TROVATA" not in await page.inner_text("body"):
                grouped.extend({**row, "categoria_gruppo": group[0]} for row in _extract_result_tables(await page.content()))
            if await page.locator("select[name='categoria']").count() == 0:
                await page.go_back()
                await page.wait_for_load_state("networkidle", timeout=60000)
        return {
            "provincia": provincia,
            "comune": comune,
            "foglio": foglio,
            "immobili": grouped,
            "total_results": len(grouped),
            "page_visits": page_logger.page_visits,
        }

    # STEP 6: Check for errors
    page_text = await page.inner_text("body")
    if "NESSUNA CORRISPONDENZA TROVATA" in page_text:
        elapsed = time.time() - time0
        log.warning("Nessuna corrispondenza trovata (%.1fs)", elapsed)
        return {
            "provincia": provincia,
            "comune": comune,
            "foglio": foglio,
            "immobili": [],
            "total_results": 0,
            "error": "NESSUNA CORRISPONDENZA TROVATA",
            "page_visits": page_logger.page_visits,
        }

    # STEP 7: Extract results
    immobili = _extract_result_tables(await page.content())
    log.info("[green]%d immobili[/green] estratti", len(immobili))

    elapsed = time.time() - time0
    log.info("[green]Elenco immobili completato[/green] in %.1fs — %d risultati", elapsed, len(immobili))

    return {
        "provincia": provincia,
        "comune": comune,
        "foglio": foglio,
        "immobili": immobili,
        "total_results": len(immobili),
        "page_visits": page_logger.page_visits,
    }


def _extract_result_tables(page_html: str) -> list:
    """Extract data rows from result tables in SISTER HTML."""
    soup = BeautifulSoup(page_html, "html.parser")
    for table in soup.find_all("table"):
        headers_text = " ".join(th.get_text(strip=True) for th in table.find_all("th"))
        if any(
            kw in headers_text
            for kw in (
                "Foglio",
                "Particella",
                "Comune",
                "Provincia",
                "Denominazione",
                "Nota",
                "Partita",
                "Indirizzo",
                "Fiduciale",
                "Mappa",
            )
        ):
            return parse_table(str(table))
    return []


async def _verify_office(page, label: str) -> None:
    """Check the office header SISTER shows after "Applica" against the office that was selected.

    Raises on a clearly different office; only warns when the header cannot be read (page layout changed).
    """
    text = re.sub(r"\s+", " ", await page.inner_text("body"))
    expected = label.replace("Territorio", "").strip().upper()
    national = expected.startswith("NAZIONALE")
    if national and "ambito nazionale" in text.lower():
        return
    match = re.search(r"Ufficio provinciale di:?\s*(.{0,60})", text)
    if not match:
        log.warning("Ufficio '%s' applicato ma intestazione non leggibile: impossibile verificare", label)
        return
    shown = match.group(1).upper()
    if expected not in shown:
        raise Exception(f"Ufficio applicato non coerente: atteso '{label}', il portale mostra '{match.group(1).strip()}'")


async def _set_office(page, page_logger, office=None, navigate=True) -> str:
    """Select the SISTER office through "Cambia Ufficio": NAZIONALE when ``office`` is empty, else a provincia.

    The office sets the level of every following query (national for persone, provincial for immobili), so a
    multi-step automation calls this again whenever its next step needs another level. Returns the label of
    the applied office, verified against the header the portal shows.
    """
    if navigate:
        await _navigate_to_scelta_servizio(page, page_logger)
    wanted = office or "NAZIONALE"
    value = await find_best_option_match(page, "select[name='listacom']", wanted)
    if not value:
        raise Exception(f"Provincia '{office}' non trovata" if office else "Opzione NAZIONALE non trovata")
    await page.locator("select[name='listacom']").select_option(value)
    label = await page.locator("select[name='listacom']").evaluate("el => el.options[el.selectedIndex].text")
    await page.locator("input[type='submit'][value='Applica']").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "provincia_applicata")
    await _verify_office(page, label)
    log.info("Ufficio: [cyan]%s[/cyan] (%s)", label.strip(), "nazionale" if not office else "provinciale")
    return label.strip()


async def _navigate_select_province_and_click(page, page_logger, provincia, menu_link_name):
    """Shared helper: navigate to SceltaServizio, select province, click a menu link."""
    await _set_office(page, page_logger, provincia)

    await page.get_by_role("link", name=menu_link_name, exact=True).click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, menu_link_name.lower().replace(" ", "_"))


_FORM_CONTEXT: contextvars.ContextVar = contextvars.ContextVar("sister_form_context", default=None)

_TRUE_VALUES = {"1", "true", "yes", "si", "sì", "on", "t", "y"}


@contextlib.contextmanager
def form_context(query: str, form_fields: dict | None):
    """Make the form inputs of a request visible to the form filler while its ``run_*`` function runs."""
    token = _FORM_CONTEXT.set((query, dict(form_fields or {})))
    try:
        yield
    finally:
        _FORM_CONTEXT.reset(token)


def current_form_fields() -> dict:
    ctx = _FORM_CONTEXT.get()
    return ctx[1] if ctx else {}


async def _set_control(page, fld, value) -> None:
    """Set one input of the SISTER form from its parameter value (text, date, select, radio or checkbox).

    An input that is not on the form is ignored; one that is there but hidden or disabled (e.g. a
    restriction that only exists for a province office) raises, instead of waiting for it to become usable.
    """
    base = None
    for name in fld.portal_names:
        candidate = f"[name='{name}']"
        if await page.locator(f"input{candidate}, select{candidate}, textarea{candidate}").count() > 0:
            base = candidate
            break
    if base is None:
        log.debug("Campo %s (%s) assente nel modulo: ignorato", fld.portal_names, fld.param)
        return
    text = str(value).strip()
    unavailable = Exception(f"Campo '{fld.param}' non disponibile in questo modulo (nascosto o disabilitato)")
    if fld.kind == "radio":
        raw = fld.choices.get(text.lower(), text)
        radio = page.locator(f"input[type='radio']{base}[value='{raw}']")
        if await radio.count() == 0:
            raise Exception(f"Valore '{value}' non valido per '{fld.param}' (valori: {', '.join(fld.choices) or raw})")
        if not await radio.first.is_enabled():
            raise unavailable
        await radio.first.check()
    elif fld.kind == "checkbox":
        box = page.locator(f"input[type='checkbox']{base}, input[type='radio']{base}").first
        if not (await box.is_visible() and await box.is_enabled()):
            raise unavailable
        if text.lower() in _TRUE_VALUES:
            await box.check()
        elif await box.get_attribute("type") == "checkbox":
            await box.uncheck()
    elif fld.kind == "select":
        select = page.locator(f"select{base}").first
        if not (await select.is_visible() and await select.is_enabled()):
            raise unavailable
        raw = fld.choices.get(text.lower())
        if raw is None:
            raw = await find_best_option_match(page, f"select{base}", text)
        if raw is None or await select.locator(f"option[value='{raw}']").count() == 0:
            raise Exception(f"Valore '{value}' non disponibile per '{fld.param}' in questo modulo")
        await select.select_option(raw)
        # dependent dropdowns (e.g. comune di nascita after the provincia) are loaded by the page
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=5000)
    else:
        usable = []
        for field_ in await page.locator(f"input{base}, textarea{base}").all():
            if await field_.is_visible() and await field_.is_enabled():
                usable.append(field_)
        if not usable:
            raise unavailable
        for field_ in usable:
            await field_.fill(text)


async def _apply_form_fields(page, page_no: int = 1) -> None:
    """Fill every input of the current query form that the request gave a value for (see query_forms).

    An input that only exists in one mode of a radio (``FormField.requires``) first switches the form to that
    mode, unless the request chose the other one, which is a conflict.
    """
    ctx = _FORM_CONTEXT.get()
    if not ctx:
        return
    query, values = ctx
    form = get_query_form(query)
    if form is None:
        return
    by_param = {fld.param: fld for fld in form.fields}
    chosen: dict[str, str] = {}  # mode parameter -> mode value switched to by this call
    # the inputs that exist in every mode first (a mode switch can hide some of them), then the mode-specific ones
    mode_params = {f.requires[0] for f in form.fields if f.requires}
    ordered = sorted(form.fields, key=lambda f: f.requires is not None or f.param in mode_params)
    for fld in ordered:
        value = values.get(fld.param)
        if fld.handled or fld.page != page_no or value is None or value == "":
            continue
        if fld.requires:
            mode_param, mode_value = fld.requires
            given = str(values.get(mode_param) or "").strip().lower()
            if given and given != mode_value:
                raise Exception(f"'{fld.param}' richiede {mode_param}={mode_value} (indicato: {given})")
            if not given and chosen.get(mode_param) != mode_value:
                await _set_control(page, by_param[mode_param], mode_value)
                chosen[mode_param] = mode_value
        await _set_control(page, fld, value)


async def _fill_richiedente_motivo(page, motivo="Esplorazione", per_conto_di=None, sezione_urbana=None):
    """Fill the richiedente, motivo, and sezione urbana fields if present."""
    import os

    if per_conto_di is None:
        per_conto_di = os.getenv("ADE_USERNAME", "")

    if per_conto_di:
        field = page.locator("input[name='richiedente']")
        if await field.count() > 0:
            await field.fill(per_conto_di)
    if motivo:
        field = page.locator("input[name='motivoText']")
        if await field.count() == 0:
            field = page.locator("input[name='motivo']")
        if await field.count() > 0:
            await field.fill(motivo)
    if sezione_urbana:
        field = page.locator("input[name='sezUrb']")
        if await field.count() > 0:
            await field.fill(str(sezione_urbana).upper())

    await _apply_form_fields(page)


_FORM_ERROR_RE = re.compile(r"((?:Il campo|La sezione)[^.]{0,80}obbligatori[oa][^.]{0,60}\.)")


async def _raise_on_form_error(page) -> None:
    """Fail loudly when SISTER bounced the form back with a validation message (e.g. "Il campo Foglio è
    obbligatorio."), instead of reporting an empty successful result."""
    match = _FORM_ERROR_RE.search(re.sub(r"\s+", " ", await page.inner_text("body")))
    if match:
        raise Exception(f"SISTER ha rifiutato il modulo: {match.group(1).strip()}")


def _search_button(page):
    """The submit button of a SISTER search form (the label differs between the web apps)."""
    return page.locator(
        "input[name='ricerca'][value='Ricerca'], input[type='submit'][value='Ricerca'], "
        "input[name='scelta'][value='Ricerca'], "
        # Elaborato planimetrico (VisureNew app) submits with "Inoltra", not "Ricerca"
        "input[type='submit'][name='submit'][value='Inoltra']"
    ).first


async def _expand_indirizzi(page, page_logger) -> list[dict]:
    """Second page of the address search: pick each address found and list its properties."""
    rows: list[dict] = []
    total = await page.locator("select[name='indirizzoSel'] option").count()
    wanted = str(current_form_fields().get("indirizzo_selezionato") or "").strip().upper()
    for i in range(total):
        select = page.locator("select[name='indirizzoSel']")
        if await select.count() == 0:
            break
        option = select.locator("option").nth(i)
        value = await option.get_attribute("value")
        label = (await option.inner_text()).strip()
        if wanted and wanted not in label.upper():
            continue
        await select.select_option(value)
        await _apply_form_fields(page, page_no=2)
        await _search_button(page).click()
        await page.wait_for_load_state("networkidle", timeout=60000)
        await _raise_on_form_error(page)
        await page_logger.log(page, f"indirizzo_{i + 1}")
        for row in _extract_result_tables(await page.content()):
            rows.append({**row, "indirizzo_trovato": label})
        if i + 1 < total:
            await page.go_back()
            await page.wait_for_load_state("networkidle", timeout=60000)
    return rows


async def _submit_and_extract(page, page_logger, step_name):
    """Submit a SISTER search form and extract results table."""
    await page_logger.log(page, f"form_compilato_{step_name}")
    await _search_button(page).click()
    await page.wait_for_load_state("networkidle", timeout=60000)
    await _raise_on_form_error(page)
    await _wait_for_captcha(page)
    await page_logger.log(page, f"risultati_{step_name}")

    page_text = await page.inner_text("body")
    if "NESSUNA CORRISPONDENZA TROVATA" in page_text or "nessun elaborato trovato" in page_text.lower():
        return None

    return _extract_result_tables(await page.content())


# ---------------------------------------------------------------------------
# Additional SISTER search types
# ---------------------------------------------------------------------------


async def run_ricerca_indirizzo(
    page,
    provincia,
    comune,
    indirizzo,
    tipo_catasto="T",
    sezione=None,
):
    """Search by address (IND) on SISTER."""
    time0 = time.time()
    page_logger = PageLogger("indirizzo")
    log.info("[bold]Ricerca indirizzo[/bold] %s/%s '%s'", provincia, comune, indirizzo)

    await _navigate_select_province_and_click(page, page_logger, provincia, "Indirizzo")

    try:
        await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)
    except Exception:
        pass

    comune_value = await find_best_option_match(page, await _comune_selector(page), comune)
    if not comune_value:
        raise Exception(f"Comune '{comune}' non trovato")
    await page.locator(await _comune_selector(page)).select_option(comune_value)

    await _select_sezione(page, comune, sezione)

    ind_field = page.locator("input[name='indirizzo']")
    if await ind_field.count() == 0:
        ind_field = page.locator("input[name='via']")
    if await ind_field.count() > 0:
        await ind_field.fill(indirizzo)

    await _fill_richiedente_motivo(page)

    results = await _submit_and_extract(page, page_logger, "indirizzo")
    if not results and await page.locator("select[name='indirizzoSel']").count() > 0:
        results = await _expand_indirizzi(page, page_logger)
    elapsed = time.time() - time0
    immobili = results or []
    log.info("[green]Ricerca indirizzo completata[/green] in %.1fs — %d risultati", elapsed, len(immobili))

    return {
        "provincia": provincia,
        "comune": comune,
        "indirizzo": indirizzo,
        "immobili": immobili,
        "total_results": len(immobili),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_ricerca_partita(
    page,
    provincia,
    comune,
    partita,
    tipo_catasto="T",
    sezione=None,
):
    """Search by partita catastale (PART) on SISTER."""
    time0 = time.time()
    page_logger = PageLogger("partita")
    log.info("[bold]Ricerca partita[/bold] %s/%s P.%s", provincia, comune, partita)

    await _navigate_select_province_and_click(page, page_logger, provincia, "Partita")

    try:
        await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)
    except Exception:
        pass

    comune_value = await find_best_option_match(page, await _comune_selector(page), comune)
    if not comune_value:
        raise Exception(f"Comune '{comune}' non trovato")
    await page.locator(await _comune_selector(page)).select_option(comune_value)

    partita_field = page.locator("input[name='partita'], input[name='numPart'], input[name='numPartita']").first
    await partita_field.fill(str(partita))

    await _fill_richiedente_motivo(page)

    results = await _submit_and_extract(page, page_logger, "partita")
    elapsed = time.time() - time0
    immobili = results or []
    log.info("[green]Ricerca partita completata[/green] in %.1fs — %d risultati", elapsed, len(immobili))

    return {
        "provincia": provincia,
        "comune": comune,
        "partita": partita,
        "immobili": immobili,
        "total_results": len(immobili),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_ricerca_nota(
    page,
    provincia,
    numero_nota,
    anno_nota=None,
    tipo_catasto="T",
):
    """Search by annotation/note reference (NOTA) on SISTER."""
    time0 = time.time()
    page_logger = PageLogger("nota")
    log.info("[bold]Ricerca nota[/bold] %s nota=%s anno=%s", provincia, numero_nota, anno_nota)

    await _navigate_select_province_and_click(page, page_logger, provincia, "Nota")

    try:
        await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)
    except Exception:
        pass

    nota_field = page.locator("input[name='numNota']")
    if await nota_field.count() == 0:
        nota_field = page.locator("input[name='nota']")
    if await nota_field.count() == 0:
        nota_field = page.locator("input[name='numero']")
    await nota_field.fill(str(numero_nota))

    if anno_nota:
        anno_field = page.locator("input[name='annoNota']")
        if await anno_field.count() == 0:
            anno_field = page.locator("input[name='anno']")
        if await anno_field.count() > 0:
            await anno_field.fill(str(anno_nota))

    await _fill_richiedente_motivo(page)

    results = await _submit_and_extract(page, page_logger, "nota")
    elapsed = time.time() - time0
    rows = results or []
    log.info("[green]Ricerca nota completata[/green] in %.1fs — %d risultati", elapsed, len(rows))

    return {
        "provincia": provincia,
        "numero_nota": numero_nota,
        "anno_nota": anno_nota,
        "risultati": rows,
        "total_results": len(rows),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_ricerca_mappa(
    page,
    provincia,
    comune,
    foglio,
    tipo_catasto="T",
    sezione=None,
    particella=None,
):
    """View/extract cadastral map data (EM) on SISTER.

    Form: EstrattoMappaForm with comuneCat, foglio, particelle fields.
    """
    time0 = time.time()
    page_logger = PageLogger("mappa")
    log.info("[bold]Ricerca mappa[/bold] %s/%s F.%s", provincia, comune, foglio)

    await _navigate_select_province_and_click(page, page_logger, provincia, "Mappa")

    # Mappa uses comuneCat (not denomComune)
    comune_selector = "select[name='comuneCat']"
    if await page.locator(comune_selector).count() == 0:
        comune_selector = "select[name='denomComune']"
    comune_value = await find_best_option_match(page, comune_selector, comune)
    if comune_value:
        await page.locator(comune_selector).select_option(comune_value)

    # Fill foglio
    foglio_field = page.locator("input[name='foglio']")
    if await foglio_field.count() > 0:
        await foglio_field.fill(str(foglio))

    # Fill particelle (optional)
    if particella:
        part_field = page.locator("input[name='particelle']")
        if await part_field.count() > 0:
            await part_field.fill(str(particella))

    # Sezione
    await _select_sezione(page, comune, sezione)

    await _fill_richiedente_motivo(page)

    results = await _submit_and_extract(page, page_logger, "mappa")
    elapsed = time.time() - time0
    rows = results or []
    log.info("[green]Ricerca mappa completata[/green] in %.1fs — %d risultati", elapsed, len(rows))

    return {
        "provincia": provincia,
        "comune": comune,
        "foglio": foglio,
        "risultati": rows,
        "total_results": len(rows),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_export_mappa(
    page,
    provincia,
    comune,
    foglio,
    tipo_catasto="T",
    sezione=None,
):
    """Export cadastral map data (EXPM) on SISTER."""
    time0 = time.time()
    page_logger = PageLogger("export_mappa")
    log.info("[bold]Export mappa[/bold] %s/%s F.%s", provincia, comune, foglio)

    await _navigate_select_province_and_click(page, page_logger, provincia, "Export Mappa")

    # Export Mappa uses comuneCat
    comune_selector = "select[name='comuneCat']"
    if await page.locator(comune_selector).count() == 0:
        comune_selector = "select[name='denomComune']"
    comune_value = await find_best_option_match(page, comune_selector, comune)
    if comune_value:
        await page.locator(comune_selector).select_option(comune_value)

    foglio_field = page.locator("input[name='foglio']")
    if await foglio_field.count() > 0:
        await foglio_field.fill(str(foglio))

    await _fill_richiedente_motivo(page)

    results = await _submit_and_extract(page, page_logger, "export_mappa")
    elapsed = time.time() - time0
    rows = results or []
    log.info("[green]Export mappa completata[/green] in %.1fs — %d risultati", elapsed, len(rows))

    return {
        "provincia": provincia,
        "comune": comune,
        "foglio": foglio,
        "risultati": rows,
        "total_results": len(rows),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_originali_impianto(
    page,
    provincia,
    comune,
    tipo_catasto="T",
    foglio=None,
):
    """Retrieve original registration records (OOII) on SISTER."""
    time0 = time.time()
    page_logger = PageLogger("originali_impianto")
    log.info("[bold]Originali di impianto[/bold] %s/%s", provincia, comune)

    await _navigate_select_province_and_click(page, page_logger, provincia, "Originali di impianto")

    try:
        await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)
    except Exception:
        pass

    comune_value = await find_best_option_match(page, await _comune_selector(page), comune)
    if comune_value:
        await page.locator(await _comune_selector(page)).select_option(comune_value)

    if foglio:
        foglio_field = page.locator("input[name='foglio']")
        if await foglio_field.count() > 0:
            await foglio_field.fill(str(foglio))

    await _fill_richiedente_motivo(page)

    results = await _submit_and_extract(page, page_logger, "originali_impianto")
    elapsed = time.time() - time0
    rows = results or []
    log.info("[green]Originali impianto completati[/green] in %.1fs — %d risultati", elapsed, len(rows))

    return {
        "provincia": provincia,
        "comune": comune,
        "risultati": rows,
        "total_results": len(rows),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_punti_fiduciali(
    page,
    provincia,
    comune,
    tipo_catasto="T",
    foglio=None,
):
    """Retrieve survey reference points (FID) on SISTER."""
    time0 = time.time()
    page_logger = PageLogger("punti_fiduciali")
    log.info("[bold]Punti fiduciali[/bold] %s/%s", provincia, comune)

    await _navigate_select_province_and_click(page, page_logger, provincia, "Punti fiduciali")

    try:
        await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)
    except Exception:
        pass

    comune_value = await find_best_option_match(page, await _comune_selector(page), comune)
    if comune_value:
        await page.locator(await _comune_selector(page)).select_option(comune_value)

    if foglio:
        foglio_field = page.locator("input[name='foglio']")
        if await foglio_field.count() > 0:
            await foglio_field.fill(str(foglio))

    await _fill_richiedente_motivo(page)

    results = await _submit_and_extract(page, page_logger, "punti_fiduciali")
    elapsed = time.time() - time0
    rows = results or []
    log.info("[green]Punti fiduciali completati[/green] in %.1fs — %d risultati", elapsed, len(rows))

    return {
        "provincia": provincia,
        "comune": comune,
        "risultati": rows,
        "total_results": len(rows),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def _navigate_to_ispezioni(page, page_logger, provincia, cartacee=False):
    """Navigate from Visure to the Ispezioni module.

    Ispezioni is a separate SISTER module at /Ispezioni/ — clicking
    "Passa a Ispezioni" lands on a Conferma Lettura page that must be
    acknowledged before the search forms appear.
    """
    # First navigate to Visure and select province
    await _navigate_select_province_and_click(
        page, page_logger, provincia, "Passa a Ispezioni Cartacee" if cartacee else "Passa a Ispezioni"
    )

    # Click "Conferma Lettura" to enter the Ispezioni module
    conferma = page.get_by_role("link", name="Conferma Lettura")
    if await conferma.count() > 0:
        await conferma.click()
        await page.wait_for_load_state("networkidle", timeout=30000)
        await page_logger.log(page, "ispezioni_conferma")
        log.info("Conferma Lettura accettata")

    # After Conferma, we should be in /Ispezioni/SceltaServizio
    # Select the province again in the Ispezioni module
    prov_select = page.locator("select[name='listacom']")
    if await prov_select.count() > 0:
        provincia_value = await find_best_option_match(page, "select[name='listacom']", provincia)
        if provincia_value:
            await prov_select.select_option(provincia_value)
            applica = page.locator("input[type='submit'][value='Applica']")
            if await applica.count() > 0:
                await applica.click()
                await page.wait_for_load_state("networkidle", timeout=30000)
                await page_logger.log(page, "ispezioni_provincia")

    # Click "Immobile" in the Ispezioni menu (default search type)
    imm_link = page.get_by_role("link", name="Immobile")
    if await imm_link.count() > 0:
        await imm_link.click()
        await page.wait_for_load_state("networkidle", timeout=30000)
        await page_logger.log(page, "ispezioni_immobile")


async def run_ispezioni(
    page,
    provincia,
    comune,
    tipo_catasto="T",
    foglio=None,
    particella=None,
    tipo_ricerca="PF",
):
    """Search property inspection records (ISP) on SISTER.

    Navigates through the /Ispezioni/ module (separate from /Visure/).
    """
    time0 = time.time()
    page_logger = PageLogger("ispezioni")
    log.info("[bold]Ispezioni[/bold] %s/%s", provincia, comune)

    await _navigate_to_ispezioni(page, page_logger, provincia, cartacee=False)

    try:
        await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)
    except Exception:
        pass

    # Select comune (Ispezioni may use comuneCat or denomComune)
    for sel in ["select[name='comuneCat']", "select[name='denomComune']"]:
        if await page.locator(sel).count() > 0:
            cv = await find_best_option_match(page, sel, comune)
            if cv:
                await page.locator(sel).select_option(cv)
            break

    if foglio:
        f = page.locator("input[name='foglio']")
        if await f.count() > 0:
            await f.fill(str(foglio))
    if particella:
        for pn in ["input[name='particella1']", "input[name='particella']"]:
            p = page.locator(pn)
            if await p.count() > 0:
                await p.fill(str(particella))
                break

    await _fill_richiedente_motivo(page)

    results = await _submit_and_extract(page, page_logger, "ispezioni")
    elapsed = time.time() - time0
    rows = results or []
    log.info("[green]Ispezioni completate[/green] in %.1fs — %d risultati", elapsed, len(rows))

    return {
        "provincia": provincia,
        "comune": comune,
        "risultati": rows,
        "total_results": len(rows),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_ispezioni_cartacee(
    page,
    provincia,
    comune,
    tipo_catasto="T",
    foglio=None,
    particella=None,
):
    """Search paper inspection records (ISPCART) on SISTER."""
    time0 = time.time()
    page_logger = PageLogger("ispezioni_cartacee")
    log.info("[bold]Ispezioni cartacee[/bold] %s/%s", provincia, comune)

    await _navigate_to_ispezioni(page, page_logger, provincia, cartacee=True)

    try:
        await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)
    except Exception:
        pass

    for sel in ["select[name='comuneCat']", "select[name='denomComune']"]:
        if await page.locator(sel).count() > 0:
            cv = await find_best_option_match(page, sel, comune)
            if cv:
                await page.locator(sel).select_option(cv)
            break

    if foglio:
        f = page.locator("input[name='foglio']")
        if await f.count() > 0:
            await f.fill(str(foglio))
    if particella:
        for pn in ["input[name='particella1']", "input[name='particella']"]:
            p = page.locator(pn)
            if await p.count() > 0:
                await p.fill(str(particella))
                break

    await _fill_richiedente_motivo(page)

    results = await _submit_and_extract(page, page_logger, "ispezioni_cartacee")
    elapsed = time.time() - time0
    rows = results or []
    log.info("[green]Ispezioni cartacee completate[/green] in %.1fs — %d risultati", elapsed, len(rows))

    return {
        "provincia": provincia,
        "comune": comune,
        "risultati": rows,
        "total_results": len(rows),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_elaborato_planimetrico(
    page,
    provincia,
    comune,
    tipo_catasto="F",
    foglio=None,
    particella=None,
):
    """Retrieve Elaborato Planimetrico (ELPL) on SISTER.

    Uses a different web app at /VisureNew/SwitchWebApp.do.
    """
    time0 = time.time()
    page_logger = PageLogger("elaborato_planimetrico")
    log.info("[bold]Elaborato Planimetrico[/bold] %s/%s", provincia, comune)

    # Navigate to Visure and select province first
    await _navigate_select_province_and_click(page, page_logger, provincia, "Elaborato Planimetrico")

    # This may land on a different app (/VisureNew/)
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "elaborato_planimetrico_form")

    # Try to fill the form fields
    for sel in ["select[name='comuneCat']", "select[name='denomComune']"]:
        if await page.locator(sel).count() > 0:
            cv = await find_best_option_match(page, sel, comune)
            if cv:
                await page.locator(sel).select_option(cv)
            break

    if foglio:
        f = page.locator("input[name='foglio']")
        if await f.count() > 0:
            await f.fill(str(foglio))

    if particella:
        # the portal requires the parcel (particella1 = number, particella2 = optional suffix)
        number, _, suffix = str(particella).partition("/")
        p1 = page.locator("input[name='particella1']")
        if await p1.count() > 0:
            await p1.fill(number)
            if suffix:
                await page.locator("input[name='particella2']").fill(suffix)

    await _fill_richiedente_motivo(page)

    results = await _submit_and_extract(page, page_logger, "elaborato_planimetrico")
    elapsed = time.time() - time0
    rows = results or []
    log.info("[green]Elaborato planimetrico completato[/green] in %.1fs — %d risultati", elapsed, len(rows))

    return {
        "provincia": provincia,
        "comune": comune,
        "risultati": rows,
        "total_results": len(rows),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_riepilogo_visure(page):
    """Retrieve Riepilogo Visure (user's query history on SISTER)."""
    time0 = time.time()
    page_logger = PageLogger("riepilogo_visure")
    log.info("[bold]Riepilogo Visure[/bold]")

    # Navigate to SceltaServizio first to ensure session
    await _navigate_to_scelta_servizio(page, page_logger)

    # Navigate to Riepilogo
    await page.goto(
        "https://sister3.agenziaentrate.gov.it/Visure/RiepilogoVisure/UtentiRiepilogoVisure.do", timeout=30000
    )
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "riepilogo_visure")

    # The page only shows a search form: fill it (data visura defaults to today) and press "Visualizza"
    await _apply_form_fields(page)
    await page.locator("input[type='submit'][name='submit'][value='Visualizza']").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "riepilogo_risultati")

    # Extract the summary table
    results = _extract_result_tables(await page.content())
    elapsed = time.time() - time0
    log.info("[green]Riepilogo visure completato[/green] in %.1fs — %d risultati", elapsed, len(results))

    return {
        "risultati": results,
        "total_results": len(results),
        "page_visits": page_logger.page_visits,
    }


async def run_consultazione_richieste(page):
    """Retrieve pending/completed requests from SISTER's Richieste service."""
    time0 = time.time()
    page_logger = PageLogger("richieste")
    log.info("[bold]Consultazione Richieste[/bold]")

    # Navigate to SceltaServizio first
    await _navigate_to_scelta_servizio(page, page_logger)

    # Get the Richieste link URL from the page (it contains the convention number)
    richieste_link = page.locator("a:has-text('Richieste')")
    if await richieste_link.count() > 0:
        href = await richieste_link.get_attribute("href")
        if href:
            if not href.startswith("http"):
                href = "https://sister3.agenziaentrate.gov.it" + href
            await page.goto(href, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page_logger.log(page, "richieste")

    # The Richieste table has its own schema and isn't a cadastral result table.
    # Return only rows with a Salva link, which means the document is ready to
    # download. Keep the request id, but don't persist the session-bound URL.
    requests = _parse_richieste_table(await page.content())
    results = [
        {key: value for key, value in item.items() if key != "salva_href"}
        for item in requests
    ]
    elapsed = time.time() - time0
    log.info("[green]Consultazione richieste completata[/green] in %.1fs — %d pronti da scaricare", elapsed, len(results))

    return {
        "richieste": results,
        "risultati": results,
        "total_results": len(results),
        "page_visits": page_logger.page_visits,
    }


# ---------------------------------------------------------------------------
# Ispezioni Ipotecarie (paid service)
# ---------------------------------------------------------------------------

ISPEZIONI_IPOTECARIE_URL = "https://sister3.agenziaentrate.gov.it/Ispezioni/SceltaServizio.do?tipo=/T/TM/VIVI_"


async def _navigate_to_ispezioni_ipotecarie(page, page_logger, provincia, menu_link_name="Immobile"):
    """Navigate to the Ispezioni Ipotecarie module and select a search type.

    This handles:
    1. Navigate via "Passa a Ispezioni" from Visure
    2. Accept "Conferma Lettura"
    3. Select province
    4. Click the appropriate menu link (Persona fisica, Persona giuridica, Immobile, Nota)
    """
    await _navigate_to_ispezioni(page, page_logger, provincia, cartacee=False)

    # After province is set, click the specific search type link
    if menu_link_name != "Immobile":
        link = page.get_by_role("link", name=menu_link_name, exact=True)
        if await link.count() > 0:
            await link.click()
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page_logger.log(page, f"ispezioni_{menu_link_name.lower().replace(' ', '_')}")


async def _extract_cost_from_page(page):
    """Extract the cost/price from a SISTER confirmation page.

    Returns (cost_text, cost_value) or (None, None) if no cost found.
    """
    page_text = await page.inner_text("body")

    # Look for cost patterns: "Costo: € X,XX" or "Importo: X,XX" or "EUR X.XX"
    import re

    patterns = [
        r"[Cc]osto[:\s]+[€EUR\s]*([\d.,]+)",
        r"[Ii]mporto[:\s]+[€EUR\s]*([\d.,]+)",
        r"[Pp]rezzo[:\s]+[€EUR\s]*([\d.,]+)",
        r"€\s*([\d.,]+)",
        r"EUR\s*([\d.,]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, page_text)
        if match:
            cost_text = match.group(0).strip()
            cost_val = match.group(1).replace(".", "").replace(",", ".")
            try:
                return cost_text, float(cost_val)
            except ValueError:
                return cost_text, 0.0

    return None, None


async def _handle_cost_confirmation(page, page_logger, auto_confirm=False):
    """Handle the cost confirmation page in Ispezioni Ipotecarie.

    Returns:
        dict with keys: confirmed (bool), cost_text, cost_value, error
    """
    await page_logger.log(page, "cost_confirmation")

    cost_text, cost_value = await _extract_cost_from_page(page)

    if cost_text:
        log.info("Costo rilevato: [yellow]%s[/yellow] (€%.2f)", cost_text, cost_value or 0)

    # Check if there's a confirmation button
    conferma_btn = page.locator("input[value='Conferma']")
    if await conferma_btn.count() == 0:
        conferma_btn = page.locator("button:has-text('Conferma')")
    if await conferma_btn.count() == 0:
        conferma_btn = page.locator("input[type='submit'][value*='onferma']")

    if await conferma_btn.count() == 0:
        # No confirmation page — might be a free query or already confirmed
        return {"confirmed": True, "cost_text": cost_text, "cost_value": cost_value}

    if not auto_confirm:
        log.warning("Conferma costo richiesta: %s — usa --yes per auto-approvare", cost_text or "importo sconosciuto")
        return {
            "confirmed": False,
            "cost_text": cost_text,
            "cost_value": cost_value,
            "error": f"Cost confirmation required: {cost_text or 'unknown amount'}. Use --yes to auto-approve.",
        }

    # Auto-confirm
    log.info("Auto-conferma costo: %s", cost_text)
    await conferma_btn.first.click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "cost_confirmed")

    return {"confirmed": True, "cost_text": cost_text, "cost_value": cost_value}


async def run_ispezione_ipotecaria(
    page,
    provincia,
    comune=None,
    tipo_ricerca="immobile",
    codice_fiscale=None,
    identificativo=None,
    foglio=None,
    particella=None,
    numero_nota=None,
    anno_nota=None,
    tipo_catasto="T",
    auto_confirm=False,
):
    """Execute an Ispezione Ipotecaria (paid inspection) on SISTER.

    tipo_ricerca: 'immobile', 'persona_fisica', 'persona_giuridica', 'nota'
    auto_confirm: if True, automatically confirm cost without prompting
    """
    import os

    time0 = time.time()
    page_logger = PageLogger("ispezione_ipotecaria")

    menu_map = {
        "immobile": "Immobile",
        "persona_fisica": "Persona fisica",
        "persona_giuridica": "Persona giuridica",
        "nota": "Nota",
    }
    menu_link = menu_map.get(tipo_ricerca, "Immobile")
    log.info("[bold]Ispezione Ipotecaria[/bold] tipo=%s %s/%s", tipo_ricerca, provincia, comune or "")

    # Navigate to Ispezioni and select the search type
    await _navigate_to_ispezioni_ipotecarie(page, page_logger, provincia, menu_link)

    # Fill search form based on tipo_ricerca
    if tipo_ricerca == "immobile":
        try:
            await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)
        except Exception:
            pass

        for sel in ["select[name='comuneCat']", "select[name='denomComune']"]:
            if await page.locator(sel).count() > 0:
                cv = await find_best_option_match(page, sel, comune or "")
                if cv:
                    await page.locator(sel).select_option(cv)
                break

        if foglio:
            f = page.locator("input[name='foglio']")
            if await f.count() > 0:
                await f.fill(str(foglio))
        if particella:
            for pn in ["input[name='particella1']", "input[name='particella']"]:
                p = page.locator(pn)
                if await p.count() > 0:
                    await p.fill(str(particella))
                    break

    elif tipo_ricerca == "persona_fisica":
        if codice_fiscale:
            # Try CF radio + field
            cf_radio = page.locator("input[name='selDatiAna'][value='CF']")
            if await cf_radio.count() > 0:
                await cf_radio.click()
            cf_field = page.locator("input[name='cod_fisc_pf']")
            if await cf_field.count() == 0:
                cf_field = page.locator("input[name='codFiscale']")
            if await cf_field.count() > 0:
                await cf_field.fill(codice_fiscale.upper())

    elif tipo_ricerca == "persona_giuridica":
        if identificativo:
            cf_radio = page.locator("input[name='selCfDn'][value='CF_PNF']")
            if await cf_radio.count() > 0:
                await cf_radio.click()
            cf_field = page.locator("input[name='cod_fisc']")
            if await cf_field.count() == 0:
                cf_field = page.locator("input[name='codFiscale']")
            if await cf_field.count() > 0:
                await cf_field.fill(identificativo.upper())

    elif tipo_ricerca == "nota":
        if numero_nota:
            nota_field = page.locator("input[name='numNota']")
            if await nota_field.count() == 0:
                nota_field = page.locator("input[name='nota']")
            if await nota_field.count() > 0:
                await nota_field.fill(str(numero_nota))
        if anno_nota:
            anno_field = page.locator("input[name='annoNota']")
            if await anno_field.count() == 0:
                anno_field = page.locator("input[name='anno']")
            if await anno_field.count() > 0:
                await anno_field.fill(str(anno_nota))

    # Fill richiedente
    per_conto_di = os.getenv("ADE_USERNAME", "")
    await _fill_richiedente_motivo(page, motivo="Ispezione ipotecaria", per_conto_di=per_conto_di)
    await page_logger.log(page, "form_compilato")

    # Submit the search
    log.info("Submitting ispezione ipotecaria...")
    ricerca_btn = page.locator("input[name='ricerca'][value='Ricerca']")
    if await ricerca_btn.count() == 0:
        ricerca_btn = page.locator("input[type='submit'][value='Ricerca']")
    await ricerca_btn.click()
    await page.wait_for_load_state("networkidle", timeout=60000)
    await page_logger.log(page, "risultati_pre_conferma")

    # Check for "no results"
    page_text = await page.inner_text("body")
    if "NESSUNA CORRISPONDENZA TROVATA" in page_text:
        elapsed = time.time() - time0
        log.warning("Nessuna corrispondenza (%.1fs)", elapsed)
        return {
            "tipo_ricerca": tipo_ricerca,
            "provincia": provincia,
            "risultati": [],
            "total_results": 0,
            "cost": None,
            "error": "NESSUNA CORRISPONDENZA TROVATA",
        }

    # Handle cost confirmation
    cost_result = await _handle_cost_confirmation(page, page_logger, auto_confirm=auto_confirm)

    if not cost_result["confirmed"]:
        elapsed = time.time() - time0
        return {
            "tipo_ricerca": tipo_ricerca,
            "provincia": provincia,
            "risultati": [],
            "total_results": 0,
            "cost": {"text": cost_result.get("cost_text"), "value": cost_result.get("cost_value")},
            "confirmed": False,
            "error": cost_result.get("error", "Cost confirmation required"),
        }

    # Extract results after confirmation
    results = _extract_result_tables(await page.content())
    elapsed = time.time() - time0
    log.info(
        "[green]Ispezione ipotecaria completata[/green] in %.1fs — %d risultati, costo: %s",
        elapsed,
        len(results),
        cost_result.get("cost_text", "N/A"),
    )

    return {
        "tipo_ricerca": tipo_ricerca,
        "provincia": provincia,
        "risultati": results,
        "total_results": len(results),
        "cost": {"text": cost_result.get("cost_text"), "value": cost_result.get("cost_value")},
        "confirmed": True,
    }


async def run_ispezioni_ipotecarie_stato(page):
    """Check automation status (Stato dell'automazione) in Ispezioni Ipotecarie."""
    page_logger = PageLogger("ispezioni_stato")
    log.info("[bold]Stato automazione ispezioni[/bold]")

    # This is typically an info page — navigate and extract content
    await _navigate_to_scelta_servizio(page, page_logger)
    # Navigate to Ispezioni via "Passa a Ispezioni"
    await page.get_by_role("link", name="Passa a Ispezioni", exact=True).click()
    await page.wait_for_load_state("networkidle", timeout=30000)

    # Click Conferma Lettura
    conferma = page.get_by_role("link", name="Conferma Lettura")
    if await conferma.count() > 0:
        await conferma.click()
        await page.wait_for_load_state("networkidle", timeout=30000)

    # Click "Stato dell'automazione"
    stato_link = page.get_by_role("link", name="Stato dell'automazione")
    if await stato_link.count() > 0:
        await stato_link.click()
        await page.wait_for_load_state("networkidle", timeout=30000)
        await page_logger.log(page, "stato_automazione")

    results = _extract_result_tables(await page.content())
    return {"risultati": results, "total_results": len(results)}


async def run_ispezioni_ipotecarie_elenchi(page):
    """Retrieve billed/accounted lists (Elenchi contabilizzati) from Ispezioni Ipotecarie."""
    page_logger = PageLogger("ispezioni_elenchi")
    log.info("[bold]Elenchi contabilizzati[/bold]")

    await _navigate_to_scelta_servizio(page, page_logger)
    await page.get_by_role("link", name="Passa a Ispezioni", exact=True).click()
    await page.wait_for_load_state("networkidle", timeout=30000)

    conferma = page.get_by_role("link", name="Conferma Lettura")
    if await conferma.count() > 0:
        await conferma.click()
        await page.wait_for_load_state("networkidle", timeout=30000)

    elenchi_link = page.get_by_role("link", name="Elenchi contabilizzati")
    if await elenchi_link.count() > 0:
        await elenchi_link.click()
        await page.wait_for_load_state("networkidle", timeout=30000)
        await page_logger.log(page, "elenchi_contabilizzati")

    results = _extract_result_tables(await page.content())
    return {"risultati": results, "total_results": len(results)}


async def extract_all_sezioni(page: Page, tipo_catasto: str = "T", max_province: int = 200) -> list:
    """
    Estrae tutte le sezioni per tutte le province e comuni d'Italia.

    Args:
        page: Pagina Playwright autenticata
        tipo_catasto: 'T' per Terreni, 'F' per Fabbricati
        max_province: Numero massimo di province da processare

    Returns:
        Lista di dizionari con dati delle sezioni
    """
    sezioni_data = []
    page_logger = PageLogger("sezioni")

    try:
        log.info("[bold]Estrazione sezioni[/bold] tipo=%s max_province=%d", tipo_catasto, max_province)

        await _navigate_to_scelta_servizio(page, page_logger)

        # Estrai tutte le province
        provincia_options = await page.locator("select[name='listacom'] option").all()
        province_list = []

        for option in provincia_options:
            value = await option.get_attribute("value")
            text = await option.inner_text()
            if value and text and value.strip() and text.strip():
                if "NAZIONALE" not in text.upper():
                    province_list.append({"value": value.strip(), "text": text.strip()})

        province_list = province_list[:max_province]
        log.info("Processando %d province", len(province_list))

        for i, provincia in enumerate(province_list):
            log.info("[bold]Provincia %d/%d[/bold]: %s", i + 1, len(province_list), provincia["text"])

            try:
                await page.locator("select[name='listacom']").select_option(provincia["value"])
                await page.locator("input[type='submit'][value='Applica']").click()
                await page.wait_for_load_state("networkidle", timeout=30000)

                await page.get_by_role("link", name="Immobile").click()
                await page.wait_for_load_state("networkidle", timeout=30000)

                try:
                    await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)
                except Exception as e:
                    log.warning("Errore selezione tipo catasto per %s: %s", provincia["text"], e)

                # Estrai tutti i comuni per questa provincia
                comune_options = await page.locator(f"{await _comune_selector(page)} option").all()
                comuni_list = []

                for option in comune_options:
                    value = await option.get_attribute("value")
                    text = await option.inner_text()
                    if value and text and value.strip() and text.strip():
                        comuni_list.append({"value": value.strip(), "text": text.strip()})

                log.info("%d comuni per %s", len(comuni_list), provincia["text"])

                for j, comune in enumerate(comuni_list):
                    log.debug("Comune %d/%d: %s", j + 1, len(comuni_list), comune["text"])

                    try:
                        await page.locator(await _comune_selector(page)).select_option(comune["value"])

                        await page.locator("input[name='selSezione'][value='scegli la sezione']").click()
                        await page.wait_for_load_state("networkidle", timeout=30000)

                        comune_sezioni_data = []

                        try:
                            sezione_options = await page.locator("select[name='sezione'] option").all()
                            available_sections = []

                            for option in sezione_options:
                                value = await option.get_attribute("value")
                                text = await option.inner_text()
                                if value and text and value.strip() and text.strip():
                                    available_sections.append({"value": value.strip(), "text": text.strip()})

                            log.debug("%d sezioni per %s", len(available_sections), comune["text"])

                            for sezione in available_sections:
                                comune_sezioni_data.append(
                                    {
                                        "provincia_nome": provincia["text"],
                                        "provincia_value": provincia["value"],
                                        "comune_nome": comune["text"],
                                        "comune_value": comune["value"],
                                        "sezione_nome": sezione["text"],
                                        "sezione_value": sezione["value"],
                                        "tipo_catasto": tipo_catasto,
                                    }
                                )

                            if len(available_sections) == 0:
                                comune_sezioni_data.append(
                                    {
                                        "provincia_nome": provincia["text"],
                                        "provincia_value": provincia["value"],
                                        "comune_nome": comune["text"],
                                        "comune_value": comune["value"],
                                        "sezione_nome": None,
                                        "sezione_value": None,
                                        "tipo_catasto": tipo_catasto,
                                    }
                                )

                        except Exception as e:
                            log.warning("Errore estrazione sezioni per %s: %s", comune["text"], e)
                            comune_sezioni_data.append(
                                {
                                    "provincia_nome": provincia["text"],
                                    "provincia_value": provincia["value"],
                                    "comune_nome": comune["text"],
                                    "comune_value": comune["value"],
                                    "sezione_nome": None,
                                    "sezione_value": None,
                                    "tipo_catasto": tipo_catasto,
                                }
                            )

                        if comune_sezioni_data:
                            sezioni_data.extend(comune_sezioni_data)

                    except Exception as e:
                        log.warning("Errore comune %s: %s", comune["text"], e)
                        continue

                log.info(
                    "Provincia %s completata — %d sezioni totali finora",
                    provincia["text"],
                    len(sezioni_data),
                )

                # Torna alla pagina principale per la prossima provincia
                await page.goto(SISTER_SCELTA_SERVIZIO_URL, timeout=60000)
                await page.wait_for_load_state("networkidle", timeout=30000)

            except Exception as e:
                log.error("Errore provincia %s: %s", provincia["text"], e)
                continue

        log.info("[green]Estrazione completata[/green]: %d sezioni totali", len(sezioni_data))
        return sezioni_data

    except Exception as e:
        log.error("Errore durante estrazione sezioni: %s", e)
        return sezioni_data


async def run_visura_immobile(
    page,
    provincia="Trieste",
    comune="Trieste",
    sezione=None,
    foglio="9",
    particella="166",
    subalterno=None,
    sezione_urbana=None,
):
    """
    Esegue una visura catastale per un immobile specifico (solo per fabbricati con subalterno).

    Args:
        page: Pagina Playwright autenticata
        provincia: Nome della provincia
        comune: Nome del comune
        sezione: Sezione territoriale (opzionale)
        foglio: Numero foglio
        particella: Numero particella
        subalterno: Numero subalterno (obbligatorio per questa funzione)

    Returns:
        Dict con intestati dell'immobile specificato
    """
    time0 = time.time()
    page_logger = PageLogger("visura_immobile")
    sezione_info = f", sezione={sezione}" if sezione else ""
    log.info(
        "[bold]Visura immobile[/bold] %s/%s F.%s P.%s Sub.%s%s",
        provincia,
        comune,
        foglio,
        particella,
        subalterno,
        sezione_info,
    )

    if not subalterno:
        raise ValueError("Il subalterno è obbligatorio per le visure per immobile specifico")

    # STEP 1: Selezione Ufficio Provinciale
    log.info("Navigando a SceltaServizio...")
    await _navigate_to_scelta_servizio(page, page_logger)

    # Trova e seleziona la provincia corretta
    provincia_value = await find_best_option_match(page, "select[name='listacom']", provincia)
    if not provincia_value:
        raise Exception(f"Provincia '{provincia}' non trovata")

    log.info("Provincia: [cyan]%s[/cyan]", provincia_value)
    await page.locator("select[name='listacom']").select_option(provincia_value)
    await page.locator("input[type='submit'][value='Applica']").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "provincia_applicata")

    # STEP 2: Ricerca per immobili
    log.info("Ricerca per immobile (Fabbricati)...")
    await page.get_by_role("link", name="Immobile").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "immobile")

    await page.locator("select[name='tipoCatasto']").select_option("F")

    # Trova e seleziona il comune
    comune_value = await find_best_option_match(page, await _comune_selector(page), comune)
    if not comune_value:
        raise Exception(f"Comune '{comune}' non trovato")

    log.info("Comune: [cyan]%s[/cyan]", comune_value)
    await page.locator(await _comune_selector(page)).select_option(comune_value)

    await _select_sezione(page, comune, sezione)

    # Fill "Sezione urbana" — only when explicitly provided (separate from dropdown sezione)
    if sezione_urbana:
        sez_urb_field = page.locator("input[name='sezUrb']")
        if await sez_urb_field.count() > 0:
            await sez_urb_field.fill(str(sezione_urbana).upper())
            log.info("Sezione urbana: [cyan]%s[/cyan]", sezione_urbana)

    # Inserisci dati immobile
    log.info(
        "Foglio: [cyan]%s[/cyan]  Particella: [cyan]%s[/cyan]  Sub: [cyan]%s[/cyan]", foglio, particella, subalterno
    )
    await page.locator("input[name='foglio']").fill(str(foglio))
    await page.locator("input[name='particella1']").fill(str(particella))
    await page.locator("input[name='subalterno1']").fill(str(subalterno))

    await _fill_richiedente_motivo(page, sezione_urbana=sezione_urbana)
    await page_logger.log(page, "form_compilato")

    # Clicca Ricerca
    log.info("Esecuzione ricerca...")
    await page.locator("input[name='scelta'][value='Ricerca']").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await _wait_for_captcha(page)
    await page_logger.log(page, "ricerca")

    # STEP 3: Gestisci conferma assenza subalterno (se necessario)
    try:
        conferma_button = page.locator("input[name='confAssSub'][value='Conferma']")
        if await conferma_button.count() > 0:
            log.warning("Confermi Assenza Subalterno — confermando automaticamente")
            await conferma_button.click()
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page_logger.log(page, "conferma_subalterno")
    except Exception as e:
        log.debug("Conferma subalterno non necessaria: %s", e)

    await page_logger.log(page, "risultati")

    # STEP 4: Estrazione dati immobile
    log.info("Estraendo dati immobile...")
    immobile_data = {}
    try:
        immobili_table = page.locator("table.listaIsp4").first
        if await immobili_table.count() > 0:
            immobili_html = await immobili_table.inner_html()
            immobili = parse_table(immobili_html)
            immobile_data = immobili[0] if immobili else {}
            log.debug("Dati immobile: %s", immobile_data)
    except Exception as e:
        log.warning("Errore estrazione dati immobile: %s", e)

    # STEP 5: Estrazione intestati
    log.info("Estraendo intestati...")
    intestati = []

    # Re-fill richiedente/motivo/sezUrb on results page (SISTER clears them after submit)
    await _fill_richiedente_motivo(page, sezione_urbana=sezione_urbana)
    try:
        intestati_button_selectors = [
            "input[name='intestati'][value='Intestati']",
            "input[value='Intestati']",
            "input[name='intestati']",
            "button:has-text('Intestati')",
            "input[type='submit'][value*='ntestat']",
            "input[type='button'][value*='ntestat']",
            "*[value='Intestati']",
            "a:has-text('Intestati')",
        ]

        intestati_button = None
        for selector in intestati_button_selectors:
            try:
                locator = page.locator(selector)
                if await locator.count() > 0:
                    intestati_button = locator.first
                    log.debug("Bottone Intestati trovato: %s", selector)
                    break
            except Exception as e:
                log.debug("Selettore Intestati '%s' fallito: %s", selector, e)
                continue

        if intestati_button:
            await intestati_button.click()
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page_logger.log(page, "intestati")

            selectors = [
                "table.listaIsp4",
                "table[class*='lista']",
                "table:has(th:text('Cognome'))",
                "table:has(th:text('Nome'))",
                "table:has(th:text('Nominativo o denominazione'))",
                "table:has(th:text('Codice fiscale'))",
                "table:has(th:text('Titolarità'))",
                "table",
            ]

            for selector in selectors:
                try:
                    intestati_table = page.locator(selector)
                    count = await intestati_table.count()

                    if count > 0:
                        for i in range(count):
                            try:
                                table_elem = intestati_table.nth(i)
                                intestati_html = await table_elem.inner_html(timeout=10000)

                                if (
                                    "Cognome" in intestati_html
                                    or "Nome" in intestati_html
                                    or "Soggetto" in intestati_html
                                    or "Nominativo o denominazione" in intestati_html
                                    or "Codice fiscale" in intestati_html
                                    or "Titolarità" in intestati_html
                                ):
                                    intestati = parse_table(intestati_html)
                                    log.info("[green]%d intestati[/green] estratti", len(intestati))
                                    break
                                else:
                                    temp_intestati = parse_table(intestati_html)
                                    if temp_intestati and len(temp_intestati) > 0:
                                        if "Foglio" not in intestati_html and "Particella" not in intestati_html:
                                            intestati = temp_intestati
                                            log.info("[green]%d intestati[/green] estratti (fallback)", len(intestati))
                                            break
                            except Exception as e:
                                log.debug("Errore tabella intestati %d: %s", i, e)
                                continue

                        if intestati:
                            break

                except Exception as e:
                    log.debug("Errore selettore intestati '%s': %s", selector, e)
                    continue
        else:
            log.warning("Bottone Intestati non trovato")

            # Debug: stampa tutti gli input e button disponibili
            try:
                all_inputs = await page.locator("input").all()
                log.debug("Trovati %d elementi input", len(all_inputs))
                for idx, inp in enumerate(all_inputs):
                    try:
                        tag_name = await inp.evaluate("el => el.tagName")
                        input_type = await inp.get_attribute("type") or "text"
                        name = await inp.get_attribute("name") or ""
                        value = await inp.get_attribute("value") or ""
                        log.debug("  %d: %s type='%s' name='%s' value='%s'", idx, tag_name, input_type, name, value)
                    except Exception:
                        pass

                all_buttons = await page.locator("button").all()
                log.debug("Trovati %d elementi button", len(all_buttons))
                for idx, btn in enumerate(all_buttons):
                    try:
                        text = await btn.inner_text()
                        name = await btn.get_attribute("name") or ""
                        value = await btn.get_attribute("value") or ""
                        log.debug("  %d: text='%s' name='%s' value='%s'", idx, text, name, value)
                    except Exception:
                        pass

            except Exception as e:
                log.debug("Errore debug elementi: %s", e)
    except Exception as e:
        log.error("Errore estrazione intestati: %s", e)

    elapsed = time.time() - time0
    log.info("[green]Visura immobile completata[/green] in %.1fs — %d intestati", elapsed, len(intestati))

    result = {
        "immobile": immobile_data,
        "intestati": intestati,
        "total_intestati": len(intestati),
        "page_visits": page_logger.page_visits,
    }

    return result


# ---------------------------------------------------------------------------
# Historical property visure and subject (persona fisica) documents / property lists
# ---------------------------------------------------------------------------


async def run_visura_storica(
    page,
    provincia,
    comune,
    foglio,
    particella,
    tipo_catasto="F",
    subalterno=None,
    sezione=None,
):
    """Historical visura per immobile (Storica Analitica); skips the per-owner Visura per Soggetto step."""
    return await run_visura(
        page,
        provincia=provincia,
        comune=comune,
        sezione=sezione,
        foglio=foglio,
        particella=particella,
        tipo_catasto=tipo_catasto,
        subalterno=subalterno,
        tipo_visura="storica_analitica",
        visura_soggetto=False,
    )


_SOGGETTO_STORICA_VALUES = {"analitica": "0", "sintetica": "5"}
_PROVINCE_RADIOS = "input[type='radio'][name='omonimonazionale'], input[type='radio'][property='omonimonazionale']"


async def _open_soggetto_province_page(page, identifier, tipo_catasto, provincia, azienda=False) -> bool:
    """Search a persona fisica (codice fiscale) or giuridica (partita IVA) and stop on the page offering
    Immobili / Visura per Soggetto.

    Returns False when SISTER finds no match.
    """
    if azienda:
        found = await run_visura_persona_giuridica(page, identifier, tipo_catasto=tipo_catasto, provincia=provincia)
    else:
        found = await _search_soggetto(page, identifier, tipo_catasto=tipo_catasto, provincia=provincia)
    if found.get("error"):
        return False
    # SceltaOmonimi rejects the submit ("Selezionare un Omonimo") unless one homonym is selected
    omonimo = page.locator("input[type='radio'][name='omonimoSelezionato']")
    if await omonimo.count() > 0:
        await omonimo.first.check()
    await page.locator("input[name='visura'][value='Ricerca']").click()
    await page.wait_for_load_state("networkidle", timeout=60000)
    return True


async def run_soggetto_documento(page, codice_fiscale, tipo_catasto="E", vista="analitica", provincia=None):
    """Request the Visura per Soggetto document (XML, differita) of a persona fisica, once per province.

    vista: 'analitica' or 'sintetica'. The request is submitted after the user solves the CAPTCHA; the
    document then appears under Richieste (download with POST /visura/download-documents).
    """
    if provincia and provincia.upper() == "NAZIONALE":
        provincia = None
    storica = _SOGGETTO_STORICA_VALUES.get(vista)
    if storica is None:
        raise ValueError(f"vista sconosciuta: {vista!r} (usa {', '.join(_SOGGETTO_STORICA_VALUES)})")
    time0 = time.time()
    page_logger = PageLogger("soggetto_documento")
    log.info("[bold]Visura per Soggetto[/bold] CF=%s vista=%s", codice_fiscale, vista)

    submitted: list[str] = []
    index = 0
    n_province = None
    while True:
        if not await _open_soggetto_province_page(page, codice_fiscale, tipo_catasto, provincia):
            return {
                "soggetto": codice_fiscale,
                "richieste": submitted,
                "error": "NESSUNA CORRISPONDENZA TROVATA",
                "page_visits": page_logger.page_visits,
            }
        province = page.locator(_PROVINCE_RADIOS)
        count = await province.count()
        if n_province is None:
            n_province = max(count, 1)
        label = "-"
        if count > 0:
            await province.nth(min(index, count - 1)).check()
            label = (await province.nth(min(index, count - 1)).get_attribute("value") or "").split("#")[0]
        await page.locator("input[name='visura'][value='Visura per Soggetto']").click()
        await page.wait_for_load_state("networkidle", timeout=60000)
        await page_logger.log(page, f"form_visura_soggetto_{index + 1}")

        await page.evaluate(
            """(storica) => {
            const si = document.querySelector('input[name="intestati"][value="1"]');
            if (si && !si.checked) si.click();
            const tipo = document.querySelector('input[name="storica"][value="' + storica + '"]');
            if (tipo) tipo.click();
            if (typeof checkPdfXml === 'function') checkPdfXml(true);
            if (typeof tipoVisuradisplayPdf === 'function' && tipo) tipoVisuradisplayPdf(tipo.value);
            const xml = document.querySelector('input[name="tipoDocFornitura"][value="XML"]');
            if (xml) { xml.parentElement.style.display = ''; xml.checked = true; }
            const differita = document.querySelector('input[name="differita"]');
            if (differita && !differita.checked) differita.checked = true;
        }""",
            storica,
        )
        log.info("Form soggetto: Con intestati, %s, XML, Differita (provincia %s)", vista, label)

        if not await _wait_for_captcha(page):
            inoltra = page.locator("input[name='inoltra'][value='Inoltra'], input[type='submit'][value='Inoltra']")
            if await inoltra.count() > 0:
                await inoltra.click()
                await page.wait_for_load_state("networkidle", timeout=30000)
        await page_logger.log(page, f"visura_soggetto_inoltrata_{index + 1}")
        submitted.append(label)
        index += 1
        if index >= n_province:
            break

    log.info("[green]Visura per Soggetto inoltrata[/green] in %.1fs (%d province)", time.time() - time0, len(submitted))
    return {
        "soggetto": codice_fiscale,
        "vista": vista,
        "richieste": submitted,
        "page_visits": page_logger.page_visits,
    }


async def _extract_owners(page) -> list[dict]:
    """Owners of the selected immobile on the Intestati page (identity from the radios, shares from the table)."""
    radios = page.locator("input[type='radio'][name='intestatoSelezionato']")
    owners = [parse_intestato_value(await radios.nth(i).get_attribute("value")) for i in range(await radios.count())]
    table = await _extract_intestati_playwright(page)
    for i, row in enumerate(table):
        extra = {
            "titolarita": row.get("Titolarità", ""),
            "quota": row.get("Quota", ""),
            "altri_dati": row.get("Altri dati", ""),
        }
        if i < len(owners):
            owners[i].update(extra)
        else:
            # table without radios (single owner): identify it from the codice fiscale column
            ident = (row.get("Codice fiscale") or "").strip()
            owners.append(
                {"codice_fiscale": ident, "tipo": "azienda" if len(ident) == 11 else "persona", **extra}
            )
    return owners


async def run_soggetto_immobili(
    page, codice_fiscale, tipo_catasto="E", provincia=None, con_intestati=False, azienda=False
):
    """Owner → properties: list every immobile of a persona fisica/giuridica in each province.

    con_intestati: also open the Intestati page of each immobile (property → owners), so that the result holds
    both directions of the ownership graph. No document is requested and no CAPTCHA is involved.
    azienda: ``codice_fiscale`` is a partita IVA (persona giuridica).
    """
    if isinstance(con_intestati, str):
        con_intestati = con_intestati.lower() in ("1", "true", "yes", "si")
    if isinstance(azienda, str):
        azienda = azienda.lower() in ("1", "true", "yes", "si")
    if provincia and provincia.upper() == "NAZIONALE":
        provincia = None
    time0 = time.time()
    page_logger = PageLogger("soggetto_immobili")
    immobili: list[dict] = []
    index = 0
    n_province = None

    async def open_list(idx):
        if not await _open_soggetto_province_page(page, codice_fiscale, tipo_catasto, provincia, azienda=azienda):
            return None
        radios_prov = page.locator(_PROVINCE_RADIOS)
        count = await radios_prov.count()
        label, nome = "-", ""
        if count > 0:
            pick = radios_prov.nth(min(idx, count - 1))
            await pick.check()
            label, _, nome = (await pick.get_attribute("value") or "").partition("#")
        await page.locator("input[name='immobili'][value='Immobili']").click()
        await page.wait_for_load_state("networkidle", timeout=60000)
        return (label, nome.strip()), max(count, 1)

    while True:
        opened = await open_list(index)
        if opened is None:
            return {
                "soggetto": codice_fiscale,
                "immobili": [],
                "total_results": 0,
                "error": "NESSUNA CORRISPONDENZA TROVATA",
                "page_visits": page_logger.page_visits,
            }
        (label, provincia_nome), count = opened
        if n_province is None:
            n_province = count
        await page_logger.log(page, f"immobili_{index + 1}")

        rows = _extract_result_tables(await page.content()) or []
        list_radios = "input[type='radio'][name='visImmSel'], input[type='radio'][property='visImmSel']"
        radios = page.locator(list_radios)
        values = [await radios.nth(i).get_attribute("value") for i in range(await radios.count())]
        batch = []
        for i, value in enumerate(values):
            row = dict(rows[i]) if i < len(rows) and isinstance(rows[i], dict) else {}
            row["provincia"] = label
            row["provincia_nome"] = provincia_nome
            row["visImmSel"] = value
            batch.append(row)

        if con_intestati:
            for i, row in enumerate(batch):
                row["intestati"] = []
                try:
                    radios = page.locator(list_radios)
                    if await radios.count() <= i:
                        opened = await open_list(index)
                        radios = page.locator(list_radios)
                    await radios.nth(i).check()
                    button = page.locator("input[name='intestati'][value='Intestati']")
                    if await button.count() == 0:
                        continue  # e.g. bene comune non censibile
                    await button.click()
                    await page.wait_for_load_state("networkidle", timeout=60000)
                    row["intestati"] = await _extract_owners(page)
                    back = page.locator("input[type='submit'][name='indietro']")
                    if await back.count() > 0:
                        await back.first.click()
                        await page.wait_for_load_state("networkidle", timeout=60000)
                    if await page.locator(list_radios).count() == 0:
                        await open_list(index)
                except Exception as e:
                    row["intestati_error"] = str(e)[:200]
                    log.warning("Intestati non letti per immobile %d: %s", i + 1, e)
                    await open_list(index)

        immobili.extend(batch)
        index += 1
        if index >= n_province:
            break

    log.info("[green]Immobili soggetto[/green] %s: %d in %.1fs", codice_fiscale, len(immobili), time.time() - time0)
    return {
        "soggetto": codice_fiscale,
        "immobili": immobili,
        "total_results": len(immobili),
        "page_visits": page_logger.page_visits,
    }
