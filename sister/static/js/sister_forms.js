/**
 * SISTER form submission and result polling.
 *
 * Handles form submission → POST to API → poll for results → render response.
 */

(function () {
  'use strict';

  const POLL_INTERVAL = 3000;  // ms
  const POLL_TIMEOUT = 120000;  // ms

  document.querySelectorAll('.sister-form').forEach(form => {
    form.addEventListener('submit', handleFormSubmit);
  });

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

    // Collect form parameters
    const body = {};
    form.querySelectorAll('input[type="text"], input[type="email"], select').forEach(input => {
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

})();
