(function () {
  'use strict';

  var CATEGORY_HEADERS = ['cat.', 'cat', 'categoria', 'categoria catastale', 'category'];
  var DESCRIPTION_HEADERS = ['descrizione categoria', 'category description'];
  var EMPTY_VALUES = ['', '—', '-', ':'];

  function descriptions() {
    var data = document.querySelector('[data-subject-page-json]');
    if (!data) return {};
    try {
      return JSON.parse(data.textContent || '{}').category_descriptions || {};
    } catch (error) {
      return {};
    }
  }

  function canonicalCode(value) {
    var match = String(value || '').trim().toUpperCase().match(/^([A-F])\s*[/:]?\s*0*(\d{1,2})(?!\d)/);
    return match ? match[1] + String(parseInt(match[2], 10)) : '';
  }

  function inlineDescription(value) {
    var match = String(value || '').trim().replace(/\s+/g, ' ')
      .match(/^[A-F]\s*\/?\s*\d{1,2}[A-Z]?(?:\s*[:\-–—]\s*|\s+)(.+)$/i);
    return match ? match[1].trim() : '';
  }

  function populateCategoryDescriptions() {
    var labels = descriptions();
    document.querySelectorAll('table.cat-compact-table').forEach(function (table) {
      var headers = Array.from(table.querySelectorAll('thead tr:first-child th'));
      var categoryIndex = headers.findIndex(function (header) {
        return CATEGORY_HEADERS.indexOf(header.textContent.trim().toLocaleLowerCase()) !== -1;
      });
      if (categoryIndex === -1) return;

      var descriptionIndex = headers.findIndex(function (header) {
        return DESCRIPTION_HEADERS.indexOf(header.textContent.trim().toLocaleLowerCase()) !== -1;
      });
      var insertedDescriptionColumn = descriptionIndex === -1;
      if (descriptionIndex === -1) {
        var descriptionHeader = document.createElement('th');
        descriptionHeader.scope = 'col';
        descriptionHeader.textContent = 'Descrizione categoria';
        headers[categoryIndex].after(descriptionHeader);
        descriptionIndex = categoryIndex + 1;
      }

      table.querySelectorAll('tbody tr').forEach(function (row) {
        var categoryCell = row.cells[categoryIndex];
        if (!categoryCell) return;
        var descriptionCell;
        if (insertedDescriptionColumn || !row.cells[descriptionIndex]) {
          descriptionCell = document.createElement('td');
          descriptionCell.className = 'small';
          categoryCell.after(descriptionCell);
        } else {
          descriptionCell = row.cells[descriptionIndex];
        }

        var rawCategory = categoryCell.textContent.trim();
        var code = canonicalCode(rawCategory);
        if (code && labels[code]) {
          descriptionCell.textContent = labels[code];
          return;
        }

        var current = descriptionCell.textContent.trim();
        if (EMPTY_VALUES.indexOf(current) !== -1) {
          descriptionCell.textContent = inlineDescription(rawCategory) || '—';
        }
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', populateCategoryDescriptions, { once: true });
  } else {
    populateCategoryDescriptions();
  }
})();
