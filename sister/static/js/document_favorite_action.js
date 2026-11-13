(() => {
  'use strict';

  document.addEventListener('click', event => {
    const button = event.target.closest('.document-favorite-toggle');
    if (!button || button.disabled) return;
    const documentId = button.dataset.favoriteId;
    const favorite = button.getAttribute('aria-pressed') !== 'true';
    button.disabled = true;
    fetch(`/documents/${encodeURIComponent(documentId)}/favorite`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({favorite}),
    }).then(response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }).then(result => {
      document.querySelectorAll('.document-favorite-toggle').forEach(peer => {
        if (peer.dataset.favoriteId !== String(documentId)) return;
        const isFavorite = result.favorite === true;
        const icon = peer.querySelector('i');
        const text = peer.querySelector('[data-favorite-label]');
        peer.classList.toggle('btn-danger', isFavorite);
        peer.classList.toggle('btn-outline-danger', !isFavorite);
        peer.setAttribute('aria-pressed', isFavorite ? 'true' : 'false');
        const label = isFavorite ? 'Rimuovi dai preferiti' : 'Aggiungi ai preferiti';
        peer.setAttribute('aria-label', label);
        peer.title = label;
        if (text) text.textContent = isFavorite ? 'Nei preferiti' : 'Aggiungi ai preferiti';
        if (icon) icon.className = `${isFavorite ? 'fas' : 'far'} fa-heart me-1`;
        peer.disabled = false;
      });
    }).catch(error => {
      button.disabled = false;
      window.alert(`Impossibile aggiornare i preferiti (${error.message}).`);
    });
  });
})();
