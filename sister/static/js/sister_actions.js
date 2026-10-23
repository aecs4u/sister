/**
 * Delegated replacement for inline event-handler attributes (onclick="..." etc.), which a strict Content-Security-Policy
 * (script-src without 'unsafe-inline') blocks. Markup declares the handler instead of embedding code:
 *
 *   <button data-click="batchWizard.goTo" data-args='[2]'>
 *   <input data-input="filterTable" data-args='["t1","t1-count","@value"]'>
 *
 * Attributes: data-click | data-change | data-input | data-dragover | data-dragleave | data-drop  = function path on window
 * Optional data-args (or data-args-<event> when an element has several handlers) = JSON array. String arguments starting with "@" are resolved against the element/event:
 *   "@this" the element, "@event" the event, "@value" element.value, "@dataset.x" element.dataset.x,
 *   "@num:dataset.x" the same as a number.
 */
(function () {
  'use strict';

  var EVENTS = ['click', 'change', 'input', 'dragover', 'dragleave', 'drop'];

  function resolve(path) {
    var parts = path.split('.');
    if (parts[0] === 'window') parts.shift();
    var owner = window;
    for (var i = 0; i < parts.length - 1; i++) {
      owner = owner && owner[parts[i]];
    }
    var fn = owner && owner[parts[parts.length - 1]];
    return typeof fn === 'function' ? { fn: fn, owner: owner } : null;
  }

  function argValue(token, el, event) {
    if (typeof token !== 'string' || token.charAt(0) !== '@') return token;
    if (token === '@this') return el;
    if (token === '@event') return event;
    if (token === '@value') return el.value;
    var numeric = token.indexOf('@num:') === 0;
    var path = (numeric ? token.slice(5) : token.slice(1)).split('.');
    var value = el;
    for (var i = 0; i < path.length && value != null; i++) value = value[path[i]];
    return numeric ? Number(value) : value;
  }

  function dispatch(event) {
    var el = event.target.closest && event.target.closest('[data-' + event.type + ']');
    if (!el) return;
    var target = resolve(el.getAttribute('data-' + event.type));
    if (!target) return;
    var args = [];
    try { args = JSON.parse(el.getAttribute('data-args-' + event.type) || el.getAttribute('data-args') || '[]'); } catch (e) { /* malformed: call without args */ }
    target.fn.apply(target.owner, args.map(function (a) { return argValue(a, el, event); }));
  }

  EVENTS.forEach(function (name) { document.addEventListener(name, dispatch); });

  // Small helpers for the handlers that used to be inline statements.
  window.sisterActions = {
    print: function () { window.print(); },
    click: function (id) { var el = document.getElementById(id); if (el) el.click(); },
    clear: function (id) { var el = document.getElementById(id); if (el) el.innerHTML = ''; },
    dropOver: function (event, el) { event.preventDefault(); el.classList.add('border-primary', 'bg-light'); },
    dropLeave: function (el) { el.classList.remove('border-primary', 'bg-light'); },
  };

  // Theme templates still ship <button onclick="window.print()">, which the CSP blocks (the button did nothing).
  // Bind the same action here until the theme uses data-click.
  document.addEventListener('click', function (event) {
    var button = event.target.closest && event.target.closest('[onclick="window.print()"]');
    if (!button) return;
    event.preventDefault();
    window.print();
  });

  // Folded <details> (e.g. the ownership history per property) hide their content from print: open them for the
  // duration of the print and put them back afterwards.
  var reopened = [];
  window.addEventListener('beforeprint', function () {
    reopened = Array.prototype.filter.call(document.querySelectorAll('details:not([open])'), function (el) {
      el.open = true;
      return true;
    });
  });
  window.addEventListener('afterprint', function () {
    reopened.forEach(function (el) { el.open = false; });
    reopened = [];
  });

  // "Dati completi" shows raw CamelCase XML names; give the words a break so they read as words.
  function spaceCamelCase(root) {
    root.querySelectorAll('.nested-key, .nested-table thead th').forEach(function (el) {
      if (el.children.length || el.dataset.spaced) return;
      el.textContent = el.textContent.replace(/([a-z0-9])([A-Z])/g, '$1 $2');
      el.dataset.spaced = 'true';
    });
  }
  document.addEventListener('DOMContentLoaded', function () {
    var dump = document.getElementById('complete-data');
    if (dump) spaceCamelCase(dump);
  });
})();
