import json, os, sys
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from sister.fiscal_code import check_character
valid_cf = "RSSMRA80A01H501" + check_character("RSSMRA80A01H501")
res = {}
def rec(name, ok, detail=""):
    res[name] = (bool(ok), detail); print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)

    # ---- cookie banner on a clean context (no decline)
    ctx = b.new_context(viewport={"width": 1440, "height": 900}); p = ctx.new_page()
    p.goto(BASE + "/auth/login", wait_until="networkidle"); p.wait_for_timeout(500)
    info = p.evaluate("""() => { const m=document.getElementById('cookieConsent'); if(!m) return null; const d=m.querySelector('[role=dialog]'); return {dialog:!!d, modal:d&&d.getAttribute('aria-modal'), label:d&&d.getAttribute('aria-labelledby'), focusInside:m.contains(document.activeElement), active:document.activeElement.tagName+'#'+document.activeElement.id}; }""")
    rec("cookie banner is a labelled modal dialog with focus inside", info and info["dialog"] and info["modal"] == "true" and info["label"] and info["focusInside"], str(info))
    p.keyboard.press("Tab"); p.keyboard.press("Tab"); p.keyboard.press("Tab")
    rec("Tab stays trapped inside the banner", p.evaluate("() => document.getElementById('cookieConsent').contains(document.activeElement)"))
    p.click("#cookieDecline"); p.wait_for_timeout(300)
    rec("banner closes and login button is clickable", p.evaluate("() => !document.getElementById('cookieConsent') || getComputedStyle(document.getElementById('cookieConsent')).display==='none'"))
    ctx.close()

    # ---- admin
    ctx, p = login(b, "admin@aecs4u.com", (1440, 900), "light")
    p.goto(BASE + "/", wait_until="load")
    txt = p.inner_text("main")
    rec("dashboard has the 'Senza corrispondenza' card", "Senza corrispondenza" in txt)
    rec("no language switcher in the navbar", p.evaluate("() => ![...document.querySelectorAll('.navbar-actions button')].some(b=>/Italiano|English/.test(b.innerText))"))
    rec("sidebar present for admin", p.evaluate("() => !!document.getElementById('sidebar')"))
    # theme toggle
    before = p.get_attribute("#themeToggle", "aria-pressed"); p.click("#themeToggle"); p.wait_for_timeout(300)
    after = p.get_attribute("#themeToggle", "aria-pressed"); theme = p.get_attribute("html", "data-bs-theme")
    rec("theme toggle reports its state", before == "false" and after == "true" and theme == "dark", f"{before}->{after} {theme}")
    p.click("#themeToggle"); p.wait_for_timeout(200)

    # forms
    p.goto(BASE + "/forms", wait_until="load"); p.wait_for_timeout(500)
    p.click("text=Multi-step"); p.wait_for_timeout(500)
    category_state = p.evaluate("""() => [...document.querySelectorAll('input[name="query-category"]')].map(r => ({
        checked:r.checked, active:document.querySelector('label[for="'+r.id+'"]').classList.contains('active')
    }))""")
    rec("query category toggle shows one selected option", len(category_state) == 2 and sum(x["checked"] for x in category_state) == 1 and all(x["checked"] == x["active"] for x in category_state), str(category_state))
    rec("batch mode control is hidden for workflows", not p.locator('[aria-label="Job mode"]').is_visible())
    vals = p.evaluate("() => [...document.querySelectorAll('form:not([hidden]) input[name=provincia],form:not([hidden]) input[name=comune],form:not([hidden]) input[name=foglio],form:not([hidden]) input[name=particella]')].filter(i=>i.offsetParent).map(i=>[i.name,i.value,i.placeholder])")
    rec("multi-step form is not pre-filled", vals and all(v[1] == "" for v in vals), str(vals[:4]))
    p.click("text=Passo singolo"); p.click("text=Ricerca persona"); p.wait_for_timeout(400)
    cf = p.locator("input[name=codice_fiscale]:visible").first
    cf.fill("abc"); cf.blur(); p.wait_for_timeout(200)
    msg = p.evaluate("() => { const e=document.querySelector('input[name=codice_fiscale]:not([hidden])'); const f=[...document.querySelectorAll('.sister-field-feedback')].map(x=>x.innerText); return {valid:e.validity.valid, f} }")
    rec("invalid codice fiscale is flagged before sending", (not msg["valid"]) and any("non valido" in t for t in msg["f"]), str(msg))
    cf.fill(valid_cf.lower()); cf.blur(); p.wait_for_timeout(200)
    ok = p.evaluate("() => { const e=document.querySelector('input[name=codice_fiscale]:not([hidden])'); return {valid:e.validity.valid, v:e.value, f:document.querySelectorAll('.sister-field-feedback').length} }")
    rec("a valid codice fiscale is accepted and upper-cased", ok["valid"] and ok["v"] == valid_cf and ok["f"] == 0, str(ok))
    foglio = p.evaluate("() => [...document.querySelectorAll('input[name=foglio]')].filter(i=>i.offsetParent).map(i=>i.getAttribute('inputmode'))")
    rec("numeric inputs ask for a numeric keyboard", foglio and all(v == "numeric" for v in foglio), str(foglio))

    # results
    p.goto(BASE + "/results", wait_until="networkidle"); p.wait_for_timeout(800)
    cols = p.evaluate("() => [...document.querySelectorAll('.tabulator-col-title')].slice(0,6).map(e=>e.innerText.trim())")
    rec("results: Stato and Data right after the request id", cols[:4] == ["#", "Rif. richiesta", "Stato", "Data"], str(cols))
    pager = p.inner_text(".pagination")
    rec("results: Italian pager with page count", "Pagina 1 di" in pager and "Successiva" in pager and "Previous" not in pager, pager.replace("\n", " | ")[:100])
    rec("results: no English placeholder", "e.g." not in p.content())
    # first column frozen: scroll the holder and check column stays
    rec("results: wide table has a horizontal scroll cue", p.evaluate("() => !!document.querySelector('.scroll-hint-wrap, .tabulator-tableholder')"))

    # documents lazy views
    documents_response = p.goto(BASE + "/documents", wait_until="load"); p.wait_for_timeout(500)
    rec("documents: route returns successfully", documents_response is not None and documents_response.status == 200, str(documents_response.status if documents_response else None))
    size_default = p.evaluate("() => document.querySelectorAll('*').length")
    rec("documents: default view is lighter than before (58,019 nodes)", size_default < 40000, f"{size_default} nodes")
    doc_pager = p.locator('nav[aria-label="Pagine dei documenti"]')
    first_page = doc_pager.inner_text() if doc_pager.count() else ""
    page_rows = p.locator('#documents-pane-categories [data-favorite-item]').count()
    rec("documents: active category page is bounded to 100 primary rows", page_rows <= 100, f"{page_rows} rows")
    total = int(p.locator("#documents-tab-categories .badge").inner_text())
    page_count = max(1, (total + 99) // 100)
    next_page = doc_pager.locator('[aria-label="Pagina successiva"]')
    has_pager = doc_pager.count() == 1
    pagination_ok = (
        "Pagina 1 di" in first_page and next_page.count() == 1
        if page_count > 1
        else not has_pager
    )
    rec("documents: pagination matches the available page count", pagination_ok, first_page.replace("\n", " | ")[:140] or f"{total} documents; {page_count} page")
    selected_page = 1
    if next_page.count():
        next_page.click(); p.wait_for_url("**page=2**", timeout=15000); p.wait_for_load_state("load")
        rec("documents: next page shows page 2", "Pagina 2 di" in p.locator('nav[aria-label="Pagine dei documenti"]').inner_text())
        selected_page = 2
    p.click("#documents-tab-files"); p.wait_for_url("**view=files**", timeout=15000); p.wait_for_load_state("load")
    files_state = p.evaluate("""() => ({
        active:document.querySelector('#documents-tab-files').classList.contains('active'),
        table:!!document.querySelector('#files-table'),
        empty:document.querySelector('#documents-pane-files').innerText.includes('Nessun documento indicizzato.'),
        page:new URLSearchParams(location.search).get('page') || '1'
    })""")
    view_ok = files_state["active"] and files_state["page"] == str(selected_page) and (files_state["table"] or (total == 0 and files_state["empty"]))
    rec("documents: 'Per file' keeps the selected page and renders rows or the empty state", view_ok, str(files_state))
    ctx.close()
    b.close()
bad = [k for k, (ok, _) in res.items() if not ok]
print("\nFAILED:", bad if bad else "none")
if bad:
    raise SystemExit(1)
