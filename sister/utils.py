import asyncio
import logging
import os
import re
import time
from datetime import datetime

from aecs4u_auth.browser import PageLogger as _BasePageLogger
from bs4 import BeautifulSoup
from playwright.async_api import Page

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
                self.page_visits.append({
                    "step": step_name,
                    "url": page.url,
                    "timestamp": datetime.now().isoformat(),
                    "screenshot_url": screenshot_url,
                    "form_elements": form_elements,
                    "errors": errors,
                })
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
                attempt, max_retries,
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
                                log.info("SceltaServizio raggiunta dopo ADE redirect (%d province)", provincia_count - 1)
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
    soup = BeautifulSoup(html, "html.parser")
    headers = [th.get_text(strip=True) for th in soup.find_all("th")]
    rows = []
    for tr in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if cells:
            # Se ci sono meno celle che header, aggiungi celle vuote
            while len(cells) < len(headers):
                cells.append("")
            rows.append(dict(zip(headers, cells)))
    return rows


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


async def _wait_for_captcha(page, timeout: int = 120):
    """Detect and wait for the user to solve a CAPTCHA if present.

    Handles two types:
    1. SISTER-native CAPTCHA: img#imgCaptcha + input[name='inCaptchaChars']
       → waits for the user to fill the code and submit (page navigates away)
    2. Generic reCAPTCHA/hCaptcha iframes
       → waits for the element to disappear
    """
    # SISTER-native CAPTCHA
    captcha_input = page.locator("input[name='inCaptchaChars']")
    if await captcha_input.count() > 0:
        current_url = page.url
        log.warning("CAPTCHA SISTER rilevato — in attesa che l'utente inserisca il codice (timeout %ds)...", timeout)
        try:
            # Wait for the page to navigate away (user solved CAPTCHA and form submitted)
            await page.wait_for_url(lambda url: url != current_url, timeout=timeout * 1000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            log.info("CAPTCHA SISTER risolto, pagina navigata a: %s", page.url)
        except Exception:
            log.warning("Timeout attesa CAPTCHA SISTER — proseguendo comunque")
        return True

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
                log.warning("Timeout attesa CAPTCHA — proseguendo comunque")
            return True
    return False


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
):
    time0 = time.time()
    page_logger = PageLogger("visura")
    sezione_info = f", sezione={sezione}" if sezione else ""
    subalterno_info = f", sub={subalterno}" if subalterno else ""
    log.info(
        "[bold]Visura[/bold] %s/%s F.%s P.%s%s%s tipo=%s",
        provincia, comune, foglio, particella, sezione_info, subalterno_info, tipo_catasto,
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
        raise Exception(
            f"Provincia '{provincia}' non trovata. Disponibili: {', '.join(available_provinces[:10])}"
        )

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
    comune_options = await page.locator("select[name='denomComune'] option").all()
    available_comuni = []
    for option in comune_options:
        value = await option.get_attribute("value")
        text = await option.inner_text()
        if value and text:
            available_comuni.append(f"{text} ({value})")

    log.debug("Comuni disponibili: %d", len(available_comuni))

    comune_value = await find_best_option_match(page, "select[name='denomComune']", comune)

    if not comune_value:
        raise Exception(
            f"Comune '{comune}' non trovato per provincia '{provincia}'. Disponibili: {', '.join(available_comuni[:10])}"
        )

    log.info("Comune: [cyan]%s[/cyan]", comune_value)
    try:
        await page.locator("select[name='denomComune']").select_option(comune_value)
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
    log.info("Foglio: [cyan]%s[/cyan]  Particella: [cyan]%s[/cyan]%s", foglio, particella, f"  Sub: [cyan]{subalterno}[/cyan]" if subalterno else "")
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
    radio_buttons_check = page.locator("input[type='radio'][property='visImmSel'], input[type='radio'][name='visImmSel']")
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

    # STEP 5: Estrai intestati e visure per immobile
    log.info("Estraendo intestati e visure per immobile...")
    all_intestati = []
    results_list = []
    skipped_soppresso = 0

    # Re-fill richiedente/motivo/sezUrb on results page (SISTER clears them after submit)
    await _fill_richiedente_motivo(page, sezione_urbana=sezione_urbana)

    try:
        # Check if there are radio buttons (multiple immobili)
        radio_buttons = page.locator("input[type='radio'][property='visImmSel'], input[type='radio'][name='visImmSel']")
        radio_count = await radio_buttons.count()

        if radio_count > 0:
            # Build list of active radio indices, classifying each
            active_indices = []
            bene_comune_indices = set()
            for i in range(radio_count):
                val = await radio_buttons.nth(i).get_attribute("value") or ""
                if "Soppress" in val:
                    skipped_soppresso += 1
                else:
                    active_indices.append(i)
                    if "Bene comune" in val:
                        bene_comune_indices.add(i)
            skipped_bene_comune = len(bene_comune_indices)
            if skipped_soppresso:
                log.info("Saltati %d immobili soppressi su %d totali", skipped_soppresso, radio_count)
            if skipped_bene_comune:
                log.info("Trovati %d 'Bene comune non censibile' — intestati saltati per questi", skipped_bene_comune)
            log.info("Iterando per %d immobili attivi", len(active_indices))
        else:
            active_indices = []
            bene_comune_indices = set()

        total_active = len(active_indices)
        for item_num, radio_idx in enumerate(active_indices, 1):
            imm_data = immobili[radio_idx] if radio_idx < len(immobili) else {}
            is_bene_comune = radio_idx in bene_comune_indices
            step_result = {"result_index": radio_idx + 1, "immobile": imm_data, "intestati": [], "visura": None}

            # Select the radio button
            radio = radio_buttons.nth(radio_idx)
            await radio.click()
            log.info("[%d/%d] Immobile radio %d — Sub.%s %s%s",
                     item_num, total_active, radio_idx + 1,
                     imm_data.get("Sub", "?"),
                     imm_data.get("Indirizzo", "")[:30],
                     " [Bene comune — skip intestati]" if is_bene_comune else "")

            # --- Click "Intestati" (skip for Bene comune non censibile) ---
            intestati_btn = page.locator("input[name='intestati'][value='Intestati']")
            if not is_bene_comune and await intestati_btn.count() > 0:
                await intestati_btn.click()
                await page.wait_for_load_state("networkidle", timeout=30000)
                await page_logger.log(page, f"intestati_{radio_idx + 1}")

                # Extract intestati using Playwright locators
                step_intestati = await _extract_intestati_playwright(page)
                log.info("[green]%d intestati[/green] per immobile (radio %d)", len(step_intestati), radio_idx + 1)
                all_intestati.extend(step_intestati)
                step_result["intestati"] = step_intestati

                # --- Click "Visura per Soggetto" (deferred PDF request) ---
                visura_sogg_btn = page.locator("input[name='visura'][value='Visura per Soggetto']")
                if await visura_sogg_btn.count() > 0:
                    await visura_sogg_btn.click()
                    await page.wait_for_load_state("networkidle", timeout=30000)

                    # Handle intermediate pages (RicercaPF, SceltaOmonimi, etc.)
                    submitted = False
                    for _nav in range(5):
                        current_url = page.url

                        # TipoVisura.do — the visura options form
                        if "TipoVisura" in current_url:
                            await _set_visura_form_defaults(page)
                            await page_logger.log(page, f"visura_soggetto_{radio_idx + 1}")
                            visura_sogg_data = await _extract_visura_immobile_playwright(page)
                            step_result["visura_soggetto"] = visura_sogg_data

                            has_captcha = await _wait_for_captcha(page)
                            if not has_captcha:
                                inoltra_btn = page.locator("input[name='inoltra'][value='Inoltra'], input[type='submit'][value='Inoltra']")
                                if await inoltra_btn.count() > 0:
                                    await inoltra_btn.click()
                                    await page.wait_for_load_state("networkidle", timeout=30000)
                            submitted = True
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
                            submit_btn = page.locator("input[type='submit'][value='Conferma'], input[type='submit'][value='Prosegui'], input[type='submit']").first
                            if await submit_btn.count() > 0:
                                await submit_btn.click()
                                await page.wait_for_load_state("networkidle", timeout=30000)
                                continue

                        # InoltraRichiestaVis.do — already submitted
                        if "InoltraRichiesta" in current_url:
                            submitted = True
                            break

                        # Unknown page — log and break
                        log.warning("Pagina inattesa durante Visura per Soggetto: %s", current_url)
                        await page_logger.log(page, f"visura_soggetto_unexpected_{radio_idx + 1}")
                        break

                    if submitted:
                        await page_logger.log(page, f"visura_soggetto_inoltrata_{radio_idx + 1}")
                        log.info("Visura per Soggetto inoltrata per radio %d", radio_idx + 1)

                    # Re-submit search to get immobili list back
                    radio_buttons = await _resubmit_search_for_immobili_list(
                        page, page_logger, provincia, comune, sezione, foglio, particella,
                        tipo_catasto, subalterno, sezione_urbana,
                    )
                else:
                    # No "Visura per Soggetto" button — go back from intestati page
                    await _navigate_back_to_immobili_list(page)
                    radio_buttons = page.locator("input[type='radio'][property='visImmSel'], input[type='radio'][name='visImmSel']")

                # Re-select the same radio for the next action (Visura Per Immobile)
                radio = radio_buttons.nth(radio_idx)
                await radio.click()

            # --- Click "Visura Per Immobile" ---
            visura_btn = page.locator("input[name='visuraImm'][value='Visura Per Immobile']")
            if await visura_btn.count() > 0:
                await visura_btn.click()
                await page.wait_for_load_state("networkidle", timeout=30000)

                # Set default options: Storica Analitica, XML, differita
                await _set_visura_form_defaults(page)
                await page_logger.log(page, f"visura_immobile_{radio_idx + 1}")

                # Extract visura data from the form page before submitting
                visura_data = await _extract_visura_immobile_playwright(page)
                step_result["visura"] = visura_data

                # Wait for user to solve CAPTCHA (fills inCaptchaChars → form auto-submits)
                has_captcha = await _wait_for_captcha(page)

                if not has_captcha:
                    # No CAPTCHA — click Inoltra manually
                    inoltra_btn = page.locator("input[name='inoltra'][value='Inoltra'], input[type='submit'][value='Inoltra']")
                    if await inoltra_btn.count() > 0:
                        await inoltra_btn.click()
                        await page.wait_for_load_state("networkidle", timeout=30000)

                await page_logger.log(page, f"visura_inoltrata_{radio_idx + 1}")
                log.info("Visura Per Immobile inoltrata per radio %d", radio_idx + 1)

                # Re-submit search to get immobili list back (SISTER loses session state after Inoltra)
                radio_buttons = await _resubmit_search_for_immobili_list(
                    page, page_logger, provincia, comune, sezione, foglio, particella,
                    tipo_catasto, subalterno, sezione_urbana,
                )

            await _fill_richiedente_motivo(page, sezione_urbana=sezione_urbana)
            results_list.append(step_result)
            log.info("[%d/%d] Completato immobile radio %d", item_num, total_active, radio_idx + 1)

    except Exception as e:
        log.error("Errore estrazione intestati/visure (item %d/%d): %s", item_num if 'item_num' in dir() else 0, total_active if 'total_active' in dir() else 0, e)

    if not results_list and immobili:
        results_list = [{"result_index": 1, "immobile": immobili[0] if immobili else {}, "intestati": all_intestati}]

    # --- Download PDFs from Richieste page ---
    downloaded_pdfs = []
    if extract_intestati and results_list:
        try:
            downloaded_pdfs = await _download_richieste_documents(page, page_logger)
        except Exception as e:
            log.warning("Errore download PDF da Richieste: %s", e)

    elapsed = time.time() - time0
    log.info("[green]Visura completata[/green] in %.1fs — %d immobili, %d intestati, %d soppressi saltati, %d PDF scaricati",
             elapsed, len(immobili), len(all_intestati), skipped_soppresso, len(downloaded_pdfs))

    result = {
        "immobili": immobili,
        "results": results_list,
        "total_results": len(immobili),
        "intestati": all_intestati,
        "skipped_soppresso": skipped_soppresso,
        "downloaded_pdfs": downloaded_pdfs,
        "page_visits": page_logger.page_visits,
    }

    return result


async def _resubmit_search_for_immobili_list(
    page, page_logger, provincia, comune, sezione, foglio, particella,
    tipo_catasto, subalterno, sezione_urbana,
):
    """Re-submit the property search to restore the immobili list.

    After submitting "Visura Per Immobile" or "Visura per Soggetto", SISTER
    loses the session state for the immobili list. We need to re-navigate
    from SceltaServizio and re-submit the search to get the radio buttons back.

    Returns a fresh radio_buttons locator.
    """
    log.info("Ri-eseguendo ricerca per ripristinare lista immobili...")

    await _navigate_to_scelta_servizio(page, page_logger)

    provincia_value = await find_best_option_match(page, "select[name='listacom']", provincia)
    if provincia_value:
        await page.locator("select[name='listacom']").select_option(provincia_value)
    await page.locator("input[type='submit'][value='Applica']").click()
    await page.wait_for_load_state("networkidle", timeout=30000)

    await page.get_by_role("link", name="Immobile").click()
    await page.wait_for_load_state("networkidle", timeout=30000)

    await page.locator("select[name='tipoCatasto']").select_option(tipo_catasto)

    comune_value = await find_best_option_match(page, "select[name='denomComune']", comune)
    if comune_value:
        await page.locator("select[name='denomComune']").select_option(comune_value)

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


async def _set_visura_form_defaults(page):
    """Set default options on the SceltaVisuraImmSogg form.

    Defaults:
      - Con intestati: selected (required for XML format)
      - Storica: Analitica (tipoVisura=3)
      - Formato documento: XML (tipoDocFornitura=XML)
      - richiesta in differita: checked (differita=1)

    Uses JS evaluate for radio buttons that may be hidden by SISTER's
    dynamic form logic (XML option is only visible with certain combinations).
    """
    await page.evaluate("""() => {
        // Select "Con intestati" first (required for XML format to be visible)
        const conIntestati = document.querySelector('input[name="intestati"][value="1"]');
        if (conIntestati && !conIntestati.checked) {
            conIntestati.click();
        }

        // Storica → Analitica (value=3)
        const analitica = document.querySelector('input[name="tipoVisura"][value="3"]');
        if (analitica) {
            analitica.click();
        }

        // Trigger the JS that shows/hides format options
        if (typeof checkPdfXml === 'function') checkPdfXml(true);
        if (typeof tipoVisuradisplayPdf === 'function' && analitica) tipoVisuradisplayPdf(analitica.value);

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
    }""")
    log.info("Form defaults: Con intestati, Storica Analitica, XML, Differita")


async def _download_richieste_documents(page, page_logger) -> list[dict]:
    """Navigate to the Richieste page, extract metadata, download and rename files.

    1. Scrapes the Richieste table for metadata (Oggetto, timestamp, idRichiesta)
    2. Downloads each document from the "salva" column
    3. Extracts P7M → XML via openssl
    4. Parses XML for structured data
    5. Renames files using Oggetto metadata (e.g. "VISURA FG.101 ... SUB.62")
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

    docs_dir = os.path.join(OUTPUTS_DIR, "documents")
    os.makedirs(docs_dir, exist_ok=True)

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

        # Build a descriptive filename from Oggetto
        safe_oggetto = re.sub(r"[^\w\-.]", "_", oggetto)[:80].strip("_")
        desc_filename = f"{safe_oggetto}_{id_richiesta}" if safe_oggetto else f"DOC_{id_richiesta}"

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

            # Determine extension from original filename or formato
            file_ext = os.path.splitext(orig_filename)[1].lower() or f".{formato.lower()}" or ".dat"
            file_format = file_ext.lstrip(".").upper()

            # Save with descriptive name
            final_filename = f"{desc_filename}{file_ext}"
            save_path = os.path.join(docs_dir, final_filename)
            # Avoid overwrites
            if os.path.exists(save_path):
                final_filename = f"{desc_filename}_{id_richiesta}{file_ext}"
                save_path = os.path.join(docs_dir, final_filename)
            await download.save_as(save_path)
            file_size = os.path.getsize(save_path)

            log.info("[%d/%d] Scaricato: %s (%s, %d bytes) — %s",
                     len(downloaded) + 1, len(richieste_meta),
                     final_filename, file_format, file_size, oggetto[:50])

            doc_info = {
                "filename": final_filename,
                "original_filename": orig_filename,
                "path": save_path,
                "file_format": file_format,
                "file_size": file_size,
                "oggetto": oggetto,
                "richiesta_del": richiesta_del,
                "id_richiesta": id_richiesta,
                "parsed_data": None,
            }

            # Extract P7M and parse XML
            if file_format == "P7M":
                extracted_path = _extract_p7m(save_path)
                if extracted_path:
                    # Rename extracted file to descriptive name
                    xml_path = os.path.join(docs_dir, f"{desc_filename}.xml")
                    if not os.path.exists(xml_path):
                        os.rename(extracted_path, xml_path)
                        extracted_path = xml_path
                    doc_info["extracted_path"] = extracted_path

            if file_format in ("XML", "P7M"):
                parsed = _parse_visura_xml(save_path)
                if parsed:
                    doc_info["parsed_data"] = parsed
                    log.info("  XML: %s F.%s P.%s Sub.%s — %d intestati",
                             parsed.get("tipo", ""), parsed.get("foglio", ""),
                             parsed.get("particella", ""), parsed.get("subalterno", ""),
                             len(parsed.get("intestati", [])))

            downloaded.append(doc_info)

        except Exception as e:
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

        results.append({
            "richiesta_del": richiesta_del,
            "oggetto": oggetto,
            "formato": formato,
            "costo": costo,
            "salva_href": salva_href,
            "id_richiesta": id_richiesta,
        })

    return results


def _descriptive_filename(parsed: dict) -> str:
    """Build a descriptive filename from parsed visura XML data."""
    tipo = parsed.get("tipo", "visura").replace("visura_", "")
    prov = parsed.get("provincia", "")
    comune = parsed.get("comune", "").strip()
    fog = parsed.get("foglio", "")
    par = parsed.get("particella", "")
    sub = parsed.get("subalterno", "")
    sez = parsed.get("sezione_urbana", "")

    intestato = ""
    if parsed.get("intestati"):
        first = parsed["intestati"][0]
        intestato = first.get("Nominativo", first.get("CF", ""))
        intestato = intestato.split(";")[0].strip()[:40]

    name = f"{tipo}_{prov}_{comune}_F{fog}_P{par}"
    if sub:
        name += f"_Sub{sub}"
    if sez:
        name += f"_Sez{sez}"
    if intestato:
        name += f"_{intestato}"
    return re.sub(r"[^\w\-.]", "_", name)


def _extract_p7m(file_path: str) -> str | None:
    """Extract the signed content from a P7M file using openssl.

    Returns the path to the extracted file, or None on failure.
    After extraction, renames the output to a descriptive filename based on content.
    """
    import subprocess
    out_path = file_path.rsplit(".p7m", 1)[0]
    if not out_path or out_path == file_path:
        out_path = file_path + ".extracted"
    try:
        result = subprocess.run(
            ["openssl", "cms", "-verify", "-inform", "DER", "-in", file_path, "-noverify", "-out", out_path],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            log.info("P7M estratto: %s → %s (%d bytes)", file_path, out_path, os.path.getsize(out_path))
            return out_path
        log.warning("openssl cms fallito (rc=%d): %s", result.returncode, result.stderr.decode(errors="ignore")[:200])
    except Exception as e:
        log.warning("Errore estrazione P7M %s: %s", file_path, e)
    return None


def _parse_visura_xml(file_path: str) -> dict | None:
    """Parse a SISTER visura XML file and extract structured data.

    Handles both plain .xml and .p7m (signed XML) files.
    P7M files are first extracted via openssl cms.
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
            "xml_content": content[:50000],
        }

        # Determine document type
        if soup.find("VisuraFabbricatiStorica") or soup.find("VisuraFabbricati"):
            result["tipo"] = "visura_fabbricati"
            result["tipo_catasto"] = "F"
        elif soup.find("VisuraTerrenoStorica") or soup.find("VisuraTerreno"):
            result["tipo"] = "visura_terreni"
            result["tipo_catasto"] = "T"
        elif soup.find("VisuraSoggetto") or soup.find("VisuraSoggettoStorica"):
            result["tipo"] = "visura_soggetto"
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
            result["sezione_urbana"] = dati_rich.get("SezUrbana", "")

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
        for intestato_el in soup.find_all("Intestato"):
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


