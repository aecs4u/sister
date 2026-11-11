/* Category links reveal their destination without changing document filters. */
(function () {
  'use strict';

  function revealCategory(hash) {
    if (!hash || hash.indexOf('#q-') !== 0) return;
    var section = document.getElementById(hash.slice(1));
    if (!section || !section.classList.contains('q-section')) return;
    var header = section.querySelector('.q-section-header');
    var body = section.querySelector('.collapse');

    function focusSection() {
      if (header) header.focus({ preventScroll: true });
      section.scrollIntoView({ block: 'start' });
    }

    if (body && !body.classList.contains('show') && window.bootstrap) {
      body.addEventListener('shown.bs.collapse', focusSection, { once: true });
      bootstrap.Collapse.getOrCreateInstance(body, { toggle: false }).show();
    } else {
      focusSection();
    }
  }

  document.addEventListener('click', function (event) {
    var link = event.target.closest('a[data-document-category]');
    if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey ||
        event.metaKey || event.shiftKey || event.altKey) return;
    // Clicking the current hash still opens a section collapsed since the last visit.
    if (link.hash === window.location.hash) {
      event.preventDefault();
      revealCategory(link.hash);
    }
  });
  window.addEventListener('hashchange', function () {
    revealCategory(window.location.hash);
  });
  revealCategory(window.location.hash);
})();
