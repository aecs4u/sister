/**
 * Small accessibility/UX fixes that the shared theme and generated tables do not provide.
 * (docs/ui_audit_2026-10-09.md: E3, D6)
 */
(function () {
  'use strict';

  function labelControls(root) {
    // Column headers need a scope for assistive tech.
    root.querySelectorAll('thead th:not([scope])').forEach(function (th) { th.setAttribute('scope', 'col'); });

    // Filter / search inputs with a placeholder but no accessible name.
    root.querySelectorAll('input:not([type=hidden]):not([aria-label]):not([aria-labelledby]), select:not([aria-label]):not([aria-labelledby])')
      .forEach(function (el) {
        if (el.labels && el.labels.length) return;
        if (el.title) return;
        var name = el.getAttribute('placeholder') || el.getAttribute('name') || el.id;
        if (name) el.setAttribute('aria-label', name.replace(/[._-]+/g, ' ').replace(/\.\.\.$/, '').trim());
      });

    // Data tables without a caption get one from the closest heading, so they are announced meaningfully.
    root.querySelectorAll('table:not([aria-label]):not([aria-labelledby])').forEach(function (table) {
      if (table.caption) return;
      var node = table.closest('section, .card, .q-section, .q-sub, .accordion-item, .gl-section, .guide-section') || table.parentElement;
      var heading = node && node.querySelector('h1, h2, h3, h4, h5, h6, summary, .card-header, .q-section-header, .q-sub-header');
      if (heading && heading.textContent.trim()) {
        table.setAttribute('aria-label', heading.textContent.trim().replace(/\s+/g, ' ').slice(0, 80));
      }
    });
  }

  // The hamburger reports aria-expanded="true" even when the off-canvas sidebar is closed (mobile).
  function syncSidebarToggle() {
    var btn = document.getElementById('sidebarToggle');
    var sidebar = document.getElementById('sidebar');
    if (!btn || !sidebar) return;
    var rect = sidebar.getBoundingClientRect();
    var visible = rect.width > 0 && rect.right > 1 && getComputedStyle(sidebar).visibility !== 'hidden';
    btn.setAttribute('aria-expanded', visible ? 'true' : 'false');
  }

  // Heading levels must not jump (h1 -> h3). Theme macros (stat cards, footer, legal pages) emit fixed levels, so the
  // exposed level is clamped to one below the previous heading via aria-level, leaving the visual styling untouched.
  function normalizeHeadings() {
    var previous = 0;
    document.querySelectorAll('h1, h2, h3, h4, h5, h6').forEach(function (heading) {
      var level = parseInt(heading.tagName.charAt(1), 10);
      var effective = previous && level > previous + 1 ? previous + 1 : level;
      if (effective !== level) heading.setAttribute('aria-level', String(effective));
      else if (heading.hasAttribute('aria-level')) heading.removeAttribute('aria-level');
      previous = effective;
    });
  }

  // Wide tables scroll inside their container; show edge shadows while there is more to see on that side.
  function addScrollHints() {
    document.querySelectorAll('.table-responsive').forEach(function (scroller) {
      if (scroller.parentElement && scroller.parentElement.classList.contains('scroll-hint-wrap')) return;
      var wrap = document.createElement('div');
      wrap.className = 'scroll-hint-wrap';
      scroller.parentNode.insertBefore(wrap, scroller);
      wrap.appendChild(scroller);
      function update() {
        var max = scroller.scrollWidth - scroller.clientWidth;
        wrap.classList.toggle('can-scroll-left', scroller.scrollLeft > 2);
        wrap.classList.toggle('can-scroll-right', max > 2 && scroller.scrollLeft < max - 2);
      }
      scroller.addEventListener('scroll', update, { passive: true });
      window.addEventListener('resize', update);
      update();
    });
  }

  // Native validation shows a bubble; also expose the invalid state to assistive technology and clear it once fixed.
  document.addEventListener('invalid', function (event) {
    if (event.target && event.target.setAttribute) event.target.setAttribute('aria-invalid', 'true');
  }, true);
  document.addEventListener('input', function (event) {
    var el = event.target;
    if (el && el.getAttribute && el.getAttribute('aria-invalid') === 'true' && el.checkValidity && el.checkValidity()) {
      el.removeAttribute('aria-invalid');
    }
  }, true);

  function init() {
    normalizeHeadings();
    addScrollHints();
    labelControls(document);
    syncSidebarToggle();
    // the theme restores the sidebar state on its own schedule: re-sync after it has settled
    [300, 900].forEach(function (ms) { setTimeout(syncSidebarToggle, ms); });
    var btn = document.getElementById('sidebarToggle');
    if (btn) btn.addEventListener('click', function () { setTimeout(syncSidebarToggle, 400); });
    window.addEventListener('resize', syncSidebarToggle);
    // Tabulator and lazily rendered panels add controls after load.
    var pending = false;
    new MutationObserver(function (mutations) {
      if (pending || !mutations.some(function (m) { return m.addedNodes.length; })) return;
      pending = true;
      setTimeout(function () { pending = false; labelControls(document); normalizeHeadings(); }, 250);
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
