import asyncio
import logging
import time

from aecs4u_auth.browser import PageLogger
from bs4 import BeautifulSoup
from playwright.async_api import Page

log = logging.getLogger("visura-api.utils")

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

    # IMPORTANTE: Selezionare sezione solo se specificata (non None e non "_")
    if sezione:
        log.info("Selezionando sezione: [cyan]%s[/cyan]", sezione)
        await page.locator("input[name='selSezione'][value='scegli la sezione']").click()
        await page.wait_for_load_state("networkidle", timeout=30000)

        options = await page.locator("select[name='sezione'] option").all()
        available_sections = []
        for option in options:
            value = await option.get_attribute("value")
            text = await option.inner_text()
            if value and text:
                available_sections.append(f"{text} ({value})")

        if not available_sections:
            log.warning("Nessuna sezione disponibile per '%s', skip", comune)
        else:
            sezione_value = await find_best_option_match(page, "select[name='sezione']", sezione)
            if not sezione_value:
                log.warning("Sezione '%s' non trovata. Disponibili: %s", sezione, ", ".join(available_sections))
            else:
                try:
                    await page.locator("select[name='sezione']").select_option(sezione_value)
                except Exception as e:
                    log.warning("Errore selezione sezione '%s': %s", sezione_value, e)

    # Inserisci foglio, particella, subalterno
    log.info("Foglio: [cyan]%s[/cyan]  Particella: [cyan]%s[/cyan]%s", foglio, particella, f"  Sub: [cyan]{subalterno}[/cyan]" if subalterno else "")
    await page.locator("input[name='foglio']").click()
    await page.locator("input[name='foglio']").fill(str(foglio))
    await page.locator("input[name='particella1']").click()
    await page.locator("input[name='particella1']").fill(str(particella))
    if subalterno:
        await page.locator("input[name='subalterno1']").fill(str(subalterno))

    # Clicca Ricerca
    log.info("Esecuzione ricerca...")
    await page.locator("input[name='scelta'][value='Ricerca']").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "ricerca")

    # STEP 3: Gestisci conferma assenza subalterno (se necessario)
    try:
        conferma_button = page.locator("input[name='confAssSub'][value='Conferma']")
        if await conferma_button.count() > 0:
            log.debug("Conferma assenza subalterno richiesta")
            await conferma_button.click()
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page_logger.log(page, "conferma_subalterno")
    except Exception as e:
        log.debug("Conferma subalterno non necessaria: %s", e)

    await page_logger.log(page, "risultati")

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

    # Se non servono intestati, la tabella immobili è tutto ciò che serve
    if not extract_intestati:
        elapsed = time.time() - time0
        log.info("[green]Visura completata[/green] in %.1fs — %d immobili", elapsed, len(immobili))
        return {
            "immobili": immobili,
            "results": [],
            "total_results": len(immobili),
            "intestati": [],
        }

    # STEP 5: Estrai intestati (solo quando extract_intestati=True)
    log.info("Estraendo intestati...")
    intestati = []

    try:
        intestati_button_selectors = [
            "input[name='intestati'][value='Intestati']",
            "input[value='Intestati']",
            "input[name='intestati']",
            "button:has-text('Intestati')",
            "input[type='submit'][value*='ntestat']",
            "*[value='Intestati']",
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

            intestati_selectors = [
                "table.listaIsp4",
                "table[class*='lista']",
                "table:has(th:text('Nominativo o denominazione'))",
                "table:has(th:text('Codice fiscale'))",
                "table:has(th:text('Titolarità'))",
                "table:has(th:text('Cognome'))",
                "table:has(th:text('Nome'))",
                "table",
            ]

            for selector in intestati_selectors:
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

    except Exception as e:
        log.error("Errore estrazione intestati: %s", e)

    elapsed = time.time() - time0
    log.info("[green]Visura completata[/green] in %.1fs — %d immobili, %d intestati", elapsed, len(immobili), len(intestati))

    result = {
        "immobili": immobili,
        "results": [{"result_index": 1, "immobile": immobili[0] if immobili else {}, "intestati": intestati}],
        "total_results": len(immobili),
        "intestati": intestati,
    }

    return result


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
    page, provincia="Trieste", comune="Trieste", sezione=None, foglio="9", particella="166", subalterno=None
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

    # Seleziona sezione se specificata
    if sezione:
        log.info("Selezionando sezione: [cyan]%s[/cyan]", sezione)
        await page.locator("input[name='selSezione'][value='scegli la sezione']").click()
        await page.wait_for_load_state("networkidle", timeout=30000)

        options = await page.locator("select[name='sezione'] option").all()
        available_sections = []
        for option in options:
            value = await option.get_attribute("value")
            text = await option.inner_text()
            if value and text:
                available_sections.append(f"{text} ({value})")

        if not available_sections:
            log.warning("Nessuna sezione disponibile per '%s', skip", comune)
        else:
            sezione_value = await find_best_option_match(page, "select[name='sezione']", sezione)
            if not sezione_value:
                log.warning("Sezione '%s' non trovata. Disponibili: %s", sezione, ", ".join(available_sections))
            else:
                try:
                    await page.locator("select[name='sezione']").select_option(sezione_value)
                except Exception as e:
                    log.warning("Errore selezione sezione '%s': %s", sezione_value, e)

    # Inserisci dati immobile
    log.info("Foglio: [cyan]%s[/cyan]  Particella: [cyan]%s[/cyan]  Sub: [cyan]%s[/cyan]", foglio, particella, subalterno)
    await page.locator("input[name='foglio']").fill(str(foglio))
    await page.locator("input[name='particella1']").fill(str(particella))
    await page.locator("input[name='subalterno1']").fill(str(subalterno))

    # Clicca Ricerca
    log.info("Esecuzione ricerca...")
    await page.locator("input[name='scelta'][value='Ricerca']").click()
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page_logger.log(page, "ricerca")

    # STEP 3: Gestisci conferma assenza subalterno (se necessario)
    try:
        conferma_button = page.locator("input[name='confAssSub'][value='Conferma']")
        if await conferma_button.count() > 0:
            log.debug("Conferma assenza subalterno richiesta")
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

    result = {"immobile": immobile_data, "intestati": intestati, "total_intestati": len(intestati)}

    return result
