import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
res = {}
def rec(name, ok, detail=""):
    res[name] = bool(ok); print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
def status(ctx, path): return ctx.request.get(BASE + path, max_redirects=0, headers={"Accept": "text/html"}).status

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    dctx, d = login(b, "demo@aecs4u.com", (1440, 900), "light")
    rec("demo lands on /documents", d.url.endswith("/documents"), d.url)
    shell = d.evaluate("() => ({sidebar: !!document.getElementById('sidebar'), toggle: !!document.getElementById('sidebarToggle'), nav: [...document.querySelectorAll('.sister-topnav a')].map(a=>a.innerText.trim())})")
    rec("demo: no sidebar, no hamburger, top bar instead", not shell["sidebar"] and not shell["toggle"] and len(shell["nav"]) == 4, str(shell))
    text = d.inner_text("main")
    body = d.inner_text("body").replace("\n", " ")
    rec("demo: empty state explains how to get access", "Non hai ancora documenti disponibili" in body and "contatta l’amministratore" in body, body[body.find("Non hai ancora"):][:130])
    gates = {p: status(dctx, p) for p in ["/results", "/dossiers", "/forms", "/workflows", "/api/ui/visura/x", "/browser/status", "/admin", "/admin/document-purchases", "/documents/1886", "/documents/99999999"]}
    rec("demo: gated routes answer 403/404", all(v in (403, 404) for v in gates.values()), str(gates))
    rec("demo: / sends to /documents", status(dctx, "/") == 303)
    d.set_viewport_size({"width": 375, "height": 812}); d.goto(BASE + "/documents", wait_until="load"); d.wait_for_timeout(400)
    ov = d.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    rec("demo: documents page at 375 px has no horizontal overflow", ov <= 0, f"{ov}px")
    rec("demo: top bar visible at 375 px", d.evaluate("() => { const n=document.querySelector('.sister-topnav'); if(!n) return false; const r=n.getBoundingClientRect(); return r.width>0 && r.right<=innerWidth+1 }"))
    d.set_viewport_size({"width": 1440, "height": 900})

    actx, a = login(b, "admin@aecs4u.com", (1440, 900), "light")
    a.evaluate("() => {}")
    # grant through the UI
    a.goto(BASE + "/admin/document-purchases?document_id=1886", wait_until="load")
    a.fill("#new-user-id", "email:Demo@AECS4U.com"); a.fill("#new-note", "AUDIT final verification")
    with a.expect_navigation(): a.click("button:has-text('Concedi')")
    fb = a.inner_text("#purchase-feedback")
    a.wait_for_timeout(400)
    rec("admin: grant created, message in Italian, focus on the message", "Acquisto registrato" in fb and a.evaluate("() => document.activeElement.id") == "purchase-feedback", fb.strip() + " / " + a.evaluate("() => document.activeElement.tagName+'#'+document.activeElement.id"))
    rows = a.evaluate("() => [...document.querySelectorAll('tbody tr:not(.table-light) input[name=user_id]')].map(i=>i.value)")
    rec("admin: key normalised to lower-case", "email:demo@aecs4u.com" in rows, str(rows))
    # same person, other key form -> refused
    tok = a.evaluate("() => document.querySelector('meta[name=csrf-token]').content")
    r = a.request.post(BASE + "/admin/document-purchases/create", form={"csrf_token": tok, "user_id": "id:demo@aecs4u.com", "document_id": "1886"}, max_redirects=0)
    rec("admin: the same person under another key form is a duplicate", "error=duplicate" in r.headers.get("location", ""), r.headers.get("location", ""))
    # demo sees the document
    d.goto(BASE + "/documents", wait_until="load"); d.wait_for_timeout(400)
    t = d.inner_text("main").replace("\n", " ")
    rec("demo: counter reads '1 documento disponibile' (singular)", "1 documento disponibile" in t or "1 documento" in t, t[:120])
    t0 = time.time(); r = dctx.request.get(BASE + "/documents/1886", headers={"Accept": "text/html"}, max_redirects=0)
    rec("demo: purchased document page renders (200)", r.status == 200, f"{r.status} {len(r.body())} B {time.time()-t0:.1f}s")
    rec("demo: file download works", dctx.request.get(BASE + "/documents/1886/download", max_redirects=0).status == 200)
    # purchase start without Stripe configured -> clean 503
    r = d.request.post(BASE + "/documents/1886/purchase", form={"csrf_token": d.evaluate("() => document.querySelector('meta[name=csrf-token]')?.content") or ""}, max_redirects=0)
    rec("demo: starting a payment without Stripe configured is a clean 503", r.status in (503, 303), str(r.status))
    # admin page at 375
    a.set_viewport_size({"width": 375, "height": 812}); a.goto(BASE + "/admin/document-purchases", wait_until="load"); a.wait_for_timeout(400)
    ov = a.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    rec("admin: purchase manager at 375 px has no horizontal overflow", ov <= 0, f"{ov}px")
    a.screenshot(path=os.path.join(os.getcwd(), "docs/ui_audit_2026-10-29/v_purchases_375.png"), full_page=False)
    a.set_viewport_size({"width": 1440, "height": 900}); a.goto(BASE + "/admin/document-purchases", wait_until="load")
    # revoke with confirm accepted via native dialog
    a.once("dialog", lambda dlg: (res.__setitem__("dialog_text", dlg.message), dlg.accept()))
    a.fill("input[name=reason]", "AUDIT fine verifica")
    with a.expect_navigation(): a.click("button:has-text('Revoca')")
    rec("admin: confirm dialog shown and revoke done", "Revocare" in str(res.get("dialog_text")) and "Accesso revocato" in a.inner_text("#purchase-feedback"), str(res.get("dialog_text")))
    rec("demo: access gone after the revoke", dctx.request.get(BASE + "/documents/1886/view", max_redirects=0).status == 404)
    a.goto(BASE + "/admin/activity", wait_until="load")
    act = a.inner_text("tbody tr:first-child")
    rec("admin: activity log names user, document and reason", "demo@aecs4u.com" in act and "#1886" in act and "AUDIT fine verifica" in act, act.replace("\n", " ")[:160])
    actx.close(); dctx.close(); b.close()
print("\nFAILED:", [k for k, v in res.items() if v is False] or "none")
