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

    function fillEstimateCell(cell, estimate) {
        var value = estimate.range || estimate.missing || 'Stima OMI non disponibile';
        cell.className = estimate.range ? 'text-end text-nowrap' : 'text-muted small';
        cell.textContent = value;
        cell.title = estimate.note || estimate.missing || 'Stima OMI non disponibile.';
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
            fillEstimateCell(cell, estimate);
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
                return applySavedChange().then(function () { input.disabled = false; });
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
                return applySavedChange().then(function () {
                    controls.forEach(function (control) { control.disabled = false; });
                });
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

    // The size, zone, typology and price cells for one building row. Built again after each save,
    // from the refreshed estimate, so the row never needs a page reload.
    function buildCells(estimate) {
        var identity = estimate.asset_identity || {};
        var cells = {size: addActualSizeCell(estimate), price: addOmiPriceRangeCell(estimate)};
        if (!identity.asset_key || !estimate.omi_comune) {
            cells.zone = document.createElement('td');
            cells.typology = document.createElement('td');
            if (!editable) {
                cells.zone.textContent = estimate.omi_zona_label || estimate.omi_zona || '—';
                cells.typology.textContent = estimate.omi_tipologia || estimate.omi_tipologia_default || '—';
                cells.zone.title = cells.typology.title = 'Accedi per modificare la selezione OMI.';
            }
            return tagCells(cells);
        }
        var options = optionsByComune[estimate.omi_comune] || {zones: [], typologies_by_zone: {}, defaults_by_category: {}};

        if (!editable) {
            var zone = String(estimate.omi_zona || '');
            var typologyCode = String(estimate.omi_tipologia || estimate.omi_tipologia_default || '');
            var typology = (options.typologies_by_zone[zone] || []).find(function (item) {
                return String(item.code) === typologyCode;
            });
            cells.zone = document.createElement('td');
            cells.zone.textContent = estimate.omi_zona_label || zone || '—';
            cells.zone.title = 'Accedi per modificare la selezione OMI.';
            cells.typology = document.createElement('td');
            cells.typology.textContent = typology ? typology.label : (typologyCode || '—');
            cells.typology.title = 'Accedi per modificare la selezione OMI.';
            return tagCells(cells);
        }

        var zoneCell = addSelectCell();
        var zoneSelect = text(zoneCell.body, 'select', 'form-select form-select-sm');
        zoneSelect.setAttribute('aria-label', 'Zona OMI per il fabbricato');
        var guessedZone = String(estimate.omi_zona || '');
        var guessedLabel = estimate.omi_zona_label || guessedZone;
        option(zoneSelect, '', guessedZone ? 'Automatica · ' + guessedLabel : 'Automatica · zona non determinata');
        var seenZones = {};
        (options.zones || []).forEach(function (item) {
            var code = String(item.code || '');
            if (!code) return;
            seenZones[code] = true;
            option(zoneSelect, code, item.label || code);
        });
        if (guessedZone && !seenZones[guessedZone]) option(zoneSelect, guessedZone, guessedLabel + ' · stima corrente');
        if (estimate.omi_zone_selected && guessedZone) zoneSelect.value = guessedZone;
        else zoneSelect.value = '';
        var typologyCell = addSelectCell();
        var typologySelect = text(typologyCell.body, 'select', 'form-select form-select-sm');
        typologySelect.setAttribute('aria-label', 'Tipologia OMI per il fabbricato');
        configureTypologies(typologySelect, estimate, options, zoneSelect.value, false);
        cells.zone = zoneCell.body;
        cells.typology = typologyCell.body;

        zoneSelect.addEventListener('change', function () {
            configureTypologies(typologySelect, estimate, options, zoneSelect.value, true);
            saveSelection(estimate, zoneSelect, typologySelect, zoneCell.body, typologyCell.body);
        });
        typologySelect.addEventListener('change', function () {
            saveSelection(estimate, zoneSelect, typologySelect, zoneCell.body, typologyCell.body);
        });
        return tagCells(cells);
    }

    function tagCells(cells) {
        Object.keys(cells).forEach(function (name) { cells[name].dataset.omiCell = name; });
        return cells;
    }

    function parseJson(node) {
        try { return JSON.parse(node.textContent || 'null'); } catch (error) { return null; }
    }

    function estimatesByAssetKey(groups) {
        var byKey = {};
        (groups || []).forEach(function (group) {
            (group.buildings || []).forEach(function (estimate) {
                var key = estimate && estimate.asset_identity && estimate.asset_identity.asset_key;
                if (key) byKey[key] = estimate;
            });
        });
        return byKey;
    }

    function replaceFromFresh(fresh, selector, closestSelector) {
        var live = document.querySelector(selector);
        var next = fresh.querySelector(selector);
        if (!live && !next) return;
        if (!live || !next) throw new Error('Page structure changed: ' + selector);
        if (closestSelector) { live = live.closest(closestSelector) || live; next = next.closest(closestSelector) || next; }
        live.replaceWith(document.importNode(next, true));
    }

    function reloadScript(pattern) {
        var old = Array.from(document.scripts).find(function (script) { return pattern.test(script.src); });
        if (!old) return;
        var script = document.createElement('script');
        if (old.nonce) script.nonce = old.nonce;
        script.async = false;
        script.src = old.src.replace(/([?&])refresh=\d+&?/, '$1').replace(/[?&]$/, '') +
            (old.src.indexOf('?') === -1 ? '?' : '&') + 'refresh=' + Date.now();
        document.head.appendChild(script);
    }

    // Applies a saved choice without reloading: fetch the server-rendered page again and copy over only the
    // parts that depend on the OMI estimate, leaving the map, scroll position and open sections alone.
    function refreshInPlace() {
        return fetch(window.location.href, {credentials: 'same-origin', headers: {Accept: 'text/html'}})
            .then(function (response) {
                if (!response.ok || response.redirected) throw new Error('Refresh failed');
                return response.text();
            })
            .then(function (html) {
                var fresh = new DOMParser().parseFromString(html, 'text/html');
                var freshEstimates = fresh.querySelector('[data-land-map-omi-estimates]');
                var freshOptions = fresh.querySelector('[data-land-map-omi-options]');
                if (!freshEstimates || !freshOptions) throw new Error('Refresh returned no OMI data');
                var nextEstimates = parseJson(freshEstimates);
                var nextOptions = parseJson(freshOptions);
                if (!Array.isArray(nextEstimates) || !nextOptions) throw new Error('Refresh returned invalid OMI data');

                ['[data-land-map-omi-estimates]', '[data-land-map-omi-options]', '[data-land-map-owner-assessment]'].forEach(function (selector) {
                    var live = document.querySelector(selector);
                    var next = fresh.querySelector(selector);
                    if (live && next) live.textContent = next.textContent;
                });
                estimates = nextEstimates;
                optionsByComune = nextOptions;

                Array.from(document.querySelectorAll('.document-land-map-kpis')).forEach(function (live, index) {
                    var next = fresh.querySelectorAll('.document-land-map-kpis')[index];
                    if (!next) throw new Error('Page structure changed: KPI cards');
                    live.replaceWith(document.importNode(next, true));
                });
                replaceFromFresh(fresh, '#central-omi-heading', 'section');
                replaceFromFresh(fresh, '#adjusted-omi-heading', 'section');

                var byKey = estimatesByAssetKey(nextEstimates);
                document.querySelectorAll('tr[data-omi-asset-key]').forEach(function (row) {
                    var estimate = byKey[row.dataset.omiAssetKey];
                    if (!estimate) return;
                    var estimateCell = row.querySelector('[data-omi-cell="estimate"]');
                    if (estimateCell) fillEstimateCell(estimateCell, estimate);
                    var cells = buildCells(estimate);
                    Object.keys(cells).forEach(function (name) {
                        var old = row.querySelector('[data-omi-cell="' + name + '"]');
                        if (old) old.replaceWith(cells[name]);
                    });
                });

                // These sections are drawn by other scripts from the data swapped above, so draw them again.
                var assessment = document.querySelector('[data-owner-financial-assessment]');
                if (assessment) assessment.remove();
                reloadScript(/cadastral_asset_enrichment\.js/);
                var manualRoot = document.querySelector('[data-manual-property-valuations-root]');
                var freshManualRoot = fresh.querySelector('[data-manual-property-valuations-root]');
                if (manualRoot && freshManualRoot) {
                    var liveManualData = document.querySelector('[data-manual-property-valuations]');
                    var freshManualData = fresh.querySelector('[data-manual-property-valuations]');
                    if (liveManualData && freshManualData) liveManualData.textContent = freshManualData.textContent;
                    manualRoot.replaceWith(document.importNode(freshManualRoot, true));
                    reloadScript(/manual_property_valuations\.js/);
                }
            });
    }

    // Prefer the in-place refresh; if anything in it fails, fall back to reloading so the page is never left stale.
    function applySavedChange() {
        return refreshInPlace().catch(function () {
            rememberAccordionState();
            window.location.reload();
        });
    }

    // One selector in the "OMI Zone" header sets the zone of every editable row of the group (table) at once.
    // Typology goes back to "automatica", as when the zone of a single row is changed.
    function addBulkZoneSelect(zoneHead, groupEstimates) {
        if (!editable) return;
        var targets = groupEstimates.filter(function (estimate) {
            return estimate && estimate.asset_identity && estimate.asset_identity.asset_key && estimate.omi_comune;
        });
        if (targets.length < 2) return;
        var comunes = {};
        targets.forEach(function (estimate) { comunes[estimate.omi_comune] = true; });
        var comuneCodes = Object.keys(comunes);
        if (comuneCodes.length !== 1) return;
        var options = optionsByComune[comuneCodes[0]] || {};
        var zones = (options.zones || []).filter(function (item) { return item && item.code; });
        if (!zones.length) return;

        var select = text(zoneHead, 'select', 'form-select form-select-sm mt-1 fw-normal');
        select.setAttribute('aria-label', 'Imposta la stessa zona OMI per tutti gli immobili del gruppo');
        select.title = 'Imposta la stessa zona OMI per tutti gli immobili di questo gruppo (' + targets.length + ')';
        option(select, '', 'Tutti…');
        option(select, '__auto__', 'Automatica (tutti)');
        zones.forEach(function (item) { option(select, String(item.code), item.label || String(item.code)); });
        select.addEventListener('change', function () {
            var chosen = select.value;
            if (!chosen) return;
            var zone = chosen === '__auto__' ? null : chosen;
            var label = zone ? select.options[select.selectedIndex].textContent : 'automatica';
            if (!window.confirm('Impostare la zona OMI «' + label + '» per ' + targets.length + ' immobili di questo gruppo?')) {
                select.value = '';
                return;
            }
            select.disabled = true;
            zoneHead.title = '';
            var chain = Promise.resolve();
            var failed = 0;
            targets.forEach(function (estimate) {
                chain = chain.then(function () {
                    return fetch('/manual-property-valuations/' + encodeURIComponent(estimate.asset_identity.asset_key) + '/omi-selection', {
                        method: 'PUT',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({comune_code: estimate.omi_comune, zone_code: zone, typology_code: null})
                    }).then(function (response) { if (!response.ok) failed += 1; }, function () { failed += 1; });
                });
            });
            chain.then(applySavedChange).then(function () {
                select.value = '';
                select.disabled = false;
                if (failed) {
                    zoneHead.title = failed + ' immobili non aggiornati (zona non disponibile per il comune?).';
                    var msg = zoneHead.querySelector('[data-omi-bulk-error]') ||
                        text(zoneHead, 'small', 'text-danger d-block', '');
                    msg.dataset.omiBulkError = 'true';
                    msg.textContent = failed + ' non aggiornati';
                }
            });
        });
    }

    // Same idea for the "OMI Tipologia" header: one typology for every editable row of the group. Each row keeps
    // its own zone choice; a row whose zone has no such typology is reported and left as it was, so a mostly
    // uniform group is set here and the few exceptions are then changed in their own row.
    // Kind of consistency written in a row ("3,5 vani", "120 m²", "45 m³"), read from the row's cells.
    function consistencyUnit(row) {
        var content = Array.from(row.cells).map(function (cell) { return cell.textContent; }).join(' | ');
        var match = /\d\s*(vani|vano|mq|m²|m2|mc|m³|m3)(?![a-z])/i.exec(content);
        if (!match) return '';
        var unit = match[1].toLowerCase();
        if (unit === 'vano' || unit === 'vani') return 'vani';
        if (unit === 'mq' || unit === 'm²' || unit === 'm2') return 'm²';
        return 'm³';
    }

    // Same idea for the "OMI Tipologia" header: one typology for the rows of the group that match a filter
    // (consistency in vani / m², or a category; default every row). Each row keeps its own zone choice; a row whose
    // zone has no such typology is reported and left as it was.
    function addBulkTypologySelect(typologyHead, groupEstimates, rows) {
        if (!editable) return;
        var items = groupEstimates.map(function (estimate, index) {
            return {estimate: estimate || {}, unit: rows[index] ? consistencyUnit(rows[index]) : ''};
        }).filter(function (item) {
            var e = item.estimate;
            return e.asset_identity && e.asset_identity.asset_key && e.omi_comune;
        });
        if (items.length < 2) return;
        var comuneCodes = Object.keys(items.reduce(function (acc, item) { acc[item.estimate.omi_comune] = true; return acc; }, {}));
        if (comuneCodes.length !== 1) return;
        var options = optionsByComune[comuneCodes[0]] || {};
        var labels = {};
        Object.keys(options.typologies_by_zone || {}).forEach(function (zone) {
            (options.typologies_by_zone[zone] || []).forEach(function (item) {
                labels[String(item.code)] = item.label || ('Tipologia OMI ' + item.code);
            });
        });
        var codes = Object.keys(labels);
        if (!codes.length) return;

        var filters = [{value: '', label: 'Tutti gli immobili', test: function () { return true; }}];
        ['vani', 'm²', 'm³'].forEach(function (unit) {
            if (items.some(function (item) { return item.unit === unit; })) {
                filters.push({value: 'u:' + unit, label: 'Consistenza in ' + unit, test: function (item) { return item.unit === unit; }});
            }
        });
        var categories = {};
        items.forEach(function (item) {
            var category = String(item.estimate.category || '').trim();
            if (category) categories[category] = true;
        });
        Object.keys(categories).sort().forEach(function (category) {
            filters.push({value: 'c:' + category, label: 'Categoria ' + category, test: function (item) {
                return String(item.estimate.category || '').trim() === category;
            }});
        });

        var filterSelect = null;
        if (filters.length > 1) {
            filterSelect = text(typologyHead, 'select', 'form-select form-select-sm mt-1 fw-normal');
            filterSelect.setAttribute('aria-label', 'Immobili a cui applicare la tipologia');
            filters.forEach(function (filter) {
                var count = items.filter(filter.test).length;
                option(filterSelect, filter.value, filter.label + ' (' + count + ')');
            });
        }
        function matching() {
            var filter = filters.find(function (f) { return filterSelect && f.value === filterSelect.value; }) || filters[0];
            return items.filter(filter.test);
        }

        var select = text(typologyHead, 'select', 'form-select form-select-sm mt-1 fw-normal');
        select.setAttribute('aria-label', 'Imposta la tipologia OMI per gli immobili selezionati');
        select.title = 'Imposta la stessa tipologia OMI per gli immobili scelti nel filtro sopra';
        option(select, '', 'Applica a…');
        option(select, '__auto__', 'Automatica');
        codes.forEach(function (code) { option(select, code, labels[code]); });
        select.addEventListener('change', function () {
            var chosen = select.value;
            if (!chosen) return;
            var targets = matching();
            var typology = chosen === '__auto__' ? null : chosen;
            var label = typology ? labels[typology] : 'automatica';
            if (!targets.length || !window.confirm('Impostare la tipologia OMI «' + label + '» per ' + targets.length + ' immobili?')) {
                select.value = '';
                return;
            }
            select.disabled = true;
            if (filterSelect) filterSelect.disabled = true;
            typologyHead.title = '';
            var current = estimatesByAssetKey(estimates);
            var chain = Promise.resolve();
            var failed = 0;
            targets.forEach(function (target) {
                var estimate = current[target.estimate.asset_identity.asset_key] || target.estimate;
                chain = chain.then(function () {
                    return fetch('/manual-property-valuations/' + encodeURIComponent(estimate.asset_identity.asset_key) + '/omi-selection', {
                        method: 'PUT',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            comune_code: estimate.omi_comune,
                            zone_code: estimate.omi_zone_selected && estimate.omi_zona ? estimate.omi_zona : null,
                            typology_code: typology
                        })
                    }).then(function (response) { if (!response.ok) failed += 1; }, function () { failed += 1; });
                });
            });
            chain.then(applySavedChange).then(function () {
                select.value = '';
                select.disabled = false;
                if (filterSelect) filterSelect.disabled = false;
                if (failed) {
                    var msg = typologyHead.querySelector('[data-omi-bulk-error]') ||
                        text(typologyHead, 'small', 'text-danger d-block', '');
                    msg.dataset.omiBulkError = 'true';
                    msg.textContent = failed + ' non aggiornati (tipologia assente nella loro zona)';
                }
            });
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

            addBulkZoneSelect(zoneHead, rowEstimates);
            addBulkTypologySelect(typologyHead, rowEstimates, rows);

            rows.forEach(function (row, rowIndex) {
                var estimate = rowEstimates[rowIndex] || {};
                var targetCell = row.cells[targetIndex] || null;
                var cells = buildCells(estimate);
                if (estimate.asset_identity && estimate.asset_identity.asset_key) {
                    row.dataset.omiAssetKey = estimate.asset_identity.asset_key;
                }
                if (targetCell && targetHead && targetHead.textContent.trim() === 'Stima OMI indicativa') {
                    targetCell.dataset.omiCell = 'estimate';
                }
                insertCells(row, [cells.size, cells.zone, cells.typology, cells.price], targetCell);
            });
            table.dataset.omiSelectionsAdded = 'true';
        });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addSelections, {once: true});
    else addSelections();
})();