async def _save_documents_to_db(documents: list[dict]) -> None:
    """Persist downloaded documents to the visura_documents table, skipping duplicates."""
    import json
    from sqlalchemy import text
    from .database import _get_session_factory
    from .db_models import VisuraDocumentDB

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

            # Skip if duplicate exists (same property + document type)
            if foglio and particella:
                existing = await session.execute(text(
                    "SELECT id FROM visura_documents "
                    "WHERE foglio = :f AND particella = :p AND subalterno = :s AND document_type = :t LIMIT 1"
                ), {"f": foglio, "p": particella, "s": subalterno, "t": doc_type})
                if existing.fetchone():
                    log.debug("Documento duplicato saltato: %s F.%s P.%s Sub.%s", doc_type, foglio, particella, subalterno)
                    skipped += 1
                    continue

            row = VisuraDocumentDB(
                document_type=doc_type,
                file_format=doc.get("file_format", ""),
                filename=doc.get("filename", ""),
                file_path=doc.get("path"),
                file_size=doc.get("file_size"),
                oggetto=doc.get("oggetto"),
                richiesta_del=doc.get("richiesta_del"),
                provincia=parsed.get("provincia"),
                comune=parsed.get("comune"),
                foglio=foglio,
                particella=particella,
                subalterno=subalterno,
                sezione_urbana=parsed.get("sezione_urbana"),
                tipo_catasto=parsed.get("tipo_catasto"),
                intestati_json=json.dumps(parsed.get("intestati", []), ensure_ascii=False) if parsed.get("intestati") else None,
                dati_immobile_json=json.dumps({
                    "immobile": parsed.get("immobile", {}),
                    "classamento": parsed.get("classamento", []),
                    "indirizzo": parsed.get("indirizzo", ""),
                }, ensure_ascii=False) if parsed.get("immobile") or parsed.get("classamento") else None,
                xml_content=parsed.get("xml_content"),
            )
            session.add(row)
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


