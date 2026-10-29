// Per-user OMI zone and typology selectors in subject cadastral tables.
(function () {
    'use strict';

    var estimatesSource = document.querySelector('[data-land-map-omi-estimates]');
    var optionsSource = document.querySelector('[data-land-map-omi-options]');
    if (!estimatesSource || !optionsSource) return;

    function read(source, fallback) {
        try { return JSON.parse(source.textContent || 'null') || fallback; } catch (error) { return fallback; }
    }
    var estimates = read(estimatesSource, []);
    var optionsByComune = read(optionsSource, {});
    var editable = optionsSource.dataset.editable !== 'false';
    if (!Array.isArray(estimates)) return;

    var accordionStateKey = 'omi-selection-accordion:' + window.location.pathname + window.location.search;

    function rememberAccordionState() {
        try {
            var openGroups = Array.from(document.querySelectorAll('details.visura-group')).map(function (group) {
                return group.open;
            });
            sessionStorage.setItem(accordionStateKey, JSON.stringify({open: openGroups, scrollY: window.scrollY}));
        } catch (error) {
            // Saving the OMI choice should still work when browser storage is unavailable.
        }
    }

    function restoreAccordionState() {
        try {
            var stored = sessionStorage.getItem(accordionStateKey);
            if (!stored) return;
            sessionStorage.removeItem(accordionStateKey);
            var state = JSON.parse(stored);
            if (!state || !Array.isArray(state.open)) return;
            Array.from(document.querySelectorAll('details.visura-group')).forEach(function (group, index) {
                if (index < state.open.length) group.open = Boolean(state.open[index]);
            });
            if (Number.isFinite(Number(state.scrollY))) {
                window.requestAnimationFrame(function () { window.scrollTo(0, Number(state.scrollY)); });
            }
        } catch (error) {
            // Ignore stale or unavailable browser storage and keep the page usable.
        }
    }

    restoreAccordionState();

    function text(parent, tag, className, value) {
        var element = document.createElement(tag);
        if (className) element.className = className;
        if (value !== undefined && value !== null) element.textContent = String(value);
        parent.appendChild(element);
        return element;
    }

    function option(select, value, label) {
        var item = text(select, 'option', '', label);
        item.value = value;
        return item;
    }

    function addSelectCell() {
        var body = document.createElement('td');
        body.className = 'omi-selection-cell';
        return {body: body};
    }

    function insertCells(row, cells, target) {
        if (target) cells.forEach(function (cell) { row.insertBefore(cell, target); });
        else row.append.apply(row, cells);
    }

    function cleanCadastralPart(value) {
        var part = String(value == null ? '' : value).trim().replace(/\s+/g, '').toLocaleUpperCase();
        return part === '—' || part === '-' ? '' : part;
    }

    function cadastralRowKey(parts) {
        var normalized = parts.map(cleanCadastralPart);
        return normalized[0] && normalized[1] ? normalized.join('|') : '';
    }

    function estimatesForRows(table, rows, entries) {
        if (rows.length === entries.length) {
            return rows.map(function (_row, index) { return entries[index] || {}; });
        }

        var header = table.querySelector('thead tr');
        var headings = header ? Array.from(header.cells).map(function (cell) {
            return cell.textContent.trim().toLocaleLowerCase();
        }) : [];
        function columnIndex(names, fallback) {
            var index = headings.findIndex(function (heading) { return names.indexOf(heading) !== -1; });
            return index >= 0 ? index : fallback;
        }
        var indices = [
            columnIndex(['fog.', 'foglio', 'sheet'], 2),
            columnIndex(['part.', 'particella', 'parcel'], 3),
            columnIndex(['sub.', 'subalterno', 'subunit'], 4)
        ];
        var byKey = new Map();
        entries.forEach(function (estimate) {
            var identity = estimate && estimate.asset_identity || {};
            var key = cadastralRowKey([identity.sheet, identity.parcel, identity.subunit]);
            if (!key) return;
            if (!byKey.has(key)) byKey.set(key, []);
            byKey.get(key).push(estimate);
        });

        return rows.map(function (row) {
            var key = cadastralRowKey(indices.map(function (index) {
                return row.cells[index] ? row.cells[index].textContent : '';
            }));
            var matches = key ? byKey.get(key) : null;
            return matches && matches.length ? matches.shift() : {};
        });
    }

    function ensureOmiEstimateColumn(table, rows, rowEstimates) {
        var header = table.querySelector('thead tr');
        if (!header) return;
        var targetIndex = Array.prototype.findIndex.call(header.cells, function (item) {
            return item.textContent.trim() === 'Stima OMI indicativa';
        });
        if (targetIndex >= 0) return;

        var cadastralIndex = Array.prototype.findIndex.call(header.cells, function (item) {
            return item.textContent.trim() === 'Valore catastale';
        });
        var headerTarget = cadastralIndex >= 0 ? header.cells[cadastralIndex + 1] || null : null;
        var estimateHead = document.createElement('th');
        estimateHead.className = 'text-nowrap';
        estimateHead.scope = 'col';
        estimateHead.textContent = 'Stima OMI indicativa';
        header.insertBefore(estimateHead, headerTarget);

        rows.forEach(function (row, index) {
            var estimate = rowEstimates[index] || {};
            var cell = document.createElement('td');
            var value = estimate.range || estimate.missing || 'Stima OMI non disponibile';
            cell.className = estimate.range ? 'text-end text-nowrap' : 'text-muted small';
            cell.textContent = value;
            cell.title = estimate.note || estimate.missing || 'Stima OMI non disponibile.';
            var rowTarget = cadastralIndex >= 0 ? row.cells[cadastralIndex + 1] || null : null;
            row.insertBefore(cell, rowTarget);
        });
    }

    function saveActualSize(estimate, input, cell) {
        var value = input.value.trim();
        if (value === input.dataset.savedValue) return;
        input.disabled = true;
        cell.title = '';
        fetch('/manual-property-valuations/' + encodeURIComponent(estimate.asset_identity.asset_key) + '/actual-size', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({actual_size_sqm: value || null})
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (result) {
                if (!response.ok) throw new Error(result.detail || 'Salvataggio della superficie non riuscito.');
                rememberAccordionState();
                window.location.reload();
            });
        }).catch(function (error) {
            input.disabled = false;
            cell.title = error.message;
            var message = cell.querySelector('[data-actual-size-error]');
            if (!message) {
                message = text(cell, 'small', 'text-danger d-block', '');
                message.dataset.actualSizeError = 'true';
            }
            message.textContent = error.message;
        });
    }

    function addActualSizeCell(estimate) {
        var body = document.createElement('td');
        body.className = 'actual-size-cell';
        var currentValue = estimate.actual_size_sqm == null
            ? (estimate.actual_size_default_sqm == null ? '' : String(estimate.actual_size_default_sqm))
            : String(estimate.actual_size_sqm);
        if (!editable) {
            text(body, 'span', '', currentValue || '—');
            body.title = 'Accedi per modificare la superficie effettiva.';
            return body;
        }
        if (!estimate.asset_identity || !estimate.asset_identity.asset_key) {
            text(body, 'span', 'text-muted', currentValue || '—');
            body.title = 'Superficie effettiva non modificabile per questa riga.';
            return body;
        }
        var input = text(body, 'input', 'form-control form-control-sm', '');
        input.type = 'text';
        input.inputMode = 'decimal';
        input.autocomplete = 'off';
        input.setAttribute('aria-label', 'Actual size in square metres');
        input.style.width = '8rem';
        input.placeholder = 'm²';
        input.value = estimate.actual_size_sqm == null ? '' : String(estimate.actual_size_sqm);
        input.dataset.savedValue = input.value;
        var defaultValue = estimate.actual_size_default_sqm;
        var defaultSource = estimate.actual_size_default_source || '';
        input.title = estimate.actual_size_is_override
            ? 'Valore personalizzato. Il valore predefinito (' + (defaultValue == null ? 'non disponibile' : defaultValue + ' m²') +
                ')' + (defaultSource ? ' deriva da ' + defaultSource : '') + '. Svuota il campo per ripristinare il default.'
            : defaultValue == null
                ? 'Superficie catastale non disponibile. Inserisci una dimensione per aggiornare la stima OMI.'
                : 'Precompilato da ' + (defaultSource || 'dati catastali') +
                    '. Modifica il valore per aggiornare la stima OMI; svuota il campo se vuoi lasciare il default automatico.';
        input.addEventListener('change', function () { saveActualSize(estimate, input, body); });
        return body;
    }

    function addOmiPriceRangeCell(estimate) {
        var body = document.createElement('td');
        body.className = 'omi-price-range-cell text-end text-nowrap';
        var rawLow = estimate.omi_price_min;
        var rawHigh = estimate.omi_price_max;
        if (rawLow == null || rawHigh == null || rawLow === '' || rawHigh === '') {
            text(body, 'span', 'text-muted', '—');
            body.title = estimate.note || 'Quotazione OMI per m² non disponibile.';
            return body;
        }

        var low = Number(rawLow);
        var high = Number(rawHigh);
        if (!Number.isFinite(low) || !Number.isFinite(high)) {
            text(body, 'span', 'text-muted', '—');
            body.title = estimate.note || 'Quotazione OMI per m² non disponibile.';
            return body;
        }

        var formatPrice = new Intl.NumberFormat('it-IT', {maximumFractionDigits: 0});
        var range = low === high
            ? '€ ' + formatPrice.format(low)
            : '€ ' + formatPrice.format(low) + ' – € ' + formatPrice.format(high);
        var comune = String(estimate.omi_comune || '').trim();
        var zona = String(estimate.omi_zona || '').trim();
        var tipologia = String(estimate.omi_tipologia || estimate.omi_tipologia_default || '').trim();

        if (!comune) {
            text(body, 'span', '', range);
            return body;
        }

        var params = new URLSearchParams({comune: comune});
        if (zona) params.set('zona', zona);
        var href = '/omi/valuation?' + params.toString();
        if (zona && tipologia) href += '#' + encodeURIComponent(tipologia);
        var link = text(body, 'a', '', range);
        link.href = href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.title = 'Apri le quotazioni OMI per la tipologia ' + (tipologia || 'selezionata') +
            (zona ? ' nella zona ' + zona : ' nel comune ' + comune);
        link.setAttribute('aria-label', range + ' €/m²; apri le quotazioni OMI');
        return body;
    }

    function configureTypologies(select, estimate, options, selectedZone, automatic) {
        select.replaceChildren();
        var category = String(estimate.category || '').toUpperCase();
        var defaults = (options.defaults_by_category || {})[category] || {};
        var effectiveZone = selectedZone || estimate.omi_zona || '';
        var defaultCode = defaults[effectiveZone] || defaults[''] || estimate.omi_tipologia_default || '';
        var available = ((options.typologies_by_zone || {})[effectiveZone] || []);
        var labelsByCode = {};
        Object.keys(options.typologies_by_zone || {}).forEach(function (zone) {
            (options.typologies_by_zone[zone] || []).forEach(function (item) {
                labelsByCode[String(item.code)] = item.label;
            });
        });
        var defaultLabel = labelsByCode[String(defaultCode)] || String(defaultCode || '');
        option(select, '', defaultCode
            ? 'Automatica per categoria · ' + defaultLabel
            : 'Automatica per categoria');

        var seen = {};
        available.forEach(function (item) {
            var code = String(item.code);
            seen[code] = true;
            option(select, code, item.label || labelsByCode[code] || ('Tipologia OMI ' + code));
        });
        if (estimate.omi_tipologia && !seen[String(estimate.omi_tipologia)]) {
            var selectedCode = String(estimate.omi_tipologia);
            option(select, selectedCode, labelsByCode[selectedCode] || ('Tipologia selezionata · ' + selectedCode));
        }
        if (!automatic && estimate.omi_typology_selected && estimate.omi_tipologia) {
            select.value = String(estimate.omi_tipologia);
        } else {
            select.value = '';
        }
    }

    function saveSelection(estimate, zoneSelect, typologySelect, zoneCell, typologyCell) {
        var controls = [zoneSelect, typologySelect];
        controls.forEach(function (control) { control.disabled = true; });
        zoneCell.title = '';
        typologyCell.title = '';
        fetch('/manual-property-valuations/' + encodeURIComponent(estimate.asset_identity.asset_key) + '/omi-selection', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                comune_code: estimate.omi_comune,
                zone_code: zoneSelect.value || null,
                typology_code: typologySelect.value || null
            })
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (result) {
                if (!response.ok) throw new Error(result.detail || 'Salvataggio della selezione OMI non riuscito.');
                rememberAccordionState();
                window.location.reload();
            });
        }).catch(function (error) {
            controls.forEach(function (control) { control.disabled = false; });
            zoneCell.title = error.message;
            typologyCell.title = error.message;
            var message = zoneCell.querySelector('[data-omi-selection-error]');
            if (!message) {
                message = text(zoneCell, 'small', 'text-danger d-block', '');
                message.dataset.omiSelectionError = 'true';
            }
            message.textContent = error.message;
        });
    }

    function addSelections() {
        document.querySelectorAll('.document-land-map-content .imm-tables').forEach(function (wrapper, groupIndex) {
            var group = estimates[groupIndex] || {};
            var entries = group.buildings || [];
            var table = Array.from(wrapper.querySelectorAll('table.cat-compact-table')).find(function (item) {
                return item.dataset.landMapKind === 'buildings';
            });
            if (!table || table.dataset.omiSelectionsAdded) return;
            var header = table.querySelector('thead tr');
            var rows = Array.from(table.querySelectorAll('tbody tr'));
            if (!header) return;
            var rowEstimates = estimatesForRows(table, rows, entries);
            ensureOmiEstimateColumn(table, rows, rowEstimates);

            var targetIndex = Array.prototype.findIndex.call(header.cells, function (item) {
                return item.textContent.trim() === 'Stima OMI indicativa';
            });
            if (targetIndex < 0) {
                var cadastralIndex = Array.prototype.findIndex.call(header.cells, function (item) {
                    return item.textContent.trim() === 'Valore catastale';
                });
                targetIndex = cadastralIndex >= 0 ? cadastralIndex + 1 : header.cells.length;
            }
            var targetHead = header.cells[targetIndex] || null;
            var sizeHead = document.createElement('th');
            sizeHead.className = 'text-nowrap';
            sizeHead.textContent = 'Actual size [m²]';
            var zoneHead = document.createElement('th');
            zoneHead.className = 'text-nowrap';
            zoneHead.textContent = 'OMI Zone';
            var typologyHead = document.createElement('th');
            typologyHead.className = 'text-nowrap';
            typologyHead.textContent = 'OMI Tipologia';
            var priceHead = document.createElement('th');
            priceHead.className = 'text-nowrap text-end';
            priceHead.textContent = 'Fascia OMI €/m²';
            zoneHead.scope = typologyHead.scope = priceHead.scope = 'col';
            sizeHead.scope = 'col';
            if (targetHead) {
                header.insertBefore(sizeHead, targetHead);
                header.insertBefore(zoneHead, targetHead);
                header.insertBefore(typologyHead, targetHead);
                header.insertBefore(priceHead, targetHead);
            } else {
                header.append(sizeHead, zoneHead, typologyHead, priceHead);
            }

            rows.forEach(function (row, rowIndex) {
                var estimate = rowEstimates[rowIndex] || {};
                var identity = estimate.asset_identity || {};
                var targetCell = row.cells[targetIndex] || null;
                var sizeCell = addActualSizeCell(estimate);
                var priceCell = addOmiPriceRangeCell(estimate);
                if (!identity.asset_key || !estimate.omi_comune) {
                    var emptyZone = document.createElement('td');
                    var emptyTypology = document.createElement('td');
                    if (!editable) {
                        emptyZone.textContent = estimate.omi_zona || '—';
                        emptyTypology.textContent = estimate.omi_tipologia || estimate.omi_tipologia_default || '—';
                        emptyZone.title = emptyTypology.title = 'Accedi per modificare la selezione OMI.';
                    }
                    insertCells(row, [sizeCell, emptyZone, emptyTypology, priceCell], targetCell);
                    return;
                }
                var options = optionsByComune[estimate.omi_comune] || {zones: [], typologies_by_zone: {}, defaults_by_category: {}};

                if (!editable) {
                    var zone = String(estimate.omi_zona || '');
                    var typologyCode = String(estimate.omi_tipologia || estimate.omi_tipologia_default || '');
                    var typology = (options.typologies_by_zone[zone] || []).find(function (item) {
                        return String(item.code) === typologyCode;
                    });
                    var zoneDisplayCell = document.createElement('td');
                    zoneDisplayCell.textContent = zone || '—';
                    zoneDisplayCell.title = 'Accedi per modificare la selezione OMI.';
                    var typologyDisplayCell = document.createElement('td');
                    typologyDisplayCell.textContent = typology ? typology.label : (typologyCode || '—');
                    typologyDisplayCell.title = 'Accedi per modificare la selezione OMI.';
                    insertCells(row, [sizeCell, zoneDisplayCell, typologyDisplayCell, priceCell], targetCell);
                    return;
                }

                var zoneCell = addSelectCell();
                var zoneSelect = text(zoneCell.body, 'select', 'form-select form-select-sm');
                zoneSelect.setAttribute('aria-label', 'Zona OMI per il fabbricato');
                var guessedZone = String(estimate.omi_zona || '');
                option(zoneSelect, '', guessedZone ? 'Automatica · ' + guessedZone : 'Automatica · zona non determinata');
                var seenZones = {};
                (options.zones || []).forEach(function (item) {
                    var code = String(item.code || '');
                    if (!code) return;
                    seenZones[code] = true;
                    option(zoneSelect, code, item.label || code);
                });
                if (guessedZone && !seenZones[guessedZone]) option(zoneSelect, guessedZone, guessedZone + ' · stima corrente');
                if (estimate.omi_zone_selected && guessedZone) zoneSelect.value = guessedZone;
                else zoneSelect.value = '';
                var typologyCell = addSelectCell();
                var typologySelect = text(typologyCell.body, 'select', 'form-select form-select-sm');
                typologySelect.setAttribute('aria-label', 'Tipologia OMI per il fabbricato');
                configureTypologies(typologySelect, estimate, options, zoneSelect.value, false);
                insertCells(row, [sizeCell, zoneCell.body, typologyCell.body, priceCell], targetCell);

                zoneSelect.addEventListener('change', function () {
                    configureTypologies(typologySelect, estimate, options, zoneSelect.value, true);
                    saveSelection(estimate, zoneSelect, typologySelect, zoneCell.body, typologyCell.body);
                });
                typologySelect.addEventListener('change', function () {
                    saveSelection(estimate, zoneSelect, typologySelect, zoneCell.body, typologyCell.body);
                });
            });
            table.dataset.omiSelectionsAdded = 'true';
        });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addSelections, {once: true});
    else addSelections();
})();
