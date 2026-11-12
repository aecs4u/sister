"""Read the page currently shown in the logged-in SISTER tab and store it in the sister database.

For manual runs: log in (``sister app login``), drive the portal by hand, and run this on any page you want kept.
It only READS the tab (no click, no typing, no navigation). Each capture becomes one ``request`` row of type
``manual_capture`` whose payload holds the URL, title, visible text, tables, form fields and the raw HTML.

    ../.venv/bin/python scripts/capture_page.py --dry-run     # print what would be stored
    ../.venv/bin/python scripts/capture_page.py               # store it
    ../.venv/bin/python scripts/capture_page.py --note "ispezione T 91777, elenco omonimi"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REQUEST_TYPE = "manual_capture"
CF_RE = re.compile(r"\b[A-Z]{6}\d{2}[A-EHLMPRST]\d{2}[A-Z]\d{3}[A-Z]\b")

_TABLES_JS = """() => [...document.querySelectorAll('table')].filter(t => !t.querySelector('table')).map(t =>
    [...t.rows].map(r => [...r.cells].map(c => c.innerText.trim().replace(/\\s+/g, ' '))).filter(r => r.some(c => c)))
    .filter(t => t.length)"""
_FIELDS_JS = """() => [...document.querySelectorAll('input:not([type=hidden]),select,textarea,button')].map(e => ({
    tag: e.tagName.toLowerCase(), type: e.type || null, name: e.name || null,
    value: e.tagName === 'SELECT' ? (e.options[e.selectedIndex] || {}).text || '' : (e.value || ''),
    checked: ['checkbox', 'radio'].includes(e.type) ? e.checked : null}))"""


async def read_page(endpoint: str, url_part: str) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(endpoint)
        pages = [pg for ctx in browser.contexts for pg in ctx.pages if url_part in pg.url and not pg.is_closed()]
        if not pages:
            raise SystemExit(f"nessuna scheda con '{url_part}' in {endpoint}: esegui prima `sister app login`")
        page = pages[-1]
        text = await page.inner_text("body")
        return {
            "url": page.url,
            "title": await page.title(),
            "text": text,
            "tables": await page.evaluate(_TABLES_JS),
            "form_fields": await page.evaluate(_FIELDS_JS),
            "html": await page.content(),
        }


async def store(capture: dict, note: str | None) -> str:
    from sister import database as db

    try:
        await db.init_db()
    except RuntimeError as exc:
        # a database migrated ahead of this checkout still has every table written below; any other problem is real
        if "expected" not in str(exc):
            raise
        print(f"avviso: {exc}", file=sys.stderr)
    request_id = f"{REQUEST_TYPE}_{uuid4().hex}"
    codes = sorted(set(CF_RE.findall(capture["text"].upper())))
    data = {
        "capture": {**capture, "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "note": note},
        "codici_fiscali": codes,
        "total_results": len(capture["tables"]),
        "page_visits": [{"step": "manual_capture", "url": capture["url"], "timestamp": datetime.now(timezone.utc).isoformat()}],
    }
    # no ``soggetto`` key on purpose: that would recompute the subject's holdings from a page that is not a search answer
    await db.save_request(
        request_id=request_id, request_type=REQUEST_TYPE, tipo_catasto="E", provincia="", comune="", foglio="",
        particella=codes[0] if codes else "",
    )
    await db.save_response(request_id, True, "E", data=data)
    return request_id


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", default=os.getenv("BROWSER_CDP_ENDPOINT", "http://localhost:9222"))
    parser.add_argument("--url-part", default="sister3.agenziaentrate.gov.it", help="substring of the tab URL to read")
    parser.add_argument("--note", help="free text kept with the capture")
    parser.add_argument("--dry-run", action="store_true", help="print the capture, store nothing")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    capture = await read_page(args.endpoint, args.url_part)
    text = capture["text"]
    start = max(text.find("Ti trovi in"), 0)
    print(f"{capture['url']} | {capture['title']}")
    print(text[start : start + 3000].strip())
    print(f"\n{len(capture['tables'])} tabelle, {len(capture['form_fields'])} campi, {len(capture['html'])} byte di HTML")
    if args.dry_run:
        return
    print("salvato come", await store(capture, args.note))


if __name__ == "__main__":
    asyncio.run(main())
