(() => {
  'use strict';

  function updateButton(button, favorite) {
    const icon = button.querySelector('i');
    button.classList.toggle('btn-danger', favorite);
    button.classList.toggle('btn-outline-secondary', !favorite);
    button.setAttribute('aria-pressed', favorite ? 'true' : 'false');
    const label = favorite ? 'Rimuovi dai preferiti' : 'Aggiungi ai preferiti';
    button.setAttribute('aria-label', label);
    button.title = label;
    const text = button.querySelector('[data-subject-favorite-label]');
    if (text) text.textContent = favorite ? 'Nei preferiti' : 'Aggiungi ai preferiti';
    if (icon) icon.className = `${favorite ? 'fas' : 'far'} fa-heart me-1`;
  }

  document.addEventListener('click', event => {
    const button = event.target.closest('[data-subject-favorite]');
    if (!button || button.disabled) return;
    const favorite = button.getAttribute('aria-pressed') !== 'true';
    button.disabled = true;
    fetch(button.dataset.favoriteUrl, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({favorite}),
    }).then(response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }).then(result => {
      document.querySelectorAll('[data-subject-favorite]').forEach(peer => {
        if (peer.dataset.subjectType !== button.dataset.subjectType ||
            peer.dataset.subjectCode !== button.dataset.subjectCode) return;
        updateButton(peer, result.favorite === true);
        peer.disabled = false;
      });
      if (!result.favorite) {
        const row = button.closest('[data-subject-favorite-item]');
        if (row) row.remove();
        const section = button.closest('[data-subject-favorites-section]');
        const list = section && section.querySelector('[data-subject-favorites-list]');
        if (list && !list.querySelector('[data-subject-favorite-item]')) list.hidden = true;
        if (section && !section.querySelector('[data-subject-favorite-item]')) section.hidden = true;
        const total = document.querySelectorAll('[data-favorite-item], [data-subject-favorite-item]').length;
        const count = document.querySelector('[data-favorites-count]');
        if (count) count.textContent = String(total);
        const emptyState = document.querySelector('[data-favorites-empty]');
        if (emptyState) emptyState.hidden = total !== 0;
      } else {
        const count = document.querySelector('[data-favorites-count]');
        if (count) count.textContent = String((Number(count.textContent) || 0) + 1);
      }
    }).catch(error => {
      button.disabled = false;
      window.alert(`Impossibile aggiornare i preferiti (${error.message}).`);
    });
  });
})();
