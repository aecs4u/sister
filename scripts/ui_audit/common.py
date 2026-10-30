"""Headless-browser checks used by the 2026-10-29 audits (docs/ui_audit_2026-10-29*.md).

Run from the repo root with the shared venv (Playwright + Chromium installed), the service on :8025 and an admin and a
demo account in the users file:  python scripts/ui_audit/sweep.py | interactions.py | demo_purchases.py
They only read, except demo_purchases.py, which grants/revokes document 1886 to the demo user and leaves a row whose note
starts with AUDIT (delete it afterwards: delete from document_purchase where note like 'AUDIT%').
"""
import json, re, sys
from playwright.sync_api import sync_playwright
import os
BASE = os.getenv("SISTER_URL", "http://localhost:8025")
USERS_FILE = os.getenv("SISTER_AUDIT_USERS_FILE", "../local_users.txt")  # "user  password" per line; never printed

def creds(user):
    for line in open(USERS_FILE, encoding="utf-8"):
        e = line.strip()
        if e and not e.startswith("#"):
            u, p = e.split(maxsplit=1)
            if u.casefold() == user.casefold():
                return u, p
    raise SystemExit("no such user")

PROBE = r"""
() => {
  const out = {};
  const cv = document.createElement('canvas'); cv.width = cv.height = 1; const cx = cv.getContext('2d', {willReadFrequently:true});
  const rgba = (c) => { cx.clearRect(0,0,1,1); cx.fillStyle = '#000'; cx.fillStyle = c; cx.fillRect(0,0,1,1); const d = cx.getImageData(0,0,1,1).data; return [d[0],d[1],d[2],d[3]/255]; };
  const lum = ([r,g,b]) => { const f = v => { v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4); }; return 0.2126*f(r)+0.7152*f(g)+0.0722*f(b); };
  const dark = document.documentElement.dataset.bsTheme === 'dark';
  const bgOf = (el) => { let layers=[]; let e=el; while (e && e.nodeType===1) { const cs=getComputedStyle(e); if (cs.backgroundImage && cs.backgroundImage!=='none') return null; const c=rgba(cs.backgroundColor); if (c[3]>0) { layers.push(c); if (c[3]>=1) break; } e=e.parentElement; } let base=dark?[18,18,18]:[255,255,255]; for (let i=layers.length-1;i>=0;i--){ const [r,g,b,a]=layers[i]; base=[r*a+base[0]*(1-a), g*a+base[1]*(1-a), b*a+base[2]*(1-a)]; } return base; };
  const vis = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el); return r.width>0 && r.height>0 && cs.visibility!=='hidden' && cs.display!=='none' && cs.opacity!=='0'; };
  const sel = (el) => { let s = el.tagName.toLowerCase(); if (el.id) s += '#'+el.id; else if (el.className && typeof el.className==='string') s += '.'+el.className.trim().split(/\s+/).slice(0,2).join('.'); return s; };
  const de = document.documentElement;
  out.title = document.title; out.nodes = document.querySelectorAll('*').length;
  out.hOverflow = de.scrollWidth - de.clientWidth;
  out.main = document.querySelectorAll('main').length; out.sidebar = !!document.getElementById('sidebar');
  const h = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(vis); let prev=0, skips=[];
  h.forEach(x=>{ const l=+(x.getAttribute('aria-level')||x.tagName[1]); if (prev && l>prev+1) skips.push(`h${prev}->h${l}`); prev=l; }); out.h1 = h.filter(x=>x.tagName==='H1').length; out.headingSkips = skips.slice(0,4);
  const ids={}; document.querySelectorAll('[id]').forEach(e=>ids[e.id]=(ids[e.id]||0)+1); out.dupIds = Object.entries(ids).filter(([k,v])=>v>1).map(([k])=>k).slice(0,4);
  out.inlineHandlers = [...document.querySelectorAll('*')].filter(e=>[...e.attributes].some(a=>/^on/.test(a.name))).length;
  const name = (el) => (el.getAttribute('aria-label') || el.innerText || el.value || el.title || el.querySelector('img[alt]')?.alt || '').trim();
  out.unnamed = [...document.querySelectorAll('button,[role=button],a[href]')].filter(vis).filter(e=>!name(e)).map(e=>sel(e)+(e.getAttribute('href')?'['+e.getAttribute('href').slice(0,40)+']':'')).slice(0,6);
  out.unlabelled = [...document.querySelectorAll('input:not([type=hidden]):not([type=submit]),select,textarea')].filter(vis).filter(e=>!(e.labels&&e.labels.length)&&!e.getAttribute('aria-label')&&!e.getAttribute('aria-labelledby')&&!e.title).length;
  const tg = [...document.querySelectorAll('a[href],button,input:not([type=hidden]),select')].filter(vis);
  const smallTargets = tg.map(e=>{const r=e.getBoundingClientRect(); return {e,w:Math.round(r.width),h:Math.round(r.height)};})
    .filter(x=>x.w<24||x.h<24);
  out.below24 = smallTargets.length;
  out.smallTargets = smallTargets.slice(0,12).map(x=>({s:sel(x.e),w:x.w,h:x.h}));
  const bad=[]; const seen=new Set(); const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT); let n;
  while ((n=w.nextNode())) { const t=n.textContent.trim(); if (t.length<2) continue; const el=n.parentElement; if (!el||seen.has(el)||!vis(el)||['SCRIPT','STYLE','NOSCRIPT'].includes(el.tagName)) continue; seen.add(el);
    const cs=getComputedStyle(el); const fg=rgba(cs.color); const bg=bgOf(el); if (!bg) continue; const a=fg[3]; const f=[fg[0]*a+bg[0]*(1-a),fg[1]*a+bg[1]*(1-a),fg[2]*a+bg[2]*(1-a)];
    const L1=lum(f),L2=lum(bg); const ratio=(Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05); const size=parseFloat(cs.fontSize); const bold=+cs.fontWeight>=700; const need=(size>=24||(size>=18.66&&bold))?3:4.5;
    if (ratio<need-0.05) bad.push({s:sel(el),r:+ratio.toFixed(2)}); }
  out.contrastFail = bad.length; out.contrastWorst = bad.sort((a,b)=>a.r-b.r).slice(0,4);
  if (dark) out.bright = [...document.querySelectorAll('body *')].filter(vis).filter(e=>{const r=e.getBoundingClientRect(); if (r.width<150||r.height<20||e.closest('.leaflet-container,.leaflet-popup')) return false; const c=rgba(getComputedStyle(e).backgroundColor); return c[3]>0.9 && lum(c)>0.6}).map(sel).slice(0,5);
  return out;
}
"""

def login(browser, user, viewport=(1440, 900), theme="light"):
    u, p = creds(user)
    ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    ctx.add_init_script(f"try{{localStorage.setItem('theme','{theme}')}}catch(e){{}}")
    page = ctx.new_page()
    page.goto(BASE + "/auth/login", wait_until="networkidle")
    banner = page.query_selector("#cookieConsent")
    if banner:
        page.click("#cookieDecline")
    page.fill("#username", u); page.fill("#password", p); page.press("#password", "Enter")
    page.wait_for_url(lambda url: "/auth/login" not in url, timeout=20000)
    page.wait_for_load_state("networkidle")
    return ctx, page
