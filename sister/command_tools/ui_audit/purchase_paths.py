"""Buy-link and prepaid-credits flow against a throwaway instance started with
SISTER_DOCUMENT_CREDITS=10 SISTER_WALLET_ALLOW_VOLATILE=true SISTER_DOCUMENT_PRICE_CENTS=1290 on another port
(SISTER_URL=http://localhost:8026). Uses the in-memory wallet; leaves a wallet-sourced grant of document 1886 for the demo user
(delete it: delete from document_purchase where source='wallet')."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
res = {}
def rec(name, ok, detail=""):
    res[name] = bool(ok); print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    actx, a = login(b, "admin@aecs4u.com", (1440, 900), "light")
    a.goto(BASE + "/admin/document-purchases?document_id=1886", wait_until="load")
    link = a.input_value("#buy-link")
    rec("admin: manager shows a signed buy link and the credits form", "/buy/" in link and a.query_selector("#credit-user") is not None, link[:60] + "…")
    rec("admin: price and credit cost shown next to the link", "10" in a.inner_text("#buy-link-heading + p, #buy-link-heading ~ p") )
    path = "/buy/" + link.split("/buy/")[1]

    dctx, d = login(b, "demo@aecs4u.com", (1440, 900), "light")
    d.goto(BASE + path, wait_until="load")
    body = d.inner_text("main")
    rec("demo: buy page names the document, ID and the credit price", "ID 1886" in body and "Paga con crediti (10)" in body, body[:110].replace("\n", " "))
    rec("demo: card payment not offered (Stripe not configured)", "Paga con carta" not in body)
    rec("demo: pay-with-credits disabled while the balance is 0", d.is_disabled("form[data-confirm] button") and "Saldo insufficiente" in body)
    rec("forged buy link is a 404", dctx.request.get(BASE + path[:-3] + "abc", max_redirects=0, headers={"Accept": "text/html"}).status == 404)
    anon = b.new_context(); r = anon.request.get(BASE + path, max_redirects=0, headers={"Accept": "text/html"})
    rec("buy link requires a login (redirect)", r.status in (302, 303, 307), str(r.status)); anon.close()

    a.fill("#credit-user", "email:Demo@AECS4U.com"); a.fill("#credit-amount", "25"); a.fill("#credit-reason", "AUDIT test credits")
    with a.expect_navigation(): a.click("button:has-text('Accredita')")
    rec("admin: credits granted, new balance shown", "Nuovo saldo: 25" in a.inner_text("#purchase-feedback"), a.inner_text("#purchase-feedback")[:80])
    tok = a.evaluate("() => document.querySelector('meta[name=csrf-token]').content")
    r = a.request.post(BASE + "/admin/credits/grant", form={"csrf_token": tok, "user_id": "id:demo@aecs4u.com", "amount": "-5"}, max_redirects=0)
    rec("admin: a negative amount is refused", "error=bad_amount" in r.headers.get("location", ""), r.headers.get("location", ""))

    d.goto(BASE + path, wait_until="load")
    rec("demo: balance 25 shown and the button enabled", "saldo 25" in d.inner_text("main") and not d.is_disabled("form[data-confirm] button"))
    d.once("dialog", lambda dlg: dlg.accept())
    with d.expect_navigation(): d.click("form[data-confirm] button")
    rec("demo: paying with credits opens the document", "/documents/1886" in d.url, d.url.replace(BASE, ""))
    rec("demo: document download works", dctx.request.get(BASE + "/documents/1886/download", max_redirects=0).status == 200)
    d.goto(BASE + "/profile", wait_until="load")
    rec("demo: profile shows the remaining 15 credits", "15" in d.inner_text("dl"), d.inner_text("dl")[-60:].replace("\n", " "))
    d.goto(BASE + path, wait_until="load")
    rec("demo: the link now says the document is already owned (no second charge)", "Hai già accesso" in d.inner_text("main"))
    r = dctx.request.post(BASE + "/documents/1886/purchase", form={"method": "credits", "csrf_token": d.evaluate("() => document.querySelector('meta[name=csrf-token]').content")}, max_redirects=0)
    d.goto(BASE + "/profile", wait_until="load")
    rec("demo: a repeated POST does not charge again", "15" in d.inner_text("dl"))
    a.goto(BASE + "/admin/document-purchases", wait_until="load")
    row = a.inner_text("tr.table-light")
    rec("admin: the list shows the wallet source and payment reference", "credito" in row and "wallet:" in row, row.replace("\n", " ")[:140])
    b.close()
print("\nFAILED:", [k for k, v in res.items() if not v] or "none")