async def run_visura_soggetto(
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
        codice_fiscale, tipo_catasto, prov_label,
    )

    # STEP 1: Navigate to SceltaServizio
    await _navigate_to_scelta_servizio(page, page_logger)

    # STEP 2: Select province (NAZIONALE or specific)
    if provincia:
        provincia_value = await find_best_option_match(page, "select[name='listacom']", provincia)
        if not provincia_value:
            raise Exception(f"Provincia '{provincia}' non trovata")
        log.info("Provincia: [cyan]%s[/cyan]", provincia_value)
    else:
        # Select NAZIONALE for nationwide search
        provincia_value = await find_best_option_match(page, "select[name='listacom']", "NAZIONALE")
        if not provincia_value:
            raise Exception("Opzione NAZIONALE non trovata nel dropdown province")
        log.info("Ricerca nazionale")

    await page.locator("select[name='listacom']").select_option(provincia_value)
    await page.locator("input[type='submit'][value='Applica']").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "provincia_applicata")

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

    # STEP 5: Select "Codice Fiscale" radio button and fill the field
    log.info("Codice fiscale: [cyan]%s[/cyan]", codice_fiscale)

    # Click the Codice Fiscale radio button (field name: selDatiAna, value: CF)
    cf_radio = page.locator("input[name='selDatiAna'][value='CF']")
    if await cf_radio.count() == 0:
        cf_radio = page.locator("input[type='radio'][value='CF']")
    if await cf_radio.count() == 0:
        cf_radio = page.locator("input[type='radio']").last
    await cf_radio.click()

    # Fill the codice fiscale field (field name: cod_fisc_pf)
    cf_field = page.locator("input[name='cod_fisc_pf']")
    if await cf_field.count() == 0:
        cf_field = page.locator("input[name='codFiscale']")
    if await cf_field.count() == 0:
        cf_field = page.locator("input[name='codiceFiscale']")
    await cf_field.click()
    await cf_field.fill(codice_fiscale.upper())

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
        elapsed, len(immobili),
    )

    return {
        "soggetto": codice_fiscale,
        "immobili": immobili,
        "total_results": len(immobili),
    }


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
        identificativo, tipo_catasto, prov_label,
    )

    # STEP 1: Navigate to SceltaServizio
    await _navigate_to_scelta_servizio(page, page_logger)

    # STEP 2: Select province
    if provincia:
        provincia_value = await find_best_option_match(page, "select[name='listacom']", provincia)
        if not provincia_value:
            raise Exception(f"Provincia '{provincia}' non trovata")
    else:
        provincia_value = await find_best_option_match(page, "select[name='listacom']", "NAZIONALE")
        if not provincia_value:
            raise Exception("Opzione NAZIONALE non trovata nel dropdown province")

    await page.locator("select[name='listacom']").select_option(provincia_value)
    await page.locator("input[type='submit'][value='Applica']").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "provincia_applicata")

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
        provincia, comune, foglio_info, tipo_catasto,
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
    await page_logger.log(page, "form_compilato")
    log.info("Esecuzione elenco immobili...")
    ricerca_btn = page.locator("input[name='ricerca'][value='Ricerca']")
    if await ricerca_btn.count() == 0:
        ricerca_btn = page.locator("input[type='submit'][value='Ricerca']")
    if await ricerca_btn.count() == 0:
        ricerca_btn = page.locator("input[name='scelta'][value='Ricerca']")
    await ricerca_btn.click()
    await page.wait_for_load_state("networkidle", timeout=60000)
    await page_logger.log(page, "risultati_elenco")

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
    }


