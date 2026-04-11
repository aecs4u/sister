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
    'persona-giuridica':   'vat_code,tipo_catasto,provincia\n02471840997,,\nTIGULLIO IMMOBILIARE SRL,,Torino',
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
        // Lazy-load SheetJS — try multiple CDNs
        const cdns = [
          'https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js',
          'https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js',
          'https://cdn.sheetjs.com/xlsx-0.20.3/package/dist/xlsx.full.min.js',
        ];
        function tryLoad(idx) {
          if (idx >= cdns.length) {
            callback('Error: Could not load XLSX library from any CDN.', null);
            return;
          }
          const script = document.createElement('script');
          script.src = cdns[idx];
          script.onload = doParseXLSX;
          script.onerror = function() { tryLoad(idx + 1); };
          document.head.appendChild(script);
        }
        tryLoad(0);
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

  // --- Field validation rules ---
  // Validator for P.IVA / VAT number: must be exactly 11 digits
  function validatePIVA(val) {
    if (!val) return { valid: false, msg: 'Required — P.IVA must be 11 digits' };
    const cleaned = val.replace(/[\s\-\.]/g, '');
    if (/^\d{11}$/.test(cleaned)) return { valid: true, cleaned: cleaned };
    if (/^\d+$/.test(cleaned) && cleaned.length !== 11) return { valid: false, msg: 'P.IVA must be exactly 11 digits (got ' + cleaned.length + ')' };
    return { valid: false, msg: 'P.IVA must be 11 digits, got: "' + val + '"' };
  }

  // Validator for CF: 16-char alphanumeric
  function validateCF(val) {
    if (!val) return { valid: false, msg: 'Required' };
    const cleaned = val.replace(/[\s\-]/g, '').toUpperCase();
    if (/^[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]$/.test(cleaned)) return { valid: true, cleaned: cleaned };
    if (/^\d{11}$/.test(cleaned)) return { valid: true, cleaned: cleaned }; // P.IVA also accepted as CF
    return { valid: false, msg: 'Invalid format (expected 16-char CF e.g. RSSMRI85E28H501E or 11-digit P.IVA)' };
  }

  // Validator for identificativo: P.IVA, CF, or company name
  function validateIdentificativo(val) {
    if (!val) return { valid: false, msg: 'Required — enter P.IVA, CF, or company name' };
    const cleaned = val.replace(/[\s\-\.]/g, '');
    if (/^\d{11}$/.test(cleaned)) return { valid: true, cleaned: cleaned };
    if (/^[A-Z0-9]{16}$/i.test(cleaned)) return { valid: true, cleaned: cleaned.toUpperCase() };
    if (val.length >= 3) return { valid: true, cleaned: val.trim() }; // company name
    return { valid: false, msg: 'Must be P.IVA (11 digits), CF (16 chars), or company name (3+ chars)' };
  }

  const FIELD_VALIDATORS = {
    'identificativo': validateIdentificativo,
    // P.IVA aliases — all validate as strict 11-digit VAT number
    'piva': validatePIVA,
    'p.iva': validatePIVA,
    'partita_iva': validatePIVA,
    'vat': validatePIVA,
    'vat_code': validatePIVA,
    'p_iva': validatePIVA,
    // CF aliases
    'codice_fiscale': validateCF,
    'cf': validateCF,
    'tax_code': validateCF,
    // Organization name — optional, just trim
    'organization': function(val) { return { valid: true, cleaned: (val || '').trim() }; },
    'organization_name': function(val) { return { valid: true, cleaned: (val || '').trim() }; },
    'company': function(val) { return { valid: true, cleaned: (val || '').trim() }; },
    'denominazione': function(val) { return { valid: true, cleaned: (val || '').trim() }; },
    'provincia': function(val) {
      if (!val) return { valid: true, cleaned: '' }; // optional in some contexts
      return { valid: true, cleaned: val.trim() };
    },
    'comune': function(val) {
      if (!val) return { valid: true, cleaned: '' };
      return { valid: true, cleaned: val.trim().toUpperCase() };
    },
    'foglio': function(val) {
      if (!val) return { valid: true, cleaned: '' };
      const cleaned = val.replace(/[\s\-]/g, '');
      if (/^\d+$/.test(cleaned)) return { valid: true, cleaned: cleaned };
      return { valid: false, msg: 'Must be numeric' };
    },
    'particella': function(val) {
      if (!val) return { valid: true, cleaned: '' };
      const cleaned = val.replace(/[\s\-]/g, '');
      if (/^\d+$/.test(cleaned)) return { valid: true, cleaned: cleaned };
      return { valid: false, msg: 'Must be numeric' };
    },
    'subalterno': function(val) {
      if (!val) return { valid: true, cleaned: '' };
      const cleaned = val.replace(/[\s\-]/g, '');
      if (/^\d+$/.test(cleaned)) return { valid: true, cleaned: cleaned };
      return { valid: false, msg: 'Must be numeric' };
    },
    'tipo_catasto': function(val) {
      if (!val) return { valid: true, cleaned: '' };
      const upper = val.trim().toUpperCase();
      if (['T', 'F', 'E', 'TF'].includes(upper)) return { valid: true, cleaned: upper };
      return { valid: false, msg: 'Must be T, F, or E' };
    },
    'indirizzo': function(val) {
      if (!val) return { valid: true, cleaned: '' };
      return { valid: true, cleaned: val.trim() };
    },
    'partita': function(val) {
      if (!val) return { valid: true, cleaned: '' };
      const cleaned = val.replace(/[\s\-]/g, '');
      if (/^\d+$/.test(cleaned)) return { valid: true, cleaned: cleaned };
      return { valid: false, msg: 'Must be numeric' };
    },
  };

  // Skip values that mean "empty"
  const SKIP_VALUES = new Set(['-', '—', 'n/a', 'na', 'null', 'none', '', 'undefined']);

  // Required fields per command (includes aliases)
  const REQUIRED_BY_COMMAND = {
    'search': ['provincia', 'comune', 'foglio', 'particella'],
    'intestati': ['provincia', 'comune', 'foglio', 'particella', 'tipo_catasto'],
    'soggetto': ['codice_fiscale', 'cf', 'tax_code'],
    'persona-giuridica': ['identificativo', 'piva', 'p.iva', 'partita_iva', 'vat', 'vat_code', 'p_iva'],
    'elenco-immobili': ['provincia', 'comune'],
    'indirizzo': ['provincia', 'comune', 'indirizzo'],
    'partita': ['provincia', 'comune', 'partita'],
    'workflow-due-diligence': ['provincia', 'comune', 'foglio', 'particella'],
    'workflow-patrimonio': ['codice_fiscale'],
    'workflow-fondiario': ['provincia', 'comune'],
    'workflow-aziendale': ['identificativo', 'vat_code'],
    'workflow-storico': ['provincia', 'comune', 'foglio', 'particella'],
  };

  function cleanCellValue(val) {
    if (!val) return '';
    let trimmed = val.trim();
    // Strip leading/trailing quotes
    if ((trimmed.startsWith('"') && trimmed.endsWith('"')) ||
        (trimmed.startsWith("'") && trimmed.endsWith("'"))) {
      trimmed = trimmed.slice(1, -1).trim();
    }
    if (SKIP_VALUES.has(trimmed.toLowerCase())) return '';
    return trimmed;
  }

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

    // Parse CSV (handles quoted fields properly)
    const lines = text.split('\n').filter(l => l.trim() && !l.trim().startsWith('#'));
    if (lines.length < 2) {
      previewDiv.classList.add('d-none');
      alert('CSV must have a header row and at least one data row.');
      return;
    }

    function parseCSVLine(line) {
      const result = [];
      let current = '';
      let inQuotes = false;
      for (let i = 0; i < line.length; i++) {
        const ch = line[i];
        if (ch === '"') {
          if (inQuotes && line[i + 1] === '"') { current += '"'; i++; } // escaped quote
          else { inQuotes = !inQuotes; }
        } else if (ch === ',' && !inQuotes) {
          result.push(current);
          current = '';
        } else {
          current += ch;
        }
      }
      result.push(current);
      return result.map(s => s.trim());
    }

    const headers = parseCSVLine(lines[0]).map(h => {
      // Strip quotes and normalize
      let cleaned = h.replace(/^["']+|["']+$/g, '').trim().toLowerCase();
      return cleaned;
    });
    const dataRows = [];
    let validCount = 0;
    let errorCount = 0;
    let skippedCount = 0;

    // Get the selected batch command for context-aware validation
    const commandSelect = document.getElementById('param-batch-command');
    const batchCommand = commandSelect ? commandSelect.value : '';

    const allRequired = REQUIRED_BY_COMMAND[batchCommand] || [];
    // Only mark as required if the column actually exists in the CSV
    const requiredFields = new Set(allRequired.filter(f => headers.includes(f)));

    for (let i = 1; i < lines.length; i++) {
      const cells = parseCSVLine(lines[i]).map(c => cleanCellValue(c));
      const rowData = {};
      const rowErrors = {};
      let allEmpty = true;

      headers.forEach((h, idx) => {
        const rawVal = cells[idx] || '';
        rowData[h] = rawVal;
        if (rawVal) allEmpty = false;

        // Check required fields
        if (requiredFields.has(h) && !rawVal) {
          rowErrors[h] = 'Required';
          return;
        }

        // Validate with field-specific validator
        const validator = FIELD_VALIDATORS[h];
        if (validator) {
          const result = validator(rawVal);
          if (!result.valid && (rawVal || requiredFields.has(h))) {
            rowErrors[h] = result.msg;
          } else if (result.cleaned !== undefined) {
            rowData[h] = result.cleaned;
          }
        }
      });

      // Skip entirely empty rows
      if (allEmpty) {
        skippedCount++;
        continue;
      }

      const hasErrors = Object.keys(rowErrors).length > 0;
      if (hasErrors) errorCount++; else validCount++;

      dataRows.push({ index: i, data: rowData, errors: rowErrors, hasErrors: hasErrors });
    }

    // Load Tabulator if not available, otherwise render directly
    if (typeof Tabulator === 'undefined') {
      // Load Tabulator CSS + JS
      if (!document.getElementById('tabulator-css')) {
        const css = document.createElement('link');
        css.id = 'tabulator-css';
        css.rel = 'stylesheet';
        css.href = 'https://unpkg.com/tabulator-tables@6.3.1/dist/css/tabulator_bootstrap5.min.css';
        document.head.appendChild(css);
      }
      const script = document.createElement('script');
      script.src = 'https://unpkg.com/tabulator-tables@6.3.1/dist/js/tabulator.min.js';
      script.onload = function() { renderTabulatorPreview(groupId, headers, dataRows, validCount, errorCount, skippedCount); };
      script.onerror = function() { renderFallbackTable(groupId, tableEl, headers, dataRows, validCount, errorCount, skippedCount); };
      document.head.appendChild(script);
    } else {
      renderTabulatorPreview(groupId, headers, dataRows, validCount, errorCount, skippedCount);
    }

    previewDiv.classList.remove('d-none');
  };

  function renderTabulatorPreview(groupId, headers, dataRows, validCount, errorCount, skippedCount) {
    const previewDiv = document.getElementById('csv-preview-' + groupId);
    const tableEl = document.getElementById('csv-table-' + groupId);

    // Build Tabulator columns
    const columns = [
      { title: '#', field: '_row', width: 50, hozAlign: 'right', headerSort: false,
        formatter: function(cell) { return cell.getValue(); } },
    ];

    headers.forEach(h => {
      columns.push({
        title: toTitleCase(h),
        field: h,
        editor: 'input',
        headerFilter: 'input',
        formatter: function(cell) {
          const row = cell.getRow().getData();
          const errors = row._errors || {};
          const val = cell.getValue() || '';
          if (errors[h]) {
            return '<span class="text-danger" title="' + escapeHtml(errors[h]) + '"><i class="fas fa-exclamation-circle me-1"></i>' + escapeHtml(val || '—') + '</span>';
          }
          return escapeHtml(val || '');
        },
      });
    });

    columns.push({
      title: 'Status', field: '_status', width: 90, hozAlign: 'center', headerSort: false,
      formatter: function(cell) {
        const hasErrors = cell.getRow().getData()._hasErrors;
        return hasErrors
          ? '<span class="badge bg-danger">Error</span>'
          : '<span class="badge bg-success">OK</span>';
      },
    });

    // Build row data
    const tabulatorData = dataRows.map(r => ({
      _row: r.index,
      ...r.data,
      _errors: r.errors,
      _hasErrors: r.hasErrors,
      _status: r.hasErrors ? 'error' : 'ok',
    }));

    // Clear and render
    tableEl.innerHTML = '';
    tableEl.className = '';

    new Tabulator(tableEl, {
      data: tabulatorData,
      columns: columns,
      layout: 'fitDataFill',
      height: Math.min(400, 50 + dataRows.length * 38),
      pagination: dataRows.length > 50,
      paginationSize: 50,
      rowFormatter: function(row) {
        if (row.getData()._hasErrors) {
          row.getElement().style.backgroundColor = '#fff5f5';
        }
      },
    });

    // Summary
    renderSummary(previewDiv, validCount, errorCount, skippedCount, dataRows.length);
  }

  function renderFallbackTable(groupId, tableEl, headers, dataRows, validCount, errorCount, skippedCount) {
    // Fallback if Tabulator fails to load
    const previewDiv = document.getElementById('csv-preview-' + groupId);
    let html = '<thead class="table-light"><tr><th>#</th>';
    headers.forEach(h => { html += '<th>' + escapeHtml(toTitleCase(h)) + '</th>'; });
    html += '<th>Status</th></tr></thead><tbody>';

    dataRows.forEach(r => {
      const cls = r.hasErrors ? 'table-danger' : '';
      html += '<tr class="' + cls + '"><td class="text-muted">' + r.index + '</td>';
      headers.forEach(h => {
        const val = r.data[h] || '';
        const err = r.errors[h];
        if (err) {
          html += '<td class="text-danger" title="' + escapeHtml(err) + '"><i class="fas fa-exclamation-circle me-1"></i>' + escapeHtml(val || '—') + '</td>';
        } else {
          html += '<td>' + escapeHtml(val || '') + '</td>';
        }
      });
      html += '<td>' + (r.hasErrors ? '<span class="badge bg-danger">Error</span>' : '<span class="badge bg-success">OK</span>') + '</td>';
      html += '</tr>';
    });
    html += '</tbody>';

    tableEl.className = 'table table-sm table-bordered';
    tableEl.innerHTML = html;

    renderSummary(previewDiv, validCount, errorCount, skippedCount, dataRows.length);
  }

  function renderSummary(container, validCount, errorCount, skippedCount, totalRows) {
    const existing = container.querySelector('.csv-summary');
    if (existing) existing.remove();

    const parts = [validCount + ' valid'];
    if (errorCount > 0) parts.push(errorCount + ' error(s)');
    if (skippedCount > 0) parts.push(skippedCount + ' skipped');
    parts.push(totalRows + ' total');

    const alertType = errorCount > 0 ? 'warning' : 'success';
    const icon = errorCount > 0 ? 'fa-exclamation-triangle' : 'fa-check-circle';

    const el = document.createElement('div');
    el.className = 'csv-summary alert alert-' + alertType + ' py-2 mb-2';
    el.innerHTML = '<i class="fas ' + icon + ' me-2"></i>' + parts.join(' &middot; ');
    container.insertBefore(el, container.firstChild);
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function toTitleCase(str) {
    return str.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }

  // =========================================================================
  // Batch Wizard — multi-step import → map → validate → confirm → submit
  // =========================================================================

  const GROUP_ID = 'batch';
  const PARAM_NAME = 'csv_data';

  // Expected API fields per command
  const API_FIELDS = {
    'search': ['provincia', 'comune', 'foglio', 'particella', 'tipo_catasto', 'sezione', 'subalterno'],
    'intestati': ['provincia', 'comune', 'foglio', 'particella', 'tipo_catasto', 'subalterno', 'sezione'],
    'soggetto': ['codice_fiscale', 'tipo_catasto', 'provincia'],
    'persona-giuridica': ['identificativo', 'tipo_catasto', 'provincia'],
    'elenco-immobili': ['provincia', 'comune', 'tipo_catasto', 'foglio', 'sezione'],
    'indirizzo': ['provincia', 'comune', 'indirizzo', 'tipo_catasto'],
    'partita': ['provincia', 'comune', 'partita', 'tipo_catasto'],
    'workflow-due-diligence': ['provincia', 'comune', 'foglio', 'particella', 'tipo_catasto'],
    'workflow-patrimonio': ['codice_fiscale', 'tipo_catasto'],
    'workflow-fondiario': ['provincia', 'comune', 'foglio', 'tipo_catasto'],
    'workflow-aziendale': ['identificativo', 'tipo_catasto'],
    'workflow-storico': ['provincia', 'comune', 'foglio', 'particella', 'tipo_catasto'],
  };

  // Auto-mapping suggestions: CSV column → API field
  const AUTO_MAP = {
    'provincia': 'provincia', 'province': 'provincia',
    'comune': 'comune', 'municipality': 'comune', 'city': 'comune',
    'foglio': 'foglio', 'sheet': 'foglio',
    'particella': 'particella', 'parcel': 'particella',
    'subalterno': 'subalterno', 'sub': 'subalterno',
    'sezione': 'sezione', 'section': 'sezione',
    'tipo_catasto': 'tipo_catasto', 'type': 'tipo_catasto', 'catasto': 'tipo_catasto',
    'codice_fiscale': 'codice_fiscale', 'cf': 'codice_fiscale', 'tax_code': 'codice_fiscale',
    'identificativo': 'identificativo',
    'piva': 'identificativo', 'p.iva': 'identificativo', 'p_iva': 'identificativo',
    'vat': 'identificativo', 'vat_code': 'identificativo', 'partita_iva': 'identificativo',
    'organization': 'identificativo', 'organization_name': 'identificativo',
    'company': 'identificativo', 'denominazione': 'identificativo',
    'indirizzo': 'indirizzo', 'address': 'indirizzo', 'via': 'indirizzo',
    'partita': 'partita',
  };

  window.batchWizard = {
    _rawHeaders: [],
    _rawRows: [],
    _mapping: {},      // csvCol → apiField
    _validatedRows: [],
    _currentStep: 1,

    onDataLoaded: function() {
      // Called after file drop/load — parse raw data and enable Next
      setTimeout(function() {
        const textarea = document.getElementById('param-' + GROUP_ID + '-' + PARAM_NAME);
        if (!textarea || !textarea.value.trim()) return;

        const lines = textarea.value.trim().split('\n').filter(l => l.trim() && !l.trim().startsWith('#'));
        if (lines.length < 2) return;

        batchWizard._rawHeaders = parseCSVLine(lines[0]).map(h => h.replace(/^["']+|["']+$/g, '').trim());
        batchWizard._rawRows = [];
        for (let i = 1; i < lines.length; i++) {
          const cells = parseCSVLine(lines[i]).map(c => cleanCellValue(c));
          if (cells.some(c => c)) batchWizard._rawRows.push(cells);
        }

        const btn = document.getElementById('batch-btn-to-step2');
        if (btn) btn.disabled = false;

        // Update file info
        const rowCount = document.getElementById('csv-row-count-' + GROUP_ID);
        if (rowCount) rowCount.textContent = batchWizard._rawRows.length + ' data row(s), ' + batchWizard._rawHeaders.length + ' columns';
      }, 200);
    },

    goTo: function(step) {
      // Hide all steps
      document.querySelectorAll('.batch-step').forEach(s => s.classList.add('d-none'));
      // Show target
      const target = document.getElementById('batch-step-' + step);
      if (target) target.classList.remove('d-none');
      // Update badges
      for (let i = 1; i <= 5; i++) {
        const badge = document.getElementById('batch-step-badge-' + i);
        if (badge) badge.className = 'badge ' + (i === step ? 'bg-primary' : i < step ? 'bg-success' : 'bg-secondary');
      }
      batchWizard._currentStep = step;

      // Step-specific init
      if (step === 2) batchWizard._buildMappingUI();
      if (step === 3) batchWizard._runValidation();
      if (step === 4) batchWizard._buildConfirmation();
    },

    reset: function() {
      clearCSVData(GROUP_ID, PARAM_NAME);
      batchWizard._rawHeaders = [];
      batchWizard._rawRows = [];
      batchWizard._mapping = {};
      batchWizard._validatedRows = [];
      const btn = document.getElementById('batch-btn-to-step2');
      if (btn) btn.disabled = true;
      batchWizard.goTo(1);
    },

    _getCommand: function() {
      const sel = document.getElementById('param-batch-command');
      return sel ? sel.value : 'search';
    },

    _buildMappingUI: function() {
      const cmd = batchWizard._getCommand();
      const apiFields = API_FIELDS[cmd] || [];
      const tbody = document.getElementById('batch-mapping-rows');
      if (!tbody) return;

      let html = '';
      batchWizard._rawHeaders.forEach((col, idx) => {
        // Sample values (first 3 non-empty)
        const samples = batchWizard._rawRows.slice(0, 5).map(r => r[idx]).filter(Boolean).slice(0, 3);
        const sampleText = samples.length ? samples.join(', ') : '<em class="text-muted">empty</em>';

        // Auto-suggest mapping
        const autoField = AUTO_MAP[col.toLowerCase()] || '';

        html += '<tr>';
        html += '<td><code>' + escapeHtml(col) + '</code></td>';
        html += '<td class="small text-muted">' + sampleText + '</td>';
        html += '<td class="text-center"><i class="fas fa-arrow-right text-muted"></i></td>';
        html += '<td><select class="form-select form-select-sm batch-map-select" data-csv-col="' + escapeHtml(col) + '">';
        html += '<option value="">(skip this column)</option>';
        apiFields.forEach(f => {
          const selected = (f === autoField) ? ' selected' : '';
          html += '<option value="' + f + '"' + selected + '>' + toTitleCase(f) + '</option>';
        });
        html += '</select></td>';
        html += '</tr>';
      });
      tbody.innerHTML = html;
    },

    _readMapping: function() {
      batchWizard._mapping = {};
      document.querySelectorAll('.batch-map-select').forEach(sel => {
        const csvCol = sel.dataset.csvCol;
        const apiField = sel.value;
        if (csvCol && apiField) {
          batchWizard._mapping[csvCol] = apiField;
        }
      });
    },

    _runValidation: function() {
      batchWizard._readMapping();
      const cmd = batchWizard._getCommand();
      const requiredSet = new Set(REQUIRED_BY_COMMAND[cmd] || REQUIRED_BY_COMMAND[cmd.replace('workflow-', '')] || []);

      // Map raw data through column mapping
      batchWizard._validatedRows = [];
      let validCount = 0, errorCount = 0;

      batchWizard._rawRows.forEach((cells, rowIdx) => {
        const mapped = {};
        const errors = {};
        let allEmpty = true;

        batchWizard._rawHeaders.forEach((col, colIdx) => {
          const apiField = batchWizard._mapping[col];
          if (!apiField) return; // skipped column
          const val = cells[colIdx] || '';
          mapped[apiField] = val;
          if (val) allEmpty = false;

          // Required check
          if (requiredSet.has(apiField) && !val) {
            errors[apiField] = 'Required';
            return;
          }

          // Field validator
          const validator = FIELD_VALIDATORS[apiField];
          if (validator && (val || requiredSet.has(apiField))) {
            const result = validator(val);
            if (!result.valid) {
              errors[apiField] = result.msg;
            } else if (result.cleaned !== undefined) {
              mapped[apiField] = result.cleaned;
            }
          }
        });

        if (allEmpty) return; // skip empty rows

        const hasErrors = Object.keys(errors).length > 0;
        if (hasErrors) errorCount++; else validCount++;
        batchWizard._validatedRows.push({
          index: rowIdx + 1,
          data: mapped,
          errors: errors,
          hasErrors: hasErrors,
          skip: false,
        });
      });

      // Show summary
      const summaryDiv = document.getElementById('batch-validation-summary');
      if (summaryDiv) {
        const cls = errorCount > 0 ? 'warning' : 'success';
        const icon = errorCount > 0 ? 'fa-exclamation-triangle' : 'fa-check-circle';
        summaryDiv.innerHTML = '<div class="alert alert-' + cls + ' py-2"><i class="fas ' + icon + ' me-2"></i>' +
          validCount + ' valid &middot; ' + errorCount + ' error(s) &middot; ' +
          batchWizard._validatedRows.length + ' total rows</div>';
      }

      // Render Tabulator with mapped fields
      const mappedFields = [...new Set(Object.values(batchWizard._mapping))];
      batchWizard._renderValidationTable(mappedFields);
    },

    _renderValidationTable: function(fields) {
      const tableEl = document.getElementById('csv-table-' + GROUP_ID);
      if (!tableEl) return;

      function doRender() {
        const columns = [
          { title: '#', field: '_row', width: 50, hozAlign: 'right', headerSort: false },
        ];
        fields.forEach(f => {
          columns.push({
            title: toTitleCase(f), field: f, editor: 'input', headerFilter: 'input',
            formatter: function(cell) {
              const errs = cell.getRow().getData()._errors || {};
              const val = cell.getValue() || '';
              if (errs[f]) return '<span class="text-danger" title="' + escapeHtml(errs[f]) + '"><i class="fas fa-exclamation-circle me-1"></i>' + escapeHtml(val || '—') + '</span>';
              return escapeHtml(val);
            },
          });
        });
        columns.push({
          title: 'Status', field: '_status', width: 90, hozAlign: 'center', headerSort: false,
          formatter: function(cell) {
            return cell.getRow().getData()._hasErrors
              ? '<span class="badge bg-danger">Error</span>'
              : '<span class="badge bg-success">OK</span>';
          },
        });

        const data = batchWizard._validatedRows.map(r => ({
          _row: r.index, ...r.data, _errors: r.errors, _hasErrors: r.hasErrors,
        }));

        tableEl.innerHTML = '';
        tableEl.className = '';
        new Tabulator(tableEl, {
          data: data, columns: columns, layout: 'fitDataFill',
          height: Math.min(350, 50 + data.length * 38),
          pagination: data.length > 50, paginationSize: 50,
          rowFormatter: function(row) {
            if (row.getData()._hasErrors) row.getElement().style.backgroundColor = '#fff5f5';
          },
        });
      }

      if (typeof Tabulator === 'undefined') {
        if (!document.getElementById('tabulator-css')) {
          const css = document.createElement('link'); css.id = 'tabulator-css'; css.rel = 'stylesheet';
          css.href = 'https://unpkg.com/tabulator-tables@6.3.1/dist/css/tabulator_bootstrap5.min.css';
          document.head.appendChild(css);
        }
        const s = document.createElement('script');
        s.src = 'https://unpkg.com/tabulator-tables@6.3.1/dist/js/tabulator.min.js';
        s.onload = doRender; document.head.appendChild(s);
      } else { doRender(); }
    },

    _buildConfirmation: function() {
      const rows = batchWizard._validatedRows;
      const valid = rows.filter(r => !r.hasErrors).length;
      const errors = rows.filter(r => r.hasErrors).length;
      const cmd = batchWizard._getCommand();

      const div = document.getElementById('batch-confirm-summary');
      if (div) {
        div.innerHTML =
          '<p class="mb-2"><strong>Query type:</strong> ' + escapeHtml(toTitleCase(cmd.replace(/-/g, ' '))) + '</p>' +
          '<p class="mb-2"><strong>Total rows:</strong> ' + rows.length + '</p>' +
          '<p class="mb-2"><span class="badge bg-success">' + valid + ' valid</span> ' +
          (errors > 0 ? '<span class="badge bg-danger">' + errors + ' errors</span>' : '') + '</p>';
      }

      const skipCheck = document.getElementById('batch-skip-errors');
      const submitCount = skipCheck && skipCheck.checked ? valid : rows.length;
      const countSpan = document.getElementById('batch-submit-count');
      if (countSpan) countSpan.textContent = '(' + submitCount + ')';

      // Update count when checkbox changes
      if (skipCheck) {
        skipCheck.onchange = function() {
          const n = this.checked ? valid : rows.length;
          if (countSpan) countSpan.textContent = '(' + n + ')';
        };
      }
    },

    submit: function() {
      const skipErrors = document.getElementById('batch-skip-errors');
      const rows = batchWizard._validatedRows.filter(r => !(skipErrors && skipErrors.checked && r.hasErrors));

      if (!rows.length) {
        alert('No rows to submit.');
        return;
      }

      // Build CSV from mapped data for the batch API
      const fields = [...new Set(Object.values(batchWizard._mapping))];
      const csvLines = [fields.join(',')];
      rows.forEach(r => {
        csvLines.push(fields.map(f => {
          const val = r.data[f] || '';
          return val.includes(',') ? '"' + val + '"' : val;
        }).join(','));
      });

      // Put into hidden textarea and submit the form
      const textarea = document.getElementById('param-' + GROUP_ID + '-' + PARAM_NAME);
      if (textarea) textarea.value = csvLines.join('\n');

      batchWizard.goTo(5);

      // Submit via the existing form handler
      const form = document.getElementById('form-' + GROUP_ID);
      if (form) {
        form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      }
    },
  };

  // Override loadDataFile callback to also trigger wizard
  const _origLoadDataFile = window.loadDataFile;
  window.loadDataFile = function(input, groupId, paramName) {
    _origLoadDataFile(input, groupId, paramName);
    if (groupId === GROUP_ID) {
      setTimeout(function() { batchWizard.onDataLoaded(); }, 300);
    }
  };

})();
