(() => {
  'use strict';

  const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function nestedTable(level, headings = null) {
    const table = element('table', `table table-sm table-bordered nested-table nested-level-${level}`);
    if (headings) {
      const thead = element('thead', 'table-light');
      const row = element('tr');
      row.append(element('th', 'nested-index', '#'));
      headings.forEach(heading => row.append(element('th', '', heading)));
      thead.append(row);
      table.append(thead);
    }
    table.append(element('tbody'));
    return table;
  }

  function nestedValue(value, level = 0) {
    if (isRecord(value)) {
      const keys = Object.keys(value);
      if (!keys.length) return element('span', 'nested-empty', 'Empty');
      const table = nestedTable(level);
      const body = table.tBodies[0];
      keys.forEach(key => {
        const row = element('tr');
        row.append(element('th', 'nested-key', key));
        const cell = element('td', 'nested-value');
        cell.append(nestedValue(value[key], level + 1));
        row.append(cell);
        body.append(row);
      });
      const scroll = element('div', 'nested-scroll');
      scroll.append(table);
      return scroll;
    }

    if (Array.isArray(value)) {
      if (!value.length) return element('span', 'nested-empty', 'Empty');
      const allRecords = value.every(isRecord);
      const columns = [];
      if (allRecords) {
        value.forEach(item => Object.keys(item).forEach(key => {
          if (!columns.includes(key)) columns.push(key);
        }));
      }
      const table = nestedTable(level, allRecords && columns.length ? columns : null);
      const body = table.tBodies[0];
      if (allRecords && columns.length) {
        value.forEach((item, index) => {
          const row = element('tr');
          row.append(element('td', 'nested-index', String(index + 1)));
          columns.forEach(column => {
            const cell = element('td', 'nested-value');
            cell.append(nestedValue(item[column], level + 1));
            row.append(cell);
          });
          body.append(row);
        });
      } else {
        value.forEach((item, index) => {
          const row = element('tr');
          row.append(element('th', 'nested-index', String(index + 1)));
          const cell = element('td', 'nested-value');
          cell.append(nestedValue(item, level + 1));
          row.append(cell);
          body.append(row);
        });
      }
      const scroll = element('div', 'nested-scroll');
      scroll.append(table);
      return scroll;
    }

    return document.createTextNode(value === null || value === '' ? '-' : String(value));
  }

  function loadCompleteData(panel, url) {
    const content = panel.querySelector('[data-complete-data-content]');
    if (!content || panel.dataset.loaded === 'true' || panel.dataset.loading === 'true') return;
    panel.dataset.loading = 'true';
    const status = element('div', 'small text-muted', 'Caricamento dei dati completi…');
    status.setAttribute('role', 'status');
    content.replaceChildren(status);

    fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(response => {
        if (!response.ok) throw new Error('I dati completi non sono disponibili.');
        return response.json();
      })
      .then(data => {
        content.replaceChildren(nestedValue(data));
        panel.dataset.loaded = 'true';
      })
      .catch(() => {
        const error = element('div', 'alert alert-warning small mb-0', 'Caricamento non riuscito. Chiudi e riapri la sezione per riprovare.');
        error.setAttribute('role', 'alert');
        content.replaceChildren(error);
      })
      .finally(() => { delete panel.dataset.loading; });
  }

  document.querySelectorAll('[data-complete-data-url]').forEach(button => {
    const panel = document.getElementById(button.getAttribute('aria-controls'));
    if (!panel) return;
    panel.addEventListener('show.bs.collapse', () => loadCompleteData(panel, button.dataset.completeDataUrl));
  });

  document.querySelectorAll('.document-land-map-content .imm-tables table.cat-compact-table').forEach(table => {
    if (table.dataset.landMapKind) return;
    const headers = Array.from(table.querySelectorAll('thead th'), cell => cell.textContent.trim().toLocaleLowerCase());
    if (headers.some(name => ['cat.', 'cat', 'categoria', 'category'].includes(name))) {
      table.dataset.landMapKind = 'buildings';
    } else if (headers.some(name => ['quality', 'qualità'].includes(name))) {
      table.dataset.landMapKind = 'lands';
    }
  });

  function revealParcelHash() {
    if (!location.hash) return;
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { id = location.hash.slice(1); }
    const target = document.getElementById(id);
    if (!target) return;
    for (let parent = target.parentElement; parent; parent = parent.parentElement) {
      if (!parent.classList.contains('accordion-collapse')) continue;
      if (window.bootstrap?.Collapse) {
        window.bootstrap.Collapse.getOrCreateInstance(parent, { toggle: false }).show();
      } else {
        parent.classList.add('show');
      }
    }
    if (target instanceof HTMLDetailsElement) target.open = true;
    window.setTimeout(() => target.scrollIntoView({ block: 'start' }), 80);
  }

  document.addEventListener('click', event => {
    if (event.target.closest('a.subject-parcel-link')) window.setTimeout(revealParcelHash, 50);
  });
  window.addEventListener('hashchange', revealParcelHash);
  if (location.hash) window.setTimeout(revealParcelHash, 0);
})();
