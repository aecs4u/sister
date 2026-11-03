import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
AUDIT_USER = sys.argv[1] if len(sys.argv) > 1 else "admin@aecs4u.com"
if AUDIT_USER.casefold() == "demo@aecs4u.com":
    pages = [
        "/", "/documents", "/documents?view=map", "/documents?view=files", "/documents?view=cadastral-map",
        "/favorites", "/profile", "/settings", "/terms", "/help", "/contact", "/notifications", "/about",
        "/valuation", "/guide", "/3d", "/persons/", "/companies/",
    ]
else:
    pages = [
        "/", "/forms", "/results", "/workflows", "/favorites", "/dossiers", "/admin", "/admin/documents",
        "/admin/activity", "/admin/document-purchases", "/documents", "/documents?view=map",
        "/documents?view=files", "/glossary", "/valuation", "/guide", "/cheatsheet", "/browser", "/profile",
        "/settings", "/terms", "/help", "/contact", "/notifications", "/about", "/persons/", "/companies/",
        "/companies/04158680134",
    ]
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    summary = {}
    VARIANTS = [v for v in (("light", (1440, 900)), ("dark", (1440, 900)), ("light", (375, 812))) if f"{v[0]}{v[1][0]}" in os.environ.get("VARIANTS", "light1440,dark1440,light375")]
    for theme, vp in VARIANTS:
        ctx, page = login(browser, AUDIT_USER, vp, theme)
        key = f"{theme}{vp[0]}"
        for path in pages:
            try:
                r = page.goto(BASE + path, wait_until="load", timeout=45000); page.wait_for_timeout(700)
                data = page.evaluate(PROBE); data["status"] = r.status if r else None
                summary.setdefault(path, {})[key] = {k: v for k, v in data.items() if v not in (0, [], None, False) or k in ("status", "sidebar")}
            except Exception as exc:
                summary.setdefault(path, {})[key] = {"error": str(exc)[:100]}
        ctx.close()
    browser.close()
json.dump(summary, open(os.path.join(os.getenv("TMPDIR", "/tmp"), "ui_audit_sweep.json"), "w"), indent=1)
for path, modes in summary.items():
    for key, d in modes.items():
        flags = []
        if d.get("status") != 200: flags.append(f"status={d.get('status')}")
        if d.get("hOverflow", 0) > 0: flags.append(f"hOverflow={d['hOverflow']}")
        if d.get("main", 1) != 1: flags.append(f"main={d.get('main')}")
        if d.get("contrastFail"): flags.append(f"contrast={d['contrastFail']} worst={d.get('contrastWorst', [{}])[0]}")
        if d.get("below24"):
            flags.append(f"targets<24={d['below24']} {d.get('smallTargets', [])}")
        if d.get("bright"): flags.append(f"bright={d['bright']}")
        if d.get("dupIds"): flags.append(f"dup={d['dupIds']}")
        if d.get("inlineHandlers"): flags.append(f"inline={d['inlineHandlers']}")
        if d.get("unnamed"): flags.append(f"unnamed={d['unnamed'][:3]}")
        if d.get("headingSkips"): flags.append(f"skips={d['headingSkips']}")
        if d.get("error"): flags.append(d["error"])
        print(f"{path:34} {key:9} {' | '.join(flags) or 'ok'}")
