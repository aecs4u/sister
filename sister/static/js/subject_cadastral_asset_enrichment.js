// SISTER adapter for the shared cadastral enrichment renderer.
// Source: aecs4u-theme/static/js/cadastral_asset_enrichment.js. Keep its rendering behavior aligned.
(function () {
    var DEFAULTS = {
        visuraTablesSelector: '[data-visura-group-tables]',
        assetTableSelector: '[data-cadastral-asset-table]',
        ownerAssessmentTarget: '[data-owner-assessment-target]',
        documentUrlTemplate: '/documents/{id}',
        omiValuationUrl: '/omi/valuation',
        categoryColumnIndex: 6,
        labels: {
            assessmentHeading: 'Valutazione patrimoniale per quota e diritto',
            assessmentUnavailable: 'Non è stato possibile associare al soggetto una titolarità corrente. Non è stata presunta la piena proprietà.',
            right: 'Diritto', assetsAndShares: 'Beni e quote', attributedOmi: 'Stima OMI attribuibile',
            otherRight: 'Altro diritto', asset: 'bene', assets: 'beni', missingShare: 'quota non indicata',
            estimateOn: 'stima su', of: 'di', unvalued: 'non stimati:', unavailable: 'Non disponibile',
            assessmentDisclaimer: 'Valori indicativi, non una perizia. Il valore catastale è una base fiscale, non di mercato.',
            methodSummary: 'Come si calcola (coefficienti e avvertenze)', usufructSource: 'Prospetto fiscale usufrutto 2026',
            unmatchedAssets: 'elementi senza titolarità associabile al soggetto', unknownQuotaAssets: 'elementi con quota assente o non interpretabile, esclusi dal calcolo pro-quota',
            distinctMunicipalities: 'Comuni distinti', municipalityGroups: 'Gruppi Comuni',
            cadastralValue: 'Valore catastale', cadastralValueTitle: 'Valore catastale ai fini IMU, base imponibile fiscale e non valore di mercato.',
            cadastralBuildingTitle: 'Valore catastale ai fini IMU (rendita × 1,05 × moltiplicatore di categoria). Base imponibile fiscale, non è un valore di mercato.',
            cadastralLandTitle: 'Valore catastale ai fini IMU (reddito dominicale × 1,25 × 135). Base imponibile fiscale, non è un valore di mercato.',
            omiEstimate: 'Stima OMI indicativa', omiTitle: 'Intervallo OMI basato sulla superficie catastale; non è una perizia.',
            cadastralUnavailable: 'Valore catastale non disponibile.', omiUnavailable: 'Stima OMI non disponibile.',
            noOmiForLand: 'Non sono pubblicate quotazioni OMI per i terreni.',
            openOmi: 'Apri le quotazioni OMI per {category} nel comune {municipality}',
            openOmiZone: 'Apri le quotazioni OMI per {category} nella zona {zone}',
            historySource: 'Visura per immobile', situationAsOf: 'situazione al',
            openSource: 'Apri visura sorgente #{id}', mutation: 'Mutazione', current: 'Attuale',
            unnamedOwner: 'Intestatario non indicato', ongoing: 'in corso', from: 'dal', to: 'al',
            omittedEvents: 'Mostrati i 25 atti più recenti; apri la visura per la cronologia completa.',
            property: 'Immobile', act: 'atto', acts: 'atti', visura: 'visura', visure: 'visure',
            ownershipHeading: 'Titolarità', traceHeading: 'Tracciabilità titolarità',
            traceDescription: 'Atti ricavati dalle visure storiche per immobile salvate.',
            noHistoryOne: 'immobile senza visura per immobile collegata.', noHistoryMany: 'immobili senza visura per immobile collegata.'
        }
    };

    function initialize(pageData) {
    pageData = pageData || {};
    var PAGE_DATA_KEYS = {
        '[data-cadastral-asset-enrichment-config]': 'enrichment_config',
        '[data-land-map-omi-estimates]': 'omi_estimates',
        '[data-land-map-ownership-history]': 'ownership_history',
        '[data-land-map-owner-assessment]': 'owner_assessment'
    };
    function readJson(selector, fallback) {
        var key = PAGE_DATA_KEYS[selector];
        if (Object.prototype.hasOwnProperty.call(pageData, key)) {
            return pageData[key] == null ? fallback : pageData[key];
        }
        var source = document.querySelector(selector);
        if (!source) return fallback;
        try { return JSON.parse(source.textContent || 'null') || fallback; } catch (error) { return fallback; }
    }
    var config = Object.assign({}, DEFAULTS, readJson('[data-cadastral-asset-enrichment-config]', {}));
    config.labels = Object.assign({}, DEFAULTS.labels, config.labels || {});
    var labels = config.labels;
    var estimates = readJson('[data-land-map-omi-estimates]', []);
    var histories = readJson('[data-land-map-ownership-history]', []);
    var assessment = readJson('[data-land-map-owner-assessment]', null);

    function isMissingValue(value) {
        return /^(Non |Nessun|Quota non|Stima OMI non|Età|Durata|Metodo non|Not |No |Missing |Unavailable)/i.test(String(value || ''));
    }
    function textNode(parent, tag, className, value) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        node.textContent = value;
        parent.appendChild(node);
        return node;
    }
    function format(template, values) {
        return String(template).replace(/\{(\w+)\}/g, function (_match, key) {
            return values[key] === undefined ? '' : values[key];
        });
    }

    function addSubjectFinancialAssessment() {
        if (!assessment) return;
        var target = document.querySelector(config.ownerAssessmentTarget);
        var body = target;
        if (!body || body.querySelector('[data-owner-financial-assessment]')) return;

        var section = document.createElement('section');
        section.className = 'border-top mt-3 pt-3';
        section.dataset.ownerFinancialAssessment = 'true';
        section.setAttribute('aria-label', labels.assessmentHeading);
        var heading = document.createElement('h6');
        heading.className = 'mb-2';
        heading.innerHTML = '<i class="fas fa-scale-balanced text-info me-2" aria-hidden="true"></i>';
        heading.appendChild(document.createTextNode(labels.assessmentHeading));
        section.appendChild(heading);

        if (!assessment.has_rights) {
            textNode(section, 'p', 'small text-muted mb-0', labels.assessmentUnavailable);
            body.appendChild(section);
            return;
        }
        var metricRow = document.createElement('div');
        metricRow.className = 'row row-cols-2 row-cols-md-3 g-2 mb-2';
        (assessment.tiles || []).forEach(function (metric) {
            var column = document.createElement('div');
            column.className = 'col';
            var tile = document.createElement('div');
            tile.className = 'h-100 rounded border bg-light px-2 py-2';
            textNode(tile, 'div', 'small text-muted', metric.label || '');
            textNode(tile, 'div', metric.missing ? 'small text-muted' : 'fw-semibold small', metric.value || '');
            tile.title = metric.hint || '';
            column.appendChild(tile);
            metricRow.appendChild(column);
        });
        section.appendChild(metricRow);

        if ((assessment.rights || []).length) {
            var wrap = document.createElement('div');
            wrap.className = 'table-responsive';
            var table = document.createElement('table');
            table.className = 'table table-sm align-middle mb-2';
            var thead = document.createElement('thead');
            var headRow = document.createElement('tr');
            [labels.right, labels.assetsAndShares, labels.attributedOmi].forEach(function (headingText) {
                textNode(headRow, 'th', '', headingText).scope = 'col';
            });
            thead.appendChild(headRow);
            table.appendChild(thead);
            var tbody = document.createElement('tbody');
            assessment.rights.forEach(function (right) {
                var row = document.createElement('tr');
                var title = textNode(row, 'th', '', right.label || labels.otherRight);
                title.scope = 'row';
                var details = (right.assets || 0) + ' ' + (right.assets === 1 ? labels.asset : labels.assets) + ' · ' + (right.shares || labels.missingShare);
                if (right.unvalued_assets) {
                    details += ' · ' + labels.estimateOn + ' ' + (right.valued_assets || 0) + ' ' + labels.of + ' ' + right.assets;
                    if (right.unvalued_reasons && right.valued_assets) details += ' · ' + labels.unvalued + ' ' + right.unvalued_reasons;
                }
                textNode(row, 'td', '', details);
                var value = textNode(row, 'td', isMissingValue(right.value) ? 'text-muted small' : 'text-nowrap', right.value || labels.unavailable);
                tbody.appendChild(row);
            });
            table.appendChild(tbody);
            wrap.appendChild(table);
            section.appendChild(wrap);
        }

        textNode(section, 'p', 'small text-muted mb-1', labels.assessmentDisclaimer);
        var method = document.createElement('details');
        method.className = 'small mb-1 assessment-method';
        textNode(method, 'summary', '', labels.methodSummary);
        textNode(method, 'p', 'text-muted mb-1 mt-1', (assessment.coefficient_note ? assessment.coefficient_note + ' ' : '') + (assessment.method_note || ''));
        var methodLink = document.createElement('a');
        methodLink.href = assessment.source_url || '#';
        methodLink.textContent = labels.usufructSource;
        methodLink.target = '_blank';
        methodLink.rel = 'noopener noreferrer';
        method.appendChild(methodLink);
        section.appendChild(method);
        var exclusions = [];
        if (assessment.unmatched_assets) exclusions.push(assessment.unmatched_assets + ' ' + labels.unmatchedAssets);
        if (assessment.unknown_quota_assets) exclusions.push(assessment.unknown_quota_assets + ' ' + labels.unknownQuotaAssets);
        if (exclusions.length) textNode(section, 'p', 'small text-warning-emphasis mb-0 mt-1', exclusions.join('; ') + '.');
        body.appendChild(section);
    }

    function addDistinctMunicipalityCard() {
        var source = document.querySelector('[data-distinct-municipality-count]');
        if (!source) return;
        var row = document.querySelector('[data-subject-stats-row]');
        if (!row || row.querySelector('[data-distinct-municipality-card]')) return;
        var firstColumn = row.querySelector('[data-subject-group-count]');
        if (!firstColumn) return;
        var column = firstColumn.cloneNode(true);
        var value = column.querySelector('h4');
        var label = column.querySelector('small');
        if (!value || !label) return;
        column.dataset.distinctMunicipalityCard = 'true';
        value.textContent = source.dataset.distinctMunicipalityCount;
        label.textContent = labels.distinctMunicipalities;
        Array.from(row.children).concat(column).forEach(function (item) { item.className = 'col-6 col-lg'; });
        firstColumn.after(column);
    }

    var TABLE_COLUMNS = {
        buildings: [
            {key: 'cadastral_value', note: 'cadastral_note', missingKey: 'cadastral_missing', label: labels.cadastralValue, title: labels.cadastralBuildingTitle, missing: labels.cadastralUnavailable},
            {key: 'range', note: 'note', missingKey: 'missing', label: labels.omiEstimate, title: labels.omiTitle, missing: labels.omiUnavailable}
        ],
        lands: [
            {key: 'cadastral_value', note: 'cadastral_note', missingKey: 'cadastral_missing', label: labels.cadastralValue, title: labels.cadastralLandTitle, missing: labels.cadastralUnavailable}
        ]
    };
    function estimatePresence() {
        var present = {buildings: {}, lands: {}};
        estimates.forEach(function (group) {
            Object.keys(present).forEach(function (kind) {
                (group[kind] || []).forEach(function (item) {
                    TABLE_COLUMNS[kind].forEach(function (column) { if (item && item[column.key]) present[kind][column.key] = true; });
                });
            });
        });
        return present;
    }
    function addCell(row, estimate, column, kind) {
        var cell = document.createElement('td');
        if (column.key === 'range' && kind === 'lands') {
            cell.className = 'text-muted small';
            cell.textContent = labels.noOmiForLand;
            cell.title = labels.noOmiForLand;
            row.appendChild(cell);
            return;
        }
        var hasValue = !!(estimate && estimate[column.key]);
        cell.className = hasValue ? 'text-nowrap' : 'text-muted small';
        cell.textContent = hasValue ? estimate[column.key] : ((estimate && estimate[column.missingKey]) || column.missing);
        cell.title = (estimate && estimate[column.note]) || (kind === 'lands' && column.key === 'cadastral_value' ? labels.cadastralLandTitle : column.missing);
        row.appendChild(cell);
    }
    function addValueColumnsToVisura(present) {
        document.querySelectorAll(config.visuraTablesSelector).forEach(function (wrapper) {
            var group = estimates[Number(wrapper.dataset.visuraGroupIndex)] || {};
            wrapper.querySelectorAll('table').forEach(function (table) {
                var kind = table.dataset.landMapKind;
                if (!kind || table.dataset.valueColumnsAdded) return;
                var header = table.querySelector('thead tr');
                var rows = Array.from(table.querySelectorAll('tbody tr'));
                var entries = group[kind] || [];
                if (!header || !rows.length || rows.length !== entries.length) return;
                TABLE_COLUMNS[kind].forEach(function (column) {
                    if (!present[kind][column.key]) return;
                    var th = textNode(header, 'th', 'text-nowrap', column.label);
                    th.scope = 'col';
                    th.title = column.title;
                    rows.forEach(function (row, index) { addCell(row, entries[index], column, kind); });
                });
                table.dataset.valueColumnsAdded = 'true';
            });
        });
    }
    function addValueColumnsToAssetTable(present) {
        document.querySelectorAll(config.assetTableSelector).forEach(function (table) {
            if (table.dataset.valueColumnsAdded) return;
            var header = table.querySelector('thead tr');
            if (!header) return;
            var rows = Array.from(table.querySelectorAll('tbody tr[data-cadastral-asset-row]'));
            if (!rows.length) return;
            var kindsPresent = {buildings: false, lands: false};
            rows.forEach(function (row) { kindsPresent[row.dataset.assetKind] = true; });
            var columns = [];
            if (kindsPresent.buildings && present.buildings.cadastral_value || kindsPresent.lands && present.lands.cadastral_value) {
                columns.push({key: 'cadastral_value', label: labels.cadastralValue, title: labels.cadastralValueTitle, missing: labels.cadastralUnavailable});
            }
            if (kindsPresent.buildings && present.buildings.range) {
                columns.push({key: 'range', label: labels.omiEstimate, title: labels.omiTitle, missing: labels.omiUnavailable});
            }
            columns.forEach(function (column) {
                var th = textNode(header, 'th', 'text-nowrap', column.label);
                th.scope = 'col';
                th.title = column.title;
                rows.forEach(function (row) {
                    var group = estimates[Number(row.dataset.assetGroupIndex)] || {};
                    var estimate = (group[row.dataset.assetKind] || [])[Number(row.dataset.assetRowIndex)] || {};
                    addCell(row, estimate, column, row.dataset.assetKind);
                });
            });
            table.dataset.valueColumnsAdded = 'true';
        });
    }
    function addValueColumns() {
        if (!estimates.length) return;
        var present = estimatePresence();
        addValueColumnsToVisura(present);
        addValueColumnsToAssetTable(present);
    }

    function omiLink(entry, category, className) {
        if (!entry || !entry.category || !entry.omi_comune) return null;
        var url = new URL(config.omiValuationUrl, window.location.origin);
        url.searchParams.set('comune', entry.omi_comune);
        if (entry.omi_zona) url.searchParams.set('zona', entry.omi_zona);
        var link = document.createElement('a');
        link.href = url.toString();
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.className = className || 'omi-category-valuation-link text-decoration-none';
        link.dataset.omiValuationLink = 'true';
        link.title = entry.omi_zona
            ? format(labels.openOmiZone, {category: category, zone: entry.omi_zona})
            : format(labels.openOmi, {category: category, municipality: entry.omi_comune});
        link.setAttribute('aria-label', link.title);
        return link;
    }
    function linkBuildingCategoriesToOmi() {
        document.querySelectorAll(config.visuraTablesSelector).forEach(function (wrapper) {
            var entries = (estimates[Number(wrapper.dataset.visuraGroupIndex)] || {}).buildings || [];
            var table = wrapper.querySelector('[data-cadastral-table-kind="buildings"] table');
            if (!table) return;
            var rows = Array.from(table.querySelectorAll('tbody tr'));
            if (rows.length !== entries.length) return;
            rows.forEach(function (row, index) {
                var entry = entries[index] || {};
                var cell = row.cells[config.categoryColumnIndex];
                if (!cell || !entry.category || !entry.omi_comune) return;
                var badge = cell.querySelector('.cat-categoria');
                var category = badge ? badge.textContent.trim() : entry.category;
                var link = omiLink(entry, category);
                if (link) {
                    link.appendChild(badge ? badge.cloneNode(true) : document.createTextNode(category));
                    cell.replaceChildren(link);
                }
            });
        });
        document.querySelectorAll(config.assetTableSelector).forEach(function (table) {
            table.querySelectorAll('tbody tr[data-cadastral-asset-row][data-asset-kind="buildings"]').forEach(function (row) {
                var group = estimates[Number(row.dataset.assetGroupIndex)] || {};
                var entry = (group.buildings || [])[Number(row.dataset.assetRowIndex)] || {};
                var cell = row.cells[config.categoryColumnIndex];
                if (!cell || cell.querySelector('[data-omi-valuation-link]')) return;
                var badge = cell.querySelector('.cat-categoria');
                var category = badge ? badge.textContent.trim() : (entry.category || cell.textContent.trim());
                var link = omiLink(entry, category);
                if (link) {
                    link.appendChild(badge ? badge.cloneNode(true) : document.createTextNode(category));
                    cell.replaceChildren(link);
                }
            });
        });
    }
    function tagTables() {
        document.querySelectorAll(config.visuraTablesSelector + ' [data-cadastral-table-kind] table').forEach(function (table) {
            var marker = table.closest('[data-cadastral-table-kind]');
            if (marker && !table.dataset.landMapKind) table.dataset.landMapKind = marker.dataset.cadastralTableKind;
        });
    }

    function sourceBlock(item) {
        var block = document.createElement('div');
        block.className = 'border rounded p-2 mt-2';
        var title = item.label || labels.historySource;
        if (item.reference_date) title += ' · ' + labels.situationAsOf + ' ' + item.reference_date;
        textNode(block, 'div', 'fw-semibold mb-1', title);
        var link = document.createElement('a');
        link.href = String(config.documentUrlTemplate).replace('{id}', encodeURIComponent(item.document_id));
        link.className = 'd-inline-block mb-1';
        link.textContent = format(labels.openSource, {id: item.document_id});
        block.appendChild(link);
        (item.ownership || []).forEach(function (event) {
            var eventBlock = document.createElement('div');
            eventBlock.className = 'border-top pt-2 mt-1';
            var act = event.act || labels.mutation;
            if (event.date) act += ' · ' + event.date;
            var actNode = textNode(eventBlock, 'div', 'fw-semibold', act);
            if (event.current) textNode(actNode, 'span', 'badge bg-success ms-2', labels.current);
            var owners = event.owners || [];
            if (owners.length) {
                var list = document.createElement('ul');
                list.className = 'mb-1 ps-3';
                owners.forEach(function (owner) {
                    textNode(list, 'li', '', [owner.name || labels.unnamedOwner, owner.right, owner.quota, owner.period].filter(Boolean).join(' · '));
                });
                eventBlock.appendChild(list);
            } else if (event.start || event.end) {
                var period = event.ongoing
                    ? labels.ongoing + (event.start ? ' ' + labels.from + ' ' + event.start : '')
                    : [event.start && labels.from + ' ' + event.start, event.end && labels.to + ' ' + event.end].filter(Boolean).join(' ');
                textNode(eventBlock, 'div', 'text-muted', period);
            }
            block.appendChild(eventBlock);
        });
        if (item.omitted_events) textNode(block, 'small', 'text-muted d-block mt-1', labels.omittedEvents);
        return block;
    }
    function historyDetails(label, entry) {
        var sources = entry.sources || [];
        var total = sources.reduce(function (sum, item) { return sum + (item.event_count || (item.ownership || []).length); }, 0);
        var details = document.createElement('details');
        details.className = 'land-map-ownership-details mb-2';
        details.open = true;
        var summary = textNode(details, 'summary', 'fw-semibold', label || labels.property);
        textNode(summary, 'span', 'badge bg-light text-dark border ms-2', total + ' ' + (total === 1 ? labels.act : labels.acts) + ' · ' + sources.length + ' ' + (sources.length === 1 ? labels.visura : labels.visure));
        sources.forEach(function (item) { details.appendChild(sourceBlock(item)); });
        return details;
    }
    function propertyLabel(table, row) {
        var columns = table.dataset.landMapKind === 'buildings'
            ? {address: 0, municipality: 1, sheet: 2, parcel: 3, subunit: 4}
            : {municipality: 0, sheet: 1, parcel: 2};
        function value(name) {
            var index = columns[name];
            if (index === undefined || !row.cells[index]) return '';
            var copy = row.cells[index].cloneNode(true);
            copy.querySelectorAll('a, button, i').forEach(function (node) { node.remove(); });
            var text = copy.textContent.replace(/\s+/g, ' ').trim();
            return text === '—' ? '' : text;
        }
        return [value('address') || value('municipality'), value('sheet') && 'Fg ' + value('sheet'), value('parcel') && 'Part. ' + value('parcel'), value('subunit') && 'Sub. ' + value('subunit')].filter(Boolean).join(' · ');
    }
    function addOwnershipHistoryToVisura() {
        if (!histories.length) return;
        document.querySelectorAll(config.visuraTablesSelector).forEach(function (wrapper) {
            var group = histories[Number(wrapper.dataset.visuraGroupIndex)] || {};
            var items = [];
            var withoutHistory = 0;
            wrapper.querySelectorAll('table').forEach(function (table) {
                var entries = group[table.dataset.landMapKind] || [];
                Array.from(table.querySelectorAll('tbody tr')).forEach(function (row, index) {
                    var entry = entries[index];
                    if (entry && (entry.sources || []).length) items.push(historyDetails(propertyLabel(table, row), entry));
                    else withoutHistory += 1;
                });
            });
            if (!items.length) return;
            var groupBody = wrapper.closest('[data-visura-group-body]');
            var body = groupBody && groupBody.querySelector('[data-visura-ownership-timeline-body]');
            if (!body) {
                var card = document.createElement('div');
                card.className = 'card border-0 shadow-sm mb-4';
                var header = document.createElement('div');
                header.className = 'card-header bg-transparent border-bottom py-2 px-3';
                var title = textNode(header, 'h3', 'h6 mb-0 text-muted', labels.ownershipHeading);
                title.insertAdjacentHTML('afterbegin', '<i class="fas fa-history me-2" aria-hidden="true"></i>');
                card.appendChild(header);
                body = document.createElement('div');
                body.className = 'card-body py-3 px-3';
                card.appendChild(body);
                wrapper.after(card);
            }
            if (body.querySelector('[data-ownership-history]')) return;
            var section = document.createElement('section');
            section.className = 'land-map-trace mt-3 pt-3 border-top';
            section.dataset.ownershipHistory = 'true';
            var heading = textNode(section, 'h4', 'h6 mb-1', labels.traceHeading);
            heading.insertAdjacentHTML('afterbegin', '<i class="fas fa-route me-2 text-info" aria-hidden="true"></i>');
            textNode(section, 'p', 'small text-muted mb-2', labels.traceDescription);
            items.forEach(function (item) { section.appendChild(item); });
            if (withoutHistory) textNode(section, 'p', 'small text-muted mb-0', withoutHistory + ' ' + (withoutHistory === 1 ? labels.noHistoryOne : labels.noHistoryMany));
            body.appendChild(section);
        });
    }
    function addOwnershipHistoryToAssetTable() {
        if (!histories.length) return;
        document.querySelectorAll(config.assetTableSelector).forEach(function (table) {
            var columns = table.querySelectorAll('thead th').length;
            table.querySelectorAll('tbody tr[data-cadastral-asset-row]').forEach(function (row) {
                if (row.dataset.historyAdded) return;
                var group = histories[Number(row.dataset.assetGroupIndex)] || {};
                var entries = group[row.dataset.assetKind] || [];
                var entry = entries[Number(row.dataset.assetRowIndex)];
                if (!entry || !(entry.sources || []).length) return;
                var historyRow = document.createElement('tr');
                historyRow.className = 'cadastral-asset-history-row';
                var cell = document.createElement('td');
                cell.colSpan = columns;
                cell.appendChild(historyDetails(row.dataset.assetLabel || labels.property, entry));
                historyRow.appendChild(cell);
                row.after(historyRow);
                row.dataset.historyAdded = 'true';
            });
        });
    }
    function expandOwnershipTimelines() {
        if (!histories.length) return;
        document.querySelectorAll(config.visuraTablesSelector).forEach(function (wrapper) {
            var group = histories[Number(wrapper.dataset.visuraGroupIndex)] || {};
            var hasHistory = ['buildings', 'lands'].some(function (kind) {
                return (group[kind] || []).some(function (row) { return row && (row.sources || []).length; });
            });
            if (!hasHistory) return;
            var groupBody = wrapper.closest('[data-visura-group-body]');
            if (!groupBody) return;
            groupBody.querySelectorAll('.ownership-timeline .tl-body.d-none').forEach(function (body) {
                body.classList.remove('d-none');
                var chevron = body.previousElementSibling && body.previousElementSibling.querySelector('.tl-chevron');
                if (chevron) chevron.classList.add('open');
            });
        });
    }
    function enhance() {
        tagTables();
        addSubjectFinancialAssessment();
        addDistinctMunicipalityCard();
        addValueColumns();
        linkBuildingCategoriesToOmi();
        addOwnershipHistoryToVisura();
        addOwnershipHistoryToAssetTable();
        expandOwnershipTimelines();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', enhance, {once: true});
    else enhance();
    }

    var pageDataSource = document.querySelector('[data-subject-page-json-url]');
    if (pageDataSource && pageDataSource.dataset.subjectPageJsonUrl) {
        fetch(pageDataSource.dataset.subjectPageJsonUrl, {credentials: 'same-origin', headers: {Accept: 'application/json'}})
            .then(function (response) {
                if (!response.ok) throw new Error('Subject page data unavailable');
                return response.json();
            })
            .then(initialize)
            .catch(function () { initialize({}); });
    } else {
        initialize({});
    }
})();
