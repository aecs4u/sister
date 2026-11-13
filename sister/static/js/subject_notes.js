(() => {
  'use strict';

  function setStatus(panel, message, isError) {
    const status = panel.querySelector('[data-subject-note-status]');
    status.textContent = message;
    status.classList.toggle('text-danger', !!isError);
    status.classList.toggle('text-muted', !isError);
  }

  function updateNote(panel, note) {
    panel.dataset.savedNote = note;
    const display = panel.querySelector('[data-subject-note-display]');
    const label = panel.querySelector('[data-subject-note-label]');
    const toggle = panel.querySelector('[data-subject-note-toggle]');
    const input = panel.querySelector('[data-subject-note-input]');
    const removeButton = panel.querySelector('[data-subject-note-delete]');
    display.textContent = note;
    display.hidden = !note;
    label.textContent = note ? 'Modifica nota' : 'Aggiungi nota';
    toggle.title = note ? 'Modifica la tua nota privata' : 'Aggiungi una nota privata a questo profilo';
    toggle.setAttribute('aria-label', note ? 'Modifica nota' : 'Aggiungi nota');
    input.value = note;
    removeButton.hidden = !note;
  }

  function closeEditor(panel) {
    panel.querySelector('[data-subject-note-form]').hidden = true;
    panel.querySelector('[data-subject-note-toggle]').setAttribute('aria-expanded', 'false');
    panel.querySelector('[data-subject-note-input]').value = panel.dataset.savedNote || '';
  }

  async function saveNote(panel, note) {
    const saveButton = panel.querySelector('[data-subject-note-save]');
    const cancelButton = panel.querySelector('[data-subject-note-cancel]');
    const removeButton = panel.querySelector('[data-subject-note-delete]');
    [saveButton, cancelButton, removeButton].forEach(button => { button.disabled = true; });
    setStatus(panel, '', false);
    try {
      const response = await fetch(panel.dataset.noteUrl, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({note}),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      updateNote(panel, result.note || '');
      closeEditor(panel);
      setStatus(panel, result.note ? 'Nota salvata.' : 'Nota eliminata.', false);
    } catch (error) {
      setStatus(panel, `Impossibile salvare la nota (${error.message}).`, true);
    } finally {
      [saveButton, cancelButton, removeButton].forEach(button => { button.disabled = false; });
    }
  }

  document.querySelectorAll('[data-subject-note]').forEach(panel => {
    const toggle = panel.querySelector('[data-subject-note-toggle]');
    if (!toggle || toggle.disabled) return;
    toggle.disabled = true;
    fetch(panel.dataset.noteUrl).then(response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }).then(result => {
      updateNote(panel, result.note || '');
      panel.dataset.noteLoaded = 'true';
      toggle.disabled = false;
    }).catch(error => {
      setStatus(panel, `Impossibile caricare la nota (${error.message}). Aggiorna la pagina per riprovare.`, true);
    });
  });

  document.addEventListener('click', event => {
    const toggle = event.target.closest('[data-subject-note-toggle]');
    if (toggle && !toggle.disabled) {
      const panel = toggle.closest('[data-subject-note]');
      const form = panel.querySelector('[data-subject-note-form]');
      form.hidden = !form.hidden;
      toggle.setAttribute('aria-expanded', form.hidden ? 'false' : 'true');
      setStatus(panel, '', false);
      if (!form.hidden) panel.querySelector('[data-subject-note-input]').focus();
      return;
    }

    const cancel = event.target.closest('[data-subject-note-cancel]');
    if (cancel) {
      const panel = cancel.closest('[data-subject-note]');
      closeEditor(panel);
      setStatus(panel, '', false);
      return;
    }

    const remove = event.target.closest('[data-subject-note-delete]');
    if (remove && !remove.disabled) saveNote(remove.closest('[data-subject-note]'), '');
  });

  document.addEventListener('submit', event => {
    const form = event.target.closest('[data-subject-note-form]');
    if (!form) return;
    event.preventDefault();
    const panel = form.closest('[data-subject-note]');
    saveNote(panel, panel.querySelector('[data-subject-note-input]').value);
  });
})();