def _extract_result_tables(page_html: str) -> list:
    """Extract data rows from result tables in SISTER HTML."""
    soup = BeautifulSoup(page_html, "html.parser")
    for table in soup.find_all("table"):
        headers_text = " ".join(th.get_text(strip=True) for th in table.find_all("th"))
        if any(kw in headers_text for kw in ("Foglio", "Particella", "Comune", "Provincia", "Denominazione",
                                              "Nota", "Partita", "Indirizzo", "Fiduciale", "Mappa")):
            return parse_table(str(table))
    return []


async def _navigate_select_province_and_click(page, page_logger, provincia, menu_link_name):
    """Shared helper: navigate to SceltaServizio, select province, click a menu link."""
    await _navigate_to_scelta_servizio(page, page_logger)

    if provincia:
        provincia_value = await find_best_option_match(page, "select[name='listacom']", provincia)
        if not provincia_value:
            raise Exception(f"Provincia '{provincia}' non trovata")
    else:
        provincia_value = await find_best_option_match(page, "select[name='listacom']", "NAZIONALE")
        if not provincia_value:
            raise Exception("Opzione NAZIONALE non trovata")

    await page.locator("select[name='listacom']").select_option(provincia_value)
    await page.locator("input[type='submit'][value='Applica']").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "provincia_applicata")

    await page.get_by_role("link", name=menu_link_name, exact=True).click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, menu_link_name.lower().replace(" ", "_"))


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


