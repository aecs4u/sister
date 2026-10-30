/**
 * Progressive enhancements the shared theme does not provide (docs/ui_audit_2026-10-29_demo_purchases.md):
 *  - theme toggle exposes its state (aria-pressed / label)
 *  - cookie banner behaves like the modal it looks like (role=dialog, focus moved in and trapped, restored on close)
 *  - query forms: numeric keyboards, codice fiscale / partita IVA validated before the request is sent
 *  - help text is tied to its input (aria-describedby)
 */
(function () {
  'use strict';

  /* ---- theme toggle ---------------------------------------------------------------------------------------- */
  function syncThemeToggle() {
    var btn = document.getElementById('themeToggle');
    if (!btn) return;
    var dark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
    btn.setAttribute('aria-pressed', dark ? 'true' : 'false');
    btn.setAttribute('aria-label', dark ? 'Tema scuro attivo: passa al tema chiaro' : 'Tema chiaro attivo: passa al tema scuro');
    btn.title = dark ? 'Passa al tema chiaro' : 'Passa al tema scuro';
  }

  /* ---- cookie banner --------------------------------------------------------------------------------------- */
  var previousFocus = null;

  function enhanceCookieBanner() {
    var banner = document.getElementById('cookieConsent');
    if (!banner || banner.getAttribute('data-sister-dialog')) return;
    banner.setAttribute('data-sister-dialog', '1');
    var box = banner.querySelector('.cookie-modal') || banner;
    var title = banner.querySelector('.cookie-modal-title');
    if (title && !title.id) title.id = 'cookieConsentTitle';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    if (title) box.setAttribute('aria-labelledby', title.id);
    previousFocus = document.activeElement;
    var buttons = banner.querySelectorAll('button, a[href]');
    if (buttons.length) buttons[buttons.length - 1].focus();
    banner.addEventListener('keydown', function (event) {
      if (event.key !== 'Tab' || !buttons.length) return;
      var first = buttons[0], last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    new MutationObserver(function (_m, observer) {
      if (!document.body.contains(banner)) {
        observer.disconnect();
        if (previousFocus && previousFocus.focus && document.body.contains(previousFocus)) previousFocus.focus();
      }
    }).observe(document.body, { childList: true, subtree: true });
  }

  /* ---- forms ----------------------------------------------------------------------------------------------- */
  var NUMERIC = ['foglio', 'particella', 'subalterno', 'denominatore', 'anno_denuncia', 'numero_denuncia',
                 'giorno_nascita', 'mese_nascita', 'anno_nascita', 'sezione_urbana'];

  function checkChar(first15) {
    var odd = { '0': 1, '1': 0, '2': 5, '3': 7, '4': 9, '5': 13, '6': 15, '7': 17, '8': 19, '9': 21,
      A: 1, B: 0, C: 5, D: 7, E: 9, F: 13, G: 15, H: 17, I: 19, J: 21, K: 2, L: 4, M: 18, N: 20, O: 11, P: 3, Q: 6,
      R: 8, S: 12, T: 14, U: 16, V: 10, W: 22, X: 25, Y: 24, Z: 23 };
    var total = 0;
    for (var i = 0; i < 15; i++) {
      var c = first15.charAt(i);
      total += i % 2 === 0 ? odd[c] : (/\d/.test(c) ? parseInt(c, 10) : c.charCodeAt(0) - 65);
    }
    return String.fromCharCode(65 + total % 26);
  }

  function validCodiceFiscale(code) { return /^[A-Z0-9]{16}$/.test(code) && checkChar(code.slice(0, 15)) === code.charAt(15); }

  function validPartitaIva(code) {
    if (!/^\d{11}$/.test(code)) return false;
    var total = 0;
    for (var i = 0; i < 10; i++) {
      var d = parseInt(code.charAt(i), 10);
      if (i % 2 === 1) d = d > 4 ? d * 2 - 9 : d * 2;
      total += d;
    }
    return (10 - total % 10) % 10 === parseInt(code.charAt(10), 10);
  }

  function fiscalMessage(name, value) {
    if (!value) return '';
    if (name === 'codice_fiscale') {
      return validCodiceFiscale(value) ? '' : 'Codice fiscale non valido: 16 caratteri, l’ultimo è di controllo.';
    }
    if (name === 'identificativo') {
      var cleaned = value.replace(/[\s\-\.]/g, '').toUpperCase();
      if (/^\d+$/.test(cleaned)) {
        if (cleaned.length !== 11) return 'La P.IVA deve avere esattamente 11 cifre.';
        return validPartitaIva(cleaned) ? '' : 'La P.IVA non supera il controllo della cifra finale.';
      }
      if (/^[A-Z0-9]{16}$/.test(cleaned)) {
        return validCodiceFiscale(cleaned) ? '' : 'Il codice fiscale non supera il controllo della lettera finale.';
      }
      if (value.trim().length >= 3) return ''; // company name
      return 'Inserisci una P.IVA, un codice fiscale o una denominazione di almeno 3 caratteri.';
    }
    if (/^\d{11}$/.test(value)) return validPartitaIva(value) ? '' : 'Partita IVA non valida: la cifra di controllo non torna.';
    if (/^[A-Z0-9]{16}$/.test(value)) return validCodiceFiscale(value) ? '' : 'Codice fiscale non valido: il carattere di controllo non torna.';
    return 'Inserisci una partita IVA (11 cifre) o un codice fiscale (16 caratteri).';
  }

  function showFieldError(input, message) {
    input.setCustomValidity(message);
    input.classList.toggle('is-invalid', !!message);
    var holder = input.parentElement;
    var feedback = holder.querySelector('.sister-field-feedback');
    if (!message) { if (feedback) feedback.remove(); input.removeAttribute('aria-invalid'); return; }
    if (!feedback) {
      feedback = document.createElement('div');
      feedback.className = 'invalid-feedback d-block sister-field-feedback';
      feedback.id = (input.id || input.name) + '-feedback';
      feedback.setAttribute('role', 'alert');
      input.insertAdjacentElement('afterend', feedback);
    }
    feedback.textContent = message;
    input.setAttribute('aria-invalid', 'true');
    input.setAttribute('aria-describedby', ((input.getAttribute('data-help-id') || '') + ' ' + feedback.id).trim());
  }

  function enhanceForms(root) {
    root.querySelectorAll('input[name]').forEach(function (input) {
      if (input.getAttribute('data-sister-enhanced')) return;
      input.setAttribute('data-sister-enhanced', '1');
      var name = input.name;
      if (NUMERIC.indexOf(name) !== -1 && input.type === 'text') input.setAttribute('inputmode', 'numeric');
      // help text under the field -> aria-describedby
      var help = input.parentElement && input.parentElement.querySelector('.form-text');
      if (help) {
        if (!help.id) help.id = (input.id || name) + '-help';
        input.setAttribute('data-help-id', help.id);
        if (!input.getAttribute('aria-describedby')) input.setAttribute('aria-describedby', help.id);
      }
      if (name === 'codice_fiscale' || name === 'identificativo') {
        if (name === 'codice_fiscale') {
          input.setAttribute('maxlength', '16');
          input.setAttribute('autocapitalize', 'characters');
          input.setAttribute('autocomplete', 'off');
          input.setAttribute('spellcheck', 'false');
        } else {
          // `identificativo` also accepts company names, which can be longer than a fiscal code.
          input.removeAttribute('maxlength');
          input.removeAttribute('autocapitalize');
        }
        var check = function () {
          input.value = input.value.trim();
          if (name === 'codice_fiscale' || /^[A-Z0-9]{16}$/i.test(input.value) || /^\d{11}$/.test(input.value)) {
            input.value = input.value.toUpperCase();
          }
          showFieldError(input, fiscalMessage(name, input.value));
        };
        input.addEventListener('blur', check);
        input.addEventListener('input', function () { if (input.validationMessage) showFieldError(input, ''); });
        if (input.form) input.form.addEventListener('submit', check, true);
      }
    });
  }

  /* ---- names for controls whose text is hidden or that carry only an icon ------------------------------------ */
  function nameControls(root) {
    root.querySelectorAll('a[href], button').forEach(function (el) {
      if (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.title) return;
      if ((el.innerText || '').trim()) return;
      var hidden = (el.textContent || '').trim();          // text hidden at this width (d-none d-md-inline …)
      if (hidden) { el.setAttribute('aria-label', hidden.replace(/\s+/g, ' ')); return; }
      var href = el.getAttribute('href') || '';
      if (/^https?:/i.test(href)) {
        try { el.setAttribute('aria-label', 'Apri ' + new URL(href).hostname.replace(/^www\./, '') + ' (nuova scheda)'); } catch (e) { /* ignore */ }
      }
    });
  }

  function init() {
    nameControls(document);
    syncThemeToggle();
    new MutationObserver(syncThemeToggle).observe(document.documentElement, { attributes: true, attributeFilter: ['data-bs-theme'] });
    enhanceCookieBanner();
    enhanceForms(document);
    var pending = false;
    new MutationObserver(function (mutations) {
      if (pending || !mutations.some(function (m) { return m.addedNodes.length; })) return;
      pending = true;
      setTimeout(function () { pending = false; enhanceCookieBanner(); enhanceForms(document); nameControls(document); }, 200);
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
