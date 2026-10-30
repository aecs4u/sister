"""Behavior checks for the small browser-side form enhancements."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


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
