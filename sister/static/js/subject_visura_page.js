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

  document.querySelectorAll('[data-visura-group-tables] [data-cadastral-table-kind] table').forEach(table => {
    const kind = table.closest('[data-cadastral-table-kind]')?.dataset.cadastralTableKind;
    if (kind) table.dataset.landMapKind = kind;
  });

  document.querySelectorAll('[data-visura-group-tables] table').forEach(table => {
    const bar = element('div', 'd-flex gap-2 mb-2 no-print');
    const filter = element('input', 'form-control form-control-sm');
    filter.type = 'search';
    filter.placeholder = 'Filtra…';
    filter.style.maxWidth = '260px';
    filter.addEventListener('input', () => {
      const query = filter.value.toLocaleLowerCase();
      table.querySelectorAll('tbody tr').forEach(row => {
        row.style.display = !query || row.textContent.toLocaleLowerCase().includes(query) ? '' : 'none';
      });
    });
    const download = element('button', 'btn btn-sm btn-outline-secondary no-print');
    download.type = 'button';
    download.append(element('i', 'fas fa-download me-1'));
    download.append(document.createTextNode('CSV'));
    download.addEventListener('click', () => {
      const rows = Array.from(table.querySelectorAll('tr')).filter(row => row.style.display !== 'none');
      const csv = rows.map(row => Array.from(row.querySelectorAll('th,td'))
        .map(cell => `"${cell.textContent.trim().replace(/\s+/g, ' ').replace(/"/g, '""')}"`)
        .join(',')).join('\n');
      const link = element('a');
      link.href = `data:text/csv;charset=utf-8,${encodeURIComponent(csv)}`;
      link.download = `${table.id || 'table'}.csv`;
      link.click();
    });
    bar.append(filter, download);
    const scroll = table.closest('.cat-table-wrap') || table;
    scroll.before(bar);
  });

  document.querySelectorAll('.tl-node[data-ddm]').forEach(node => {
    const text = node.dataset.ddm || '';
    const match = text.match(/(\d{2}\/\d{2}\/\d{4})/) || text.match(/\b(\d{4})\b/);
    const date = node.querySelector('.tl-date-tag');
    if (match && date && !date.textContent.trim()) date.textContent = match[1];
  });
  document.querySelectorAll('.tl-header').forEach(header => {
    header.addEventListener('click', () => {
      const body = header.nextElementSibling;
      if (!body) return;
      const hidden = body.classList.toggle('d-none');
      const chevron = header.querySelector('.tl-chevron');
      if (chevron) chevron.classList.toggle('open', !hidden);
    });
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
