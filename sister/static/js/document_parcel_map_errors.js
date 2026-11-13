(() => {
  'use strict';

  document.addEventListener('error', event => {
    const script = event.target;
    if (!script || !script.matches?.('script[data-document-map-script]')) return;
    const card = document.querySelector('.document-parcel-map-card');
    const status = card && card.querySelector('[data-document-map-status]');
    const message = status && status.querySelector('[data-map-status-text]');
    const retry = status && status.querySelector('[data-document-map-retry]');
    let labels = {};
    try { labels = JSON.parse(card?.dataset.documentMapLabels || '{}'); } catch {}
    if (message) message.textContent = labels.script_error || 'Non è stato possibile caricare la mappa catastale.';
    if (status) status.hidden = false;
    if (retry) {
      retry.hidden = false;
      retry.dataset.documentMapScriptRetry = 'true';
    }
  }, true);

  document.addEventListener('click', event => {
    const retry = event.target.closest?.('[data-document-map-retry]');
    if (retry?.dataset.documentMapScriptRetry === 'true') window.location.reload();
  });
})();
