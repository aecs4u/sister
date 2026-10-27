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
        if (!estimate.asset_identity || !estimate.asset_identity.asset_key) return body;
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
            if (!header || rows.length !== entries.length) return;

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
            zoneHead.scope = typologyHead.scope = 'col';
            sizeHead.scope = 'col';
            if (targetHead) {
                header.insertBefore(sizeHead, targetHead);
                header.insertBefore(zoneHead, targetHead);
                header.insertBefore(typologyHead, targetHead);
            } else {
                header.append(sizeHead, zoneHead, typologyHead);
            }

            rows.forEach(function (row, rowIndex) {
                var estimate = entries[rowIndex] || {};
                var identity = estimate.asset_identity || {};
                var targetCell = row.cells[targetIndex] || null;
                var sizeCell = addActualSizeCell(estimate);
                if (!identity.asset_key || !estimate.omi_comune) {
                    var emptyZone = document.createElement('td');
                    var emptyTypology = document.createElement('td');
                    insertCells(row, [sizeCell, emptyZone, emptyTypology], targetCell);
                    return;
                }
                var options = optionsByComune[estimate.omi_comune] || {zones: [], typologies_by_zone: {}, defaults_by_category: {}};

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
                insertCells(row, [sizeCell, zoneCell.body, typologyCell.body], targetCell);

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