async def _submit_and_extract(page, page_logger, step_name):
    """Submit a SISTER search form and extract results table."""
    await page_logger.log(page, f"form_compilato_{step_name}")
    ricerca_btn = page.locator("input[name='ricerca'][value='Ricerca']")
    if await ricerca_btn.count() == 0:
        ricerca_btn = page.locator("input[type='submit'][value='Ricerca']")
    if await ricerca_btn.count() == 0:
        ricerca_btn = page.locator("input[name='scelta'][value='Ricerca']")
    await ricerca_btn.click()
    await page.wait_for_load_state("networkidle", timeout=60000)
    await _wait_for_captcha(page)
    await page_logger.log(page, f"risultati_{step_name}")

    page_text = await page.inner_text("body")
    if "NESSUNA CORRISPONDENZA TROVATA" in page_text:
        return None

    return _extract_result_tables(await page.content())


# ---------------------------------------------------------------------------
# Additional SISTER search types
# ---------------------------------------------------------------------------


async def run_ricerca_indirizzo(
    page, provincia, comune, indirizzo, tipo_catasto="T", sezione=None,
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

    comune_value = await find_best_option_match(page, "select[name='denomComune']", comune)
    if not comune_value:
        raise Exception(f"Comune '{comune}' non trovato")
    await page.locator("select[name='denomComune']").select_option(comune_value)

    await _select_sezione(page, comune, sezione)

    ind_field = page.locator("input[name='indirizzo']")
    if await ind_field.count() == 0:
        ind_field = page.locator("input[name='via']")
    if await ind_field.count() > 0:
        await ind_field.fill(indirizzo)

    await _fill_richiedente_motivo(page)

    results = await _submit_and_extract(page, page_logger, "indirizzo")
    elapsed = time.time() - time0
    immobili = results or []
    log.info("[green]Ricerca indirizzo completata[/green] in %.1fs — %d risultati", elapsed, len(immobili))

    return {
        "provincia": provincia, "comune": comune, "indirizzo": indirizzo,
        "immobili": immobili, "total_results": len(immobili),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_ricerca_partita(
    page, provincia, comune, partita, tipo_catasto="T", sezione=None,
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

    comune_value = await find_best_option_match(page, "select[name='denomComune']", comune)
    if not comune_value:
        raise Exception(f"Comune '{comune}' non trovato")
    await page.locator("select[name='denomComune']").select_option(comune_value)

    partita_field = page.locator("input[name='partita']")
    if await partita_field.count() == 0:
        partita_field = page.locator("input[name='numPartita']")
    await partita_field.fill(str(partita))

    await _fill_richiedente_motivo(page)

    results = await _submit_and_extract(page, page_logger, "partita")
    elapsed = time.time() - time0
    immobili = results or []
    log.info("[green]Ricerca partita completata[/green] in %.1fs — %d risultati", elapsed, len(immobili))

    return {
        "provincia": provincia, "comune": comune, "partita": partita,
        "immobili": immobili, "total_results": len(immobili),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_ricerca_nota(
    page, provincia, numero_nota, anno_nota=None, tipo_catasto="T",
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
        "provincia": provincia, "numero_nota": numero_nota, "anno_nota": anno_nota,
        "risultati": rows, "total_results": len(rows),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_ricerca_mappa(
    page, provincia, comune, foglio, tipo_catasto="T", sezione=None, particella=None,
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
        "provincia": provincia, "comune": comune, "foglio": foglio,
        "risultati": rows, "total_results": len(rows),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_export_mappa(
    page, provincia, comune, foglio, tipo_catasto="T", sezione=None,
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
        "provincia": provincia, "comune": comune, "foglio": foglio,
        "risultati": rows, "total_results": len(rows),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_originali_impianto(
    page, provincia, comune, tipo_catasto="T", foglio=None,
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

    comune_value = await find_best_option_match(page, "select[name='denomComune']", comune)
    if comune_value:
        await page.locator("select[name='denomComune']").select_option(comune_value)

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
        "provincia": provincia, "comune": comune,
        "risultati": rows, "total_results": len(rows),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_punti_fiduciali(
    page, provincia, comune, tipo_catasto="T", foglio=None,
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

    comune_value = await find_best_option_match(page, "select[name='denomComune']", comune)
    if comune_value:
        await page.locator("select[name='denomComune']").select_option(comune_value)

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
        "provincia": provincia, "comune": comune,
        "risultati": rows, "total_results": len(rows),
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
        page, page_logger, provincia,
        "Passa a Ispezioni Cartacee" if cartacee else "Passa a Ispezioni"
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
    page, provincia, comune, tipo_catasto="T", foglio=None, particella=None, tipo_ricerca="PF",
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
        "provincia": provincia, "comune": comune,
        "risultati": rows, "total_results": len(rows),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_ispezioni_cartacee(
    page, provincia, comune, tipo_catasto="T", foglio=None, particella=None,
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
        "provincia": provincia, "comune": comune,
        "risultati": rows, "total_results": len(rows),
        **({"error": "NESSUNA CORRISPONDENZA TROVATA"} if results is None else {}),
    }


async def run_elaborato_planimetrico(
    page, provincia, comune, tipo_catasto="F", foglio=None,
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

    await _fill_richiedente_motivo(page)

    results = await _submit_and_extract(page, page_logger, "elaborato_planimetrico")
    elapsed = time.time() - time0
    rows = results or []
    log.info("[green]Elaborato planimetrico completato[/green] in %.1fs — %d risultati", elapsed, len(rows))

    return {
        "provincia": provincia, "comune": comune,
        "risultati": rows, "total_results": len(rows),
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
    await page.goto("https://sister3.agenziaentrate.gov.it/Visure/RiepilogoVisure/UtentiRiepilogoVisure.do", timeout=30000)
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "riepilogo_visure")

    # Extract the summary table
    results = _extract_result_tables(await page.content())
    elapsed = time.time() - time0
    log.info("[green]Riepilogo visure completato[/green] in %.1fs — %d risultati", elapsed, len(results))

    return {
        "risultati": results, "total_results": len(results),
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

    results = _extract_result_tables(await page.content())
    elapsed = time.time() - time0
    log.info("[green]Consultazione richieste completata[/green] in %.1fs — %d risultati", elapsed, len(results))

    return {
        "risultati": results, "total_results": len(results),
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
        r'[Cc]osto[:\s]+[€EUR\s]*([\d.,]+)',
        r'[Ii]mporto[:\s]+[€EUR\s]*([\d.,]+)',
        r'[Pp]rezzo[:\s]+[€EUR\s]*([\d.,]+)',
        r'€\s*([\d.,]+)',
        r'EUR\s*([\d.,]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, page_text)
        if match:
            cost_text = match.group(0).strip()
            cost_val = match.group(1).replace('.', '').replace(',', '.')
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
            "tipo_ricerca": tipo_ricerca, "provincia": provincia,
            "risultati": [], "total_results": 0, "cost": None,
            "error": "NESSUNA CORRISPONDENZA TROVATA",
        }

    # Handle cost confirmation
    cost_result = await _handle_cost_confirmation(page, page_logger, auto_confirm=auto_confirm)

    if not cost_result["confirmed"]:
        elapsed = time.time() - time0
        return {
            "tipo_ricerca": tipo_ricerca, "provincia": provincia,
            "risultati": [], "total_results": 0,
            "cost": {"text": cost_result.get("cost_text"), "value": cost_result.get("cost_value")},
            "confirmed": False,
            "error": cost_result.get("error", "Cost confirmation required"),
        }

    # Extract results after confirmation
    results = _extract_result_tables(await page.content())
    elapsed = time.time() - time0
    log.info("[green]Ispezione ipotecaria completata[/green] in %.1fs — %d risultati, costo: %s",
             elapsed, len(results), cost_result.get("cost_text", "N/A"))

    return {
        "tipo_ricerca": tipo_ricerca, "provincia": provincia,
        "risultati": results, "total_results": len(results),
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
                comune_options = await page.locator("select[name='denomComune'] option").all()
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
                        await page.locator("select[name='denomComune']").select_option(comune["value"])

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
                    provincia["text"], len(sezioni_data),
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
    page, provincia="Trieste", comune="Trieste", sezione=None, foglio="9", particella="166", subalterno=None, sezione_urbana=None
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
        provincia, comune, foglio, particella, subalterno, sezione_info,
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
    comune_value = await find_best_option_match(page, "select[name='denomComune']", comune)
    if not comune_value:
        raise Exception(f"Comune '{comune}' non trovato")

    log.info("Comune: [cyan]%s[/cyan]", comune_value)
    await page.locator("select[name='denomComune']").select_option(comune_value)

    await _select_sezione(page, comune, sezione)

    # Fill "Sezione urbana" — only when explicitly provided (separate from dropdown sezione)
    if sezione_urbana:
        sez_urb_field = page.locator("input[name='sezUrb']")
        if await sez_urb_field.count() > 0:
            await sez_urb_field.fill(str(sezione_urbana).upper())
            log.info("Sezione urbana: [cyan]%s[/cyan]", sezione_urbana)

    # Inserisci dati immobile
    log.info("Foglio: [cyan]%s[/cyan]  Particella: [cyan]%s[/cyan]  Sub: [cyan]%s[/cyan]", foglio, particella, subalterno)
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

    result = {"immobile": immobile_data, "intestati": intestati, "total_intestati": len(intestati), "page_visits": page_logger.page_visits}

    return result
