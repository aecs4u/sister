(function () {
  'use strict';

  const reasonLabels = {
    search_not_found: 'Ispezione non trovata',
    formality_not_found: 'Formalità non trovata',
    note_not_available_electronically: 'Nota elettronica non disponibile',
    no_electronic_title: 'Titolo telematico assente',
    xml_not_supported: 'XML non disponibile',
    tariff_stale: 'Tariffa scaduta: serve una verifica aggiornata',
  };

  function list(values) {
    return values && values.length ? values.join(', ') : 'nessuna';
  }

  function money(value) {
    const amount = Number(value);
    return Number.isFinite(amount) ? amount.toLocaleString('it-IT', { style: 'currency', currency: 'EUR' }) : 'non disponibile';
  }

  function renderQuote(target, quote) {
    const owned = (quote.already_owned || []).map(function (row) {
      return row.ordinal + (row.file_name ? ' (' + row.file_name + ')' : '');
    });
    const blocked = (quote.blocked || []).map(function (row) {
      return row.ordinal ? row.ordinal + ': ' + (reasonLabels[row.reason] || row.reason) : (reasonLabels[row.reason] || row.reason);
    });
    const cost = quote.new_cost === null ? 'non disponibile' : money(quote.new_cost);
    const estimate = quote.estimate ? 'stima' : 'importo del portale';
    const source = quote.source ? 'Fonte: ' + quote.source + ' (misurata il ' + quote.measured_on + ').' : '';
    target.textContent = [
      'Selezionate: ' + list(quote.selected),
      'Già archiviate o in attesa: ' + list(owned),
      'Bloccate: ' + list(blocked),
      'Nuovo costo (' + estimate + '): ' + cost + '.',
      source,
      'Anteprima soltanto: non è stata inviata alcuna richiesta al portale. Prima di procedere, verifica il costo su AdE.',
    ].filter(Boolean).join(' ');
    target.hidden = false;
  }

  document.addEventListener('click', async function (event) {
    const button = event.target.closest('[data-mortgage-quote]');
    if (!button) return;
    const root = button.closest('[data-mortgage-quote-root]');
    const result = root && root.querySelector('[data-mortgage-quote-result]');
    if (!root || !result) return;

    const kind = button.dataset.kind;
    const body = { search_id: root.dataset.searchId, kind: kind, format: 'PDF' };
    if (kind === 'title') {
      body.ordinal = Number(button.dataset.ordinal);
    } else {
      body.ordinals = Array.from(root.querySelectorAll('[data-mortgage-formality-select]:checked'))
        .map(function (input) { return Number(input.value); });
      if (!body.ordinals.length) {
        result.textContent = 'Seleziona almeno una formalità.';
        result.hidden = false;
        return;
      }
    }

    button.disabled = true;
    result.textContent = 'Calcolo del preventivo…';
    result.hidden = false;
    try {
      const response = await fetch('/admin/subject-formalities/quote', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(body),
      });
      const quote = await response.json();
      if (!response.ok) throw new Error(quote.detail || 'Preventivo non disponibile.');
      renderQuote(result, quote);
    } catch (error) {
      result.textContent = error.message || 'Preventivo non disponibile.';
    } finally {
      button.disabled = false;
    }
  });
})();
