"""Behavior checks for the small browser-side form enhancements."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_soggetto_csv_placeholder_uses_checksum_valid_fiscal_codes():
    from sister.fiscal_code import valid_codice_fiscale

    project_root = Path(__file__).parents[1]
    script = (project_root / "sister" / "static" / "js" / "sister_forms.js").read_text()
    soggetto_line = next(line for line in script.splitlines() if "'soggetto':" in line)
    codes = re.findall(r"[A-Z0-9]{16}", soggetto_line)

    assert len(codes) == 2
    assert all(valid_codice_fiscale(code) for code in codes)


def test_query_form_csv_preview_checks_fiscal_code_control_letter():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")

    project_root = Path(__file__).parents[1]
    script_path = project_root / "sister" / "static" / "js" / "sister_forms.js"
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const textarea = {
  value: 'codice_fiscale\nRSSMRA85T10A562X\nrssmra85t10a562s',
  placeholder: '',
};
const preview = {
  classList: { add() {}, remove() {} },
  querySelector() { return null; },
  insertBefore() {},
};
const table = {};
const command = { value: 'soggetto', addEventListener() {} };
const elements = {
  'param-batch-csv_data': textarea,
  'csv-preview-batch': preview,
  'csv-table-batch': table,
  'param-batch-command': command,
};
const document = {
  head: { appendChild() {} },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  getElementById(id) { return elements[id] || null; },
  createElement() { return {}; },
};
let previewRows;
function Tabulator(_table, options) { previewRows = options.data; }
const window = { setTimeout };

vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document, window, Tabulator, URL, console, fetch() { throw new Error('unexpected fetch'); },
});
window.validateAndPreviewCSV('batch', 'csv_data');

assert.equal(previewRows.length, 2);
assert.equal(previewRows[0]._hasErrors, true);
assert.match(previewRows[0]._errors.codice_fiscale, /lettera finale/);
assert.equal(previewRows[1]._hasErrors, false);
assert.equal(previewRows[1].codice_fiscale, 'RSSMRA85T10A562S');
"""
    completed = subprocess.run([node, "-e", harness, str(script_path)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_fiscal_field_enhancement_preserves_company_names_and_checks_fiscal_codes():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")

    project_root = Path(__file__).parents[1]
    script_path = project_root / "sister" / "static" / "js" / "sister_enhance.js"
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function input(name, value) {
  const attributes = {};
  const listeners = {};
  const form = { addEventListener() {} };
  return {
    name, value, type: 'text', form, attributes, listeners,
    validationMessage: '',
    parentElement: { querySelector() { return null; } },
    classList: { toggle() {} },
    setAttribute(key, val) { attributes[key] = val; },
    getAttribute(key) { return attributes[key] || null; },
    removeAttribute(key) { delete attributes[key]; },
    setCustomValidity(message) { this.validationMessage = message; },
    addEventListener(name, callback) { listeners[name] = callback; },
    insertAdjacentElement() {},
  };
}

const company = input('identificativo', '  Acme Immobiliare Internazionale S.r.l.  ');
const invalidCf = input('codice_fiscale', 'RSSMRA85T10A562X');
const validCf = input('codice_fiscale', 'rssmra85t10a562s');
const inputs = [company, invalidCf, validCf];
const document = {
  readyState: 'complete',
  activeElement: null,
  body: { contains() { return true; } },
  documentElement: { getAttribute() { return 'light'; } },
  getElementById() { return null; },
  querySelectorAll(selector) { return selector === 'input[name]' ? inputs : []; },
  createElement() { return { setAttribute() {}, remove() {} }; },
};
class MutationObserver { observe() {} disconnect() {} }

vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document, MutationObserver, setTimeout, URL, console,
});

company.listeners.blur();
invalidCf.listeners.blur();
validCf.listeners.blur();

assert.equal(company.value, 'Acme Immobiliare Internazionale S.r.l.');
assert.equal(company.validationMessage, '');
assert.equal(company.attributes.maxlength, undefined);
assert.match(invalidCf.validationMessage, /controllo/);
assert.equal(validCf.value, 'RSSMRA85T10A562S');
assert.equal(validCf.validationMessage, '');
assert.equal(invalidCf.attributes.maxlength, '16');
"""
    subprocess.run([node, "-e", harness, str(script_path)], check=True, capture_output=True, text=True)
