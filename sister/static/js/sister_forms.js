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
    // Normalize: remove leading /visura/ to get the endpoint name for the proxy
    const proxyEndpoint = path.replace(/^\/visura\//, '').replace(/^\/visura$/, '');

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
      const apiPath = proxyEndpoint ? '/web/api/' + proxyEndpoint : '/web/api/';
      const res = await fetch(apiPath, {
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

  // --- CSV file loader for batch form ---
  window.loadCSVFile = function(input, groupId, paramName) {
    const file = input.files[0];
    if (!file) return;

    const nameSpan = document.getElementById('file-name-' + groupId);
    if (nameSpan) nameSpan.textContent = file.name;

    const reader = new FileReader();
    reader.onload = function(e) {
      const textarea = document.getElementById('param-' + groupId + '-' + paramName);
      if (textarea) {
        textarea.value = e.target.result;
      }
    };
    reader.readAsText(file);
  };

})();
