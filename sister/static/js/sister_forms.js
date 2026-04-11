/**
 * SISTER form submission and result polling.
 *
 * Handles form submission → POST to API → poll for results → render response.
 */

(function () {
  'use strict';

  const POLL_INTERVAL = 3000;  // ms
  const POLL_TIMEOUT = 120000;  // ms

  // Available workflow flowcharts
  const WORKFLOW_PRESETS = ['due-diligence', 'patrimonio', 'fondiario', 'aziendale', 'storico'];

  // CSV placeholder templates per query type
  const BATCH_CSV_TEMPLATES = {
    'search':              'provincia,comune,foglio,particella,tipo_catasto\nRoma,ROMA,100,50,T\nTrieste,TRIESTE,9,166,F',
    'intestati':           'provincia,comune,foglio,particella,tipo_catasto,subalterno\nRoma,ROMA,100,50,F,3\nTrieste,TRIESTE,9,166,F,1',
    'soggetto':            'codice_fiscale,tipo_catasto,provincia\nRSSMRI85E28H501E,,\nBNCLRA90A41H501Z,F,Roma',
    'persona-giuridica':   'identificativo,tipo_catasto,provincia\n02471840997,,\nTIGULLIO IMMOBILIARE SRL,,Torino',
    'elenco-immobili':     'provincia,comune,tipo_catasto,foglio\nRoma,ROMA,T,100\nTrieste,TRIESTE,F,',
    'indirizzo':           'provincia,comune,indirizzo,tipo_catasto\nRoma,ROMA,VIA ROMA,T\nTerni,TERNI,DEL RIVO,F',
    'partita':             'provincia,comune,partita,tipo_catasto\nRoma,ROMA,12345,T\nBologna,BOLOGNA,67890,F',
  };

  document.querySelectorAll('.sister-form').forEach(form => {
    form.addEventListener('submit', handleFormSubmit);
  });

  // --- Batch: update CSV placeholder when query type changes ---
  const batchCommandSelect = document.getElementById('param-batch-command');
  if (batchCommandSelect) {
    batchCommandSelect.addEventListener('change', updateBatchPlaceholder);
    updateBatchPlaceholder();
  }

  function updateBatchPlaceholder() {
    const textarea = document.getElementById('param-batch-csv_data');
    if (!textarea || !batchCommandSelect) return;
    const cmd = batchCommandSelect.value;
    const template = BATCH_CSV_TEMPLATES[cmd] || BATCH_CSV_TEMPLATES['search'];
    textarea.placeholder = template;
    // Also update if textarea is empty or still has a previous template
    const currentVal = textarea.value.trim();
    const isTemplate = Object.values(BATCH_CSV_TEMPLATES).some(t => currentVal === t);
    if (!currentVal || isTemplate) {
      textarea.value = '';
      textarea.placeholder = template;
    }
  }

  // --- Workflow flowchart: show SVG when preset changes ---
  const presetSelect = document.getElementById('param-workflow-preset');
  if (presetSelect) {
    presetSelect.addEventListener('change', updateWorkflowFlowchart);
    // Load initial
    updateWorkflowFlowchart();
  }

  function updateWorkflowFlowchart() {
    const container = document.getElementById('workflow-svg-container');
    if (!container || !presetSelect) return;
    const preset = presetSelect.value;
    if (WORKFLOW_PRESETS.includes(preset)) {
      container.innerHTML = '<div class="text-center py-2"><i class="fas fa-spinner fa-spin"></i></div>';
      fetch('/static/images/workflows/' + preset + '.svg')
        .then(r => r.ok ? r.text() : '')
        .then(svg => {
          container.innerHTML = svg || '<p class="text-muted">Flowchart not available</p>';
        })
        .catch(() => { container.innerHTML = '<p class="text-muted">Flowchart not available</p>'; });
    } else {
      container.innerHTML = '<p class="text-muted">Select a preset to see its flowchart</p>';
    }
  }

  document.querySelectorAll('.btn-copy-response').forEach(btn => {
    btn.addEventListener('click', function () {
      const groupId = this.dataset.formGroup;
      const content = document.getElementById('response-content-' + groupId);
      if (content) {
        navigator.clipboard.writeText(content.textContent);
        this.innerHTML = '<i class="fas fa-check me-1"></i>Copied';
        setTimeout(() => { this.innerHTML = '<i class="fas fa-copy me-1"></i>Copy'; }, 2000);
      }
    });
  });

  async function handleFormSubmit(e) {
    e.preventDefault();
    const form = e.target;
    const groupId = form.dataset.formGroup;

    // Determine endpoint path
    const endpointRadio = form.querySelector('input[name="endpoint-' + groupId + '"]:checked');
    const endpointHidden = form.querySelector('input[type="hidden"][name="endpoint-' + groupId + '"]');
    const endpointEl = endpointRadio || endpointHidden;
    if (!endpointEl) return;

    let path = endpointEl.dataset.path || endpointEl.value;

    // Determine the API URL to post to
    let apiUrl;
    if (path.startsWith('/web/api/')) {
      // Direct proxy path (e.g. /web/api/batch) — use as-is
      apiUrl = path;
    } else {
      // Sister API path (e.g. /visura/soggetto) — route through proxy
      const proxyEndpoint = path.replace(/^\/visura\//, '').replace(/^\/visura$/, '');
      apiUrl = proxyEndpoint ? '/web/api/' + proxyEndpoint : '/web/api/';
    }

    // Collect form parameters (inputs, selects, and textareas)
    const body = {};
    form.querySelectorAll('input[type="text"], input[type="email"], select, textarea').forEach(input => {
      const name = input.name;
      if (name && !name.startsWith('endpoint-') && input.value.trim()) {
        body[name] = input.value.trim();
      }
    });

    const responseDiv = document.getElementById('response-' + groupId);
    const statusDiv = document.getElementById('response-status-' + groupId);
    const contentDiv = document.getElementById('response-content-' + groupId);

    responseDiv.style.display = 'block';
    statusDiv.innerHTML = '<div class="alert alert-info"><i class="fas fa-spinner fa-spin me-2"></i>Submitting request...</div>';
    contentDiv.textContent = '';

    try {
      // POST to proxy
      const res = await fetch(apiUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });

      const data = await res.json();

      if (!res.ok) {
        statusDiv.innerHTML = '<div class="alert alert-danger"><i class="fas fa-times-circle me-2"></i>Error: ' + (data.detail || res.statusText) + '</div>';
        contentDiv.textContent = JSON.stringify(data, null, 2);
        return;
      }

      // Check for batch response (has results array, no request_ids)
      if (data.total_rows !== undefined && data.results) {
        const submitted = data.results.filter(r => r.status === 'submitted').length;
        const errors = data.results.filter(r => r.status === 'error').length;
        const alertClass = errors > 0 ? 'alert-warning' : 'alert-success';
        const icon = errors > 0 ? 'fa-exclamation-triangle' : 'fa-check-circle';
        statusDiv.innerHTML = '<div class="alert ' + alertClass + '"><i class="fas ' + icon + ' me-2"></i>Batch: ' + submitted + ' submitted, ' + errors + ' error(s) out of ' + data.total_rows + ' rows</div>';
        contentDiv.textContent = JSON.stringify(data, null, 2);
        return;
      }

      // Check for cached results
      if (data.status === 'cached' && data.cached_results) {
        statusDiv.innerHTML = '<div class="alert alert-success"><i class="fas fa-bolt me-2"></i>Cached result returned instantly!</div>';
        contentDiv.textContent = JSON.stringify(data, null, 2);
        return;
      }

      // Queued — start polling
      const requestIds = data.request_ids || [data.request_id].filter(Boolean);
      if (!requestIds.length) {
        statusDiv.innerHTML = '<div class="alert alert-warning"><i class="fas fa-exclamation-triangle me-2"></i>No request ID returned.</div>';
        contentDiv.textContent = JSON.stringify(data, null, 2);
        return;
      }

      statusDiv.innerHTML = '<div class="alert alert-info"><i class="fas fa-spinner fa-spin me-2"></i>Queued. Polling for results... (' + requestIds.join(', ') + ')</div>';

      // Poll each request_id
      const allResults = {};
      for (const rid of requestIds) {
        const result = await pollForResult(rid, statusDiv);
        allResults[rid] = result;
      }

      // Display results
      const allCompleted = Object.values(allResults).every(r => r && r.status === 'completed');
      const alertClass = allCompleted ? 'alert-success' : 'alert-warning';
      const icon = allCompleted ? 'fa-check-circle' : 'fa-exclamation-triangle';
      statusDiv.innerHTML = '<div class="alert ' + alertClass + '"><i class="fas ' + icon + ' me-2"></i>Done. ' + requestIds.length + ' result(s).</div>';
      contentDiv.textContent = JSON.stringify(allResults, null, 2);

    } catch (err) {
      statusDiv.innerHTML = '<div class="alert alert-danger"><i class="fas fa-times-circle me-2"></i>' + err.message + '</div>';
    }
  }

  async function pollForResult(requestId, statusDiv) {
    const start = Date.now();

    while (true) {
      try {
        const res = await fetch('/web/api/visura/' + requestId);
        const data = await res.json();

        if (data.status === 'completed' || data.status === 'error' || data.status === 'expired') {
          return data;
        }

        // Still processing
        const elapsed = Math.round((Date.now() - start) / 1000);
        statusDiv.innerHTML = '<div class="alert alert-info"><i class="fas fa-spinner fa-spin me-2"></i>Processing... (' + elapsed + 's elapsed, polling ' + requestId.substring(0, 16) + '...)</div>';

        if (Date.now() - start > POLL_TIMEOUT) {
          return { status: 'timeout', message: 'Polling timeout after ' + (POLL_TIMEOUT / 1000) + 's' };
        }

        await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL));
      } catch (err) {
        return { status: 'error', error: err.message };
      }
    }
  }

  // --- CSV drop zone, file loader, validation & preview ---

  function setCSVData(groupId, paramName, text, fileName) {
    const textarea = document.getElementById('param-' + groupId + '-' + paramName);
    if (textarea) {
      textarea.value = text;
      textarea.style.opacity = '1';
      textarea.style.position = 'relative';
      textarea.style.height = 'auto';
      textarea.rows = Math.min(Math.max(text.split('\n').length + 1, 4), 15);
    }

    // Hide drop hint, show file info
    const hint = document.getElementById('dropzone-hint-' + groupId);
    if (hint) hint.classList.add('d-none');

    const info = document.getElementById('csv-file-info-' + groupId);
    if (info) info.classList.remove('d-none');

    const nameEl = document.getElementById('csv-file-name-' + groupId);
    if (nameEl) nameEl.textContent = fileName || 'pasted data';

    const lines = text.trim().split('\n').filter(l => l.trim() && !l.trim().startsWith('#'));
    const rowCount = document.getElementById('csv-row-count-' + groupId);
    if (rowCount) rowCount.textContent = (lines.length - 1) + ' data row(s)';
  }

  window.showCSVTextarea = function(groupId) {
    const textarea = document.querySelector('#dropzone-' + groupId + ' textarea');
    if (textarea && textarea.style.opacity === '0') {
      textarea.style.opacity = '1';
      textarea.style.position = 'relative';
      textarea.rows = 6;
      const hint = document.getElementById('dropzone-hint-' + groupId);
      if (hint) hint.classList.add('d-none');
    }
  };

  // Detect file type and parse accordingly
  function parseFileToCSV(file, callback) {
    const name = file.name.toLowerCase();
    const ext = name.split('.').pop();

    if (ext === 'xlsx' || ext === 'xls') {
      // XLSX — load SheetJS on demand if not already loaded
      const doParseXLSX = function() {
        const reader = new FileReader();
        reader.onload = function(e) {
          try {
            const wb = XLSX.read(e.target.result, { type: 'array' });
            const ws = wb.Sheets[wb.SheetNames[0]];
            const csv = XLSX.utils.sheet_to_csv(ws);
            callback(null, csv);
          } catch (err) {
            callback('Error parsing XLSX: ' + err.message, null);
          }
        };
        reader.readAsArrayBuffer(file);
      };

      if (typeof XLSX !== 'undefined') {
        doParseXLSX();
      } else {
        // Lazy-load SheetJS
        const script = document.createElement('script');
        script.src = 'https://cdn.sheetjs.com/xlsx-0.20.3/package/dist/xlsx.full.min.js';
        script.onload = doParseXLSX;
        script.onerror = function() { callback('Error: Could not load XLSX library.', null); };
        document.head.appendChild(script);
      }

    } else if (ext === 'json') {
      // JSON — convert array of objects to CSV
      const reader = new FileReader();
      reader.onload = function(e) {
        try {
          const data = JSON.parse(e.target.result);
          const rows = Array.isArray(data) ? data : (data.data || data.results || [data]);
          if (!rows.length || typeof rows[0] !== 'object') {
            callback('Error: JSON must be an array of objects', null);
            return;
          }
          const headers = Object.keys(rows[0]);
          const lines = [headers.join(',')];
          rows.forEach(row => {
            lines.push(headers.map(h => {
              const val = row[h];
              if (val === null || val === undefined) return '';
              const str = String(val);
              return str.includes(',') ? '"' + str.replace(/"/g, '""') + '"' : str;
            }).join(','));
          });
          callback(null, lines.join('\n'));
        } catch (err) {
          callback('Error parsing JSON: ' + err.message, null);
        }
      };
      reader.readAsText(file);

    } else {
      // CSV / TXT — read as text
      const reader = new FileReader();
      reader.onload = function(e) { callback(null, e.target.result); };
      reader.readAsText(file);
    }
  }

  window.loadDataFile = function(input, groupId, paramName) {
    const file = input.files[0];
    if (!file) return;
    parseFileToCSV(file, function(err, csv) {
      if (err) {
        alert(err);
        return;
      }
      setCSVData(groupId, paramName, csv, file.name);
    });
  };

  window.handleFileDrop = function(event, groupId, paramName) {
    event.preventDefault();
    const dropzone = document.getElementById('dropzone-' + groupId);
    if (dropzone) dropzone.classList.remove('border-primary', 'bg-light');

    // Check for file
    if (event.dataTransfer.files && event.dataTransfer.files.length > 0) {
      const file = event.dataTransfer.files[0];
      parseFileToCSV(file, function(err, csv) {
        if (err) {
          alert(err);
          return;
        }
        setCSVData(groupId, paramName, csv, file.name);
      });
      return;
    }

    // Check for pasted text
    const text = event.dataTransfer.getData('text');
    if (text) {
      setCSVData(groupId, paramName, text, null);
    }
  };

  window.clearCSVData = function(groupId, paramName) {
    const textarea = document.getElementById('param-' + groupId + '-' + paramName);
    if (textarea) {
      textarea.value = '';
      textarea.style.opacity = '0';
      textarea.style.position = 'absolute';
    }

    const hint = document.getElementById('dropzone-hint-' + groupId);
    if (hint) hint.classList.remove('d-none');

    const info = document.getElementById('csv-file-info-' + groupId);
    if (info) info.classList.add('d-none');

    const preview = document.getElementById('csv-preview-' + groupId);
    if (preview) preview.classList.add('d-none');

    // Reset file input
    const fileInput = document.getElementById('file-input-' + groupId);
    if (fileInput) fileInput.value = '';
  };

  window.validateAndPreviewCSV = function(groupId, paramName) {
    const textarea = document.getElementById('param-' + groupId + '-' + paramName);
    const previewDiv = document.getElementById('csv-preview-' + groupId);
    const tableEl = document.getElementById('csv-table-' + groupId);
    if (!textarea || !previewDiv || !tableEl) return;

    const text = textarea.value.trim();
    if (!text) {
      previewDiv.classList.add('d-none');
      alert('No CSV data to validate. Paste data or drop a file.');
      return;
    }

    // Parse CSV
    const lines = text.split('\n').filter(l => l.trim() && !l.trim().startsWith('#'));
    if (lines.length < 2) {
      previewDiv.classList.add('d-none');
      alert('CSV must have a header row and at least one data row.');
      return;
    }

    const headers = lines[0].split(',').map(h => h.trim());
    const rows = [];
    const errors = [];

    for (let i = 1; i < lines.length; i++) {
      const cells = lines[i].split(',').map(c => c.trim());
      if (cells.length !== headers.length) {
        errors.push('Row ' + i + ': expected ' + headers.length + ' columns, got ' + cells.length);
      }
      rows.push(cells);
    }

    // Build table
    let html = '<thead class="table-light"><tr><th class="text-muted">#</th>';
    headers.forEach(h => { html += '<th>' + escapeHtml(h) + '</th>'; });
    html += '<th>Status</th></tr></thead><tbody>';

    rows.forEach((cells, idx) => {
      const hasError = cells.length !== headers.length;
      const rowClass = hasError ? 'table-danger' : '';
      html += '<tr class="' + rowClass + '"><td class="text-muted">' + (idx + 1) + '</td>';
      cells.forEach(c => { html += '<td>' + escapeHtml(c || '—') + '</td>'; });
      // Pad if fewer columns
      for (let j = cells.length; j < headers.length; j++) { html += '<td class="text-danger">—</td>'; }
      html += '<td>' + (hasError ? '<span class="badge bg-danger">Error</span>' : '<span class="badge bg-success">OK</span>') + '</td>';
      html += '</tr>';
    });
    html += '</tbody>';

    tableEl.innerHTML = html;
    previewDiv.classList.remove('d-none');

    // Show summary
    const validCount = rows.length - errors.length;
    const summary = validCount + ' valid, ' + errors.length + ' error(s) — ' + rows.length + ' total row(s)';
    const alertType = errors.length > 0 ? 'warning' : 'success';
    const icon = errors.length > 0 ? 'fa-exclamation-triangle' : 'fa-check-circle';

    // Insert summary before table
    const existingSummary = previewDiv.querySelector('.csv-summary');
    if (existingSummary) existingSummary.remove();
    const summaryEl = document.createElement('div');
    summaryEl.className = 'csv-summary alert alert-' + alertType + ' py-2 mb-2';
    summaryEl.innerHTML = '<i class="fas ' + icon + ' me-2"></i>' + summary;
    previewDiv.insertBefore(summaryEl, previewDiv.firstChild);
  };

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

})();
