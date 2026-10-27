/**
 * Subject-visura page (/documents/<id>): presentation fixes on top of the theme's server-rendered markup.
 *
 * The theme owns this markup, so what it gets wrong for SISTER is corrected here, after the page's own enhancements
 * (value columns, assessment card) have run: Italian labels, a sane heading outline, dd/mm/yyyy dates, thousands
 * separators, reduced quotas and accessible table names. Everything is idempotent and skips the "Dati completi" dump.
 */
(function () {
  'use strict';

  var ROOT = '.document-land-map-content';

  function each(selector, root, fn) {
    Array.prototype.forEach.call((root || document).querySelectorAll(selector), fn);
  }

  function textNodes(root, fn) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (!node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        var parent = node.parentElement;
        if (!parent || parent.closest('#complete-data, script, style, textarea')) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(fn);
  }

  function gcd(a, b) { return b ? gcd(b, a % b) : a; }

  // 2026/10/22 -> 22/10/2026; 500/1000 -> 1/2; "€ 2540,97" -> "€ 2.540,97"; "1 mutazioni" -> "1 mutazione"
  function fixTexts(root) {
    textNodes(root, function (node) {
      var text = node.nodeValue;
      var fixed = text
        .replace(/\b(\d{4})\/(\d{2})\/(\d{2})\b/g, '$3/$2/$1')
        .replace(/\b1 mutazioni\b/, '1 mutazione');
      if (node.parentElement.closest('.tl-owner-row')) {
        fixed = fixed.replace(/\bper (\d+)\/(\d+)\b/, function (match, n, d) {
          var divisor = gcd(Number(n), Number(d)) || 1;
          return 'per ' + (n / divisor) + '/' + (d / divisor);
        });
      }
      if (node.parentElement.closest('table.cat-compact-table td')) {
        fixed = fixed.replace(/^(\s*€ )(\d{4,})(,\d{2})\s*$/, function (match, prefix, whole, cents) {
          return prefix + whole.replace(/\B(?=(\d{3})+(?!\d))/g, '.') + cents;
        });
      }
      if (fixed !== text) node.nodeValue = fixed;
    });
  }

  function relabel(root) {
    each('th', root, function (th) {
      var text = th.textContent.trim();
      if (text === 'Coerenza') th.textContent = 'Consistenza';
      else if (text === 'Category') th.textContent = 'Cat.';
      else if (text === 'Category Description') th.textContent = 'Descrizione categoria';
    });
    // "E" means both cadastres; say so wherever the theme prints the bare code
    each('.lead, p.lead', root, function (el) {
      el.innerHTML = el.innerHTML.replace(/(\s—\s+)E(\s+—|\s*$)/, '$1Entrambi$2');
    });
    each('.card-body', root, function (card) {
      var label = card.querySelector('small.text-muted');
      var value = card.querySelector('h4');
      if (!label || !value) return;
      if (label.textContent.trim() === 'Tipo Catasto' && value.textContent.trim() === 'E') value.textContent = 'Entrambi';
      if (label.textContent.trim() === 'Gruppi Comuni') label.textContent = 'Gruppi di immobili';
    });
    // A company has no birth date or sex: the theme printed "-" for both. Show its registered office instead.
    each('.persona-header', root, function (card) {
      var labels = {};
      each('.detail-label', card, function (label) { labels[label.textContent.trim()] = label; });
      if (!labels.Nascita || !labels.Sesso) return;
      var birthValue = labels.Nascita.nextElementSibling;
      var place = birthValue && birthValue.nextElementSibling;
      var sexValue = labels.Sesso.nextElementSibling;
      if (!birthValue || !sexValue || birthValue.textContent.trim() !== '-' || sexValue.textContent.trim() !== '-') return;
      if (!place || !place.textContent.trim()) return;
      labels.Nascita.textContent = 'Sede';
      birthValue.textContent = place.textContent.trim();
      place.remove();
      labels.Sesso.hidden = true;
      sexValue.hidden = true;
    });
    each('h2 .badge', root, function (badge) {
      if (badge.textContent.trim() === 'F') badge.title = 'Catasto Fabbricati';
      if (badge.textContent.trim() === 'T') badge.title = 'Catasto Terreni';
    });
  }

  // The theme jumps h1 -> h5 -> h6 -> h4 (KPI numbers) -> h2. Fix the outline without changing the markup.
  function headings(root) {
    each('.persona-header .card-title', root, function (h) { h.setAttribute('aria-level', '2'); });
    each('[data-owner-financial-assessment] h6', document, function (h) { h.setAttribute('aria-level', '3'); });
    each('.stat-card h4, .card-body h4', root, function (h) {
      if (/^[\dE]+$|^Entrambi$/.test(h.textContent.trim())) h.setAttribute('role', 'presentation');
    });
  }

  // ~100 tables share the page: name each one by its comune and kind instead of leaving them anonymous.
  function tableNames(root) {
    each('h2', root, function (heading) {
      var comune = '';
      Array.prototype.forEach.call(heading.childNodes, function (node) {
        if (!comune && node.nodeType === 3 && node.nodeValue.trim()) comune = node.nodeValue.trim();
      });
      var group = heading.closest('details.visura-group');
      var next = group ? group.querySelector('.visura-group-body') : heading.nextElementSibling;
      while (next && (group || next.tagName !== 'H2')) {
        each('table.cat-compact-table', next, function (table) {
          if (table.hasAttribute('aria-label')) return;
          var kind = table.dataset.landMapKind === 'lands' ? 'Terreni' : 'Fabbricati';
          table.setAttribute('aria-label', kind + ' — ' + comune);
        });
        next = group ? null : next.nextElementSibling;
      }
    });
    each('.ownership-timeline', root, function (timeline) {
      if (!timeline.hasAttribute('aria-label')) timeline.setAttribute('aria-label', 'Cronologia della titolarità');
    });
  }

  function run() {
    var root = document.querySelector(ROOT) || document;
    relabel(root);
    headings(root);
    fixTexts(root);
    tableNames(root);
  }

  if (document.readyState === 'complete') run();
  else document.addEventListener('DOMContentLoaded', run);
})();
