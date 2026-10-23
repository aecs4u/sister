(function () {
    'use strict';

    function elementsFor(container) {
        var card = container.closest('.document-parcel-map-card');
        var status = card && card.querySelector('[data-document-map-status]');
        return {
            status: status,
            message: status && status.querySelector('[data-map-status-text]'),
            retry: status && status.querySelector('[data-document-map-retry]')
        };
    }

    function setStatus(container, message, showRetry) {
        var parts = elementsFor(container);
        if (parts.message) parts.message.textContent = message;
        if (parts.status) parts.status.hidden = false;
        if (parts.retry) parts.retry.hidden = !showRetry;
    }

    function clearStatus(container) {
        var parts = elementsFor(container);
        if (parts.status) parts.status.hidden = true;
        if (parts.retry) parts.retry.hidden = true;
    }

    function cleanText(value) {
        return String(value == null ? '' : value).trim().replace(/\s+/g, '');
    }

    function normalizeName(value) {
        return String(value == null ? '' : value)
            .replace(/\s*\([^)]*\)\s*$/, '')
            .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
            .toLocaleLowerCase('it').replace(/[^a-z0-9]/g, '');
    }

    function normalizeCadastralPart(value) {
        var text = cleanText(value).toLocaleUpperCase('it');
        if (/^\d+$/.test(text)) return String(Number(text));
        return text;
    }

    function parcelMapKey(item) {
        var municipality = String(item.municipality_code || '').trim().toUpperCase()
            || normalizeName(item.municipality);
        return [municipality, normalizeCadastralPart(item.sheet), normalizeCadastralPart(item.parcel)].join('|');
    }

    function cellIndex(headers, matcher) {
        for (var i = 0; i < headers.length; i += 1) {
            if (matcher.test(headers[i])) return i;
        }
        return -1;
    }

    function findPropertyRows() {
        var records = [];
        document.querySelectorAll('.cat-compact-table').forEach(function (table) {
            var headers = Array.prototype.map.call(table.querySelectorAll('thead th'), function (cell) {
                return cell.textContent.trim().toLocaleLowerCase('it').replace(/\s+/g, ' ');
            });
            var municipalityIndex = cellIndex(headers, /^(?:municipality|comune)$/);
            var sheetIndex = cellIndex(headers, /^(?:fog\.?|foglio|sheet)$/);
            var parcelIndex = cellIndex(headers, /^(?:part\.?|particella|parcel)$/);
            if (sheetIndex < 0 || parcelIndex < 0) return;

            table.querySelectorAll('tbody tr').forEach(function (row) {
                var cells = row.cells;
                if (!cells[sheetIndex] || !cells[parcelIndex]) return;
                var sheet = normalizeCadastralPart(cells[sheetIndex].textContent);
                var parcel = normalizeCadastralPart(cells[parcelIndex].textContent);
                if (!sheet || !parcel || sheet === '—' || parcel === '—' || sheet === '-') return;

                var municipalityText = municipalityIndex >= 0 && cells[municipalityIndex]
                    ? cells[municipalityIndex].textContent.trim() : '';
                var codeMatch = municipalityText.match(/\(([A-Z0-9]{3,8})\)\s*$/i);
                var record = {
                    row: row,
                    parcelCell: cells[parcelIndex],
                    municipality: normalizeName(municipalityText),
                    municipalityCode: codeMatch ? codeMatch[1].toUpperCase() : '',
                    sheet: sheet,
                    parcel: parcel
                };
                records.push(record);
            });
        });
        return records;
    }

    function resolveRow(records, item) {
        var sheet = normalizeCadastralPart(item.sheet);
        var parcel = normalizeCadastralPart(item.parcel);
        if (!sheet || !parcel) return null;
        var candidates = records.filter(function (record) {
            return record.sheet === sheet && record.parcel === parcel;
        });
        var code = String(item.municipality_code || '').toUpperCase();
        if (code) {
            var byCode = candidates.find(function (record) { return record.municipalityCode === code; });
            if (byCode) return byCode;
        }
        var municipality = normalizeName(item.municipality);
        if (municipality) {
            var byMunicipality = candidates.find(function (record) { return record.municipality === municipality; });
            if (byMunicipality) return byMunicipality;
        }
        return candidates.length === 1 && !municipality && !code ? candidates[0] : null;
    }

    function rowTarget(targets, row) {
        if (!row) return null;
        if (!targets.has(row.row)) targets.set(row.row, { row: row.row, layers: [], point: null });
        return targets.get(row.row);
    }

    function highlightRow(row) {
        if (!row) return;
        row.scrollIntoView({ behavior: 'smooth', block: 'center' });
        row.classList.add('document-map-row-highlight');
        window.setTimeout(function () { row.classList.remove('document-map-row-highlight'); }, 1800);
    }

    function addMapActions(records, targets, map) {
        records.forEach(function (record) {
            var button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn btn-sm btn-link document-map-row-action';
            button.textContent = 'Mappa';
            button.setAttribute('aria-label', 'Mostra sulla mappa foglio ' + record.sheet + ', particella ' + record.parcel);
            button.addEventListener('click', function () {
                var target = targets.get(record.row);
                if (!target || (!target.layers.length && !target.point)) return;
                if (target.layers.length) {
                    var bounds = window.L.latLngBounds([]);
                    target.layers.forEach(function (layer) {
                        if (layer.getBounds && layer.getBounds().isValid()) bounds.extend(layer.getBounds());
                    });
                    if (bounds.isValid()) map.flyToBounds(bounds, { padding: [32, 32], maxZoom: 18 });
                    else if (target.layers[0].getLatLng) map.flyTo(target.layers[0].getLatLng(), Math.max(map.getZoom(), 17));
                    target.layers[0].openPopup && target.layers[0].openPopup();
                } else if (target.point) {
                    map.flyTo(target.point, Math.max(map.getZoom(), 17));
                }
                highlightRow(record.row);
            });
            var target = targets.get(record.row);
            if (target && (target.layers.length || target.point)) record.parcelCell.appendChild(button);
        });
    }

    async function initializeMap(container) {
        if (container.dataset.mapLoading === 'true' || container.dataset.mapInitialized === 'true') return;
        container.dataset.mapLoading = 'true';
        setStatus(container, 'Caricamento mappa catastale…', false);
        try {
            if (!window.L || !window.LandRegistryMap) {
                throw new Error('Leaflet or LandRegistryMap did not load');
            }
            var dataUrl = container.dataset.mapDataUrl;
            if (!dataUrl) throw new Error('Map data URL is missing');
            var response = await fetch(dataUrl, {
                credentials: 'same-origin',
                headers: { Accept: 'application/json' }
            });
            if (!response.ok) throw new Error('Map data request failed with status ' + response.status);
            var mapData = await response.json();

            var latitude = Number(container.dataset.centerLat);
            var longitude = Number(container.dataset.centerLng);
            var zoom = Number(container.dataset.zoom);
            if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
                throw new Error('Invalid map center coordinates');
            }

            var labels = {};
            try { labels = JSON.parse(container.dataset.labels || '{}'); } catch (_error) { labels = {}; }
            var parcelMarkers = Array.isArray(mapData.parcel_markers) ? mapData.parcel_markers : [];
            var parcelFeatures = mapData.parcel_features || { type: 'FeatureCollection', features: [] };
            var sheetFeatures = mapData.sheet_features || { type: 'FeatureCollection', features: [] };
            var listedOnly = container.dataset.listedOnly === 'true';
            var instance = window.LandRegistryMap.create({
                container: container,
                apiBase: container.dataset.apiBase,
                center: [latitude, longitude],
                zoom: Number.isFinite(zoom) ? zoom : 17,
                labels: labels
            });
            container._documentMapInstance = instance;
            if (listedOnly) {
                instance.map.removeLayer(instance.cadastral.sheets);
                instance.map.removeLayer(instance.cadastral.parcels);
            }

            var records = findPropertyRows();
            var targets = new Map();
            var dataBounds = window.L.latLngBounds([]);
            var listedLayers = {};
            var featureKeys = new Set();

            function addListedGeometry(key, collection, style) {
                var features = collection && Array.isArray(collection.features) ? collection.features : [];
                var layer = window.L.geoJSON({ type: 'FeatureCollection', features: features }, {
                    style: style,
                    onEachFeature: function (feature, featureLayer) {
                        var properties = feature.properties || {};
                        var popup = document.createElement('span');
                        popup.textContent = properties.reference || 'Elemento catastale';
                        featureLayer.bindPopup(popup);
                        if (key === 'parcels') {
                            featureKeys.add(parcelMapKey(properties));
                            var row = resolveRow(records, properties);
                            var target = rowTarget(targets, row);
                            if (target) target.layers.push(featureLayer);
                            featureLayer.on('click', function () { if (row) highlightRow(row.row); });
                        }
                    }
                }).addTo(instance.map);
                if (layer.getBounds().isValid()) dataBounds.extend(layer.getBounds());
                listedLayers[key] = layer;
            }

            if (listedOnly) {
                addListedGeometry('sheets', sheetFeatures, {
                    color: '#1976a8', weight: 2, fillColor: '#1976a8', fillOpacity: 0.05
                });
                addListedGeometry('parcels', parcelFeatures, {
                    color: '#d63384', weight: 2, fillColor: '#d63384', fillOpacity: 0.18
                });
            }

            var markerBounds = [];
            parcelMarkers.forEach(function (item) {
                var markerLat = Number(item.latitude);
                var markerLng = Number(item.longitude);
                if (!Number.isFinite(markerLat) || !Number.isFinite(markerLng)) return;
                var markerKey = parcelMapKey(item);
                if (listedOnly && featureKeys.has(markerKey)) return;
                var selected = Boolean(item.selected);
                var marker = window.L.circleMarker([markerLat, markerLng], {
                    radius: selected ? 8 : 6,
                    color: '#ffffff',
                    weight: 2,
                    fillColor: selected ? '#d63384' : '#0d6efd',
                    fillOpacity: 1
                }).addTo(instance.map);
                var popup = document.createElement('span');
                popup.textContent = item.reference || 'Particella';
                marker.bindPopup(popup);
                var row = resolveRow(records, item);
                var target = rowTarget(targets, row);
                if (target) {
                    target.layers.push(marker);
                    target.point = [markerLat, markerLng];
                }
                marker.on('click', function () { if (row) highlightRow(row.row); });
                if (selected) marker.openPopup();
                markerBounds.push([markerLat, markerLng]);
                dataBounds.extend([markerLat, markerLng]);
            });

            if (!markerBounds.length && (!listedOnly || !(parcelFeatures.features || []).length)
                && container.dataset.centerKind === 'parcel') {
                var selectedMarker = window.L.circleMarker([latitude, longitude], {
                    radius: 8, color: '#ffffff', weight: 2, fillColor: '#d63384', fillOpacity: 1
                }).addTo(instance.map);
                var selectedPopup = document.createElement('span');
                selectedPopup.textContent = container.dataset.reference || 'Particella selezionata';
                selectedMarker.bindPopup(selectedPopup).openPopup();
                markerBounds.push([latitude, longitude]);
                dataBounds.extend([latitude, longitude]);
            }

            if (listedOnly && dataBounds.isValid()) {
                instance.map.fitBounds(dataBounds, { padding: [28, 28], maxZoom: 17 });
            } else if (markerBounds.length > 1) {
                instance.map.fitBounds(markerBounds, { padding: [28, 28], maxZoom: 17 });
            }

            addMapActions(records, targets, instance.map);
            document.querySelectorAll('[data-document-map-layer]').forEach(function (toggle) {
                if (toggle.dataset.documentMapLayer !== container.id) return;
                var layer = listedLayers[toggle.dataset.layerKey];
                if (!layer) return;
                function applyListedLayerState() {
                    if (toggle.checked) layer.addTo(instance.map);
                    else instance.map.removeLayer(layer);
                }
                applyListedLayerState();
                toggle.addEventListener('change', applyListedLayerState);
            });

            document.querySelectorAll('[data-land-registry-layer-map]').forEach(function (toggle) {
                if (toggle.dataset.landRegistryLayerMap !== container.id) return;
                var layer = instance.cadastral[toggle.dataset.layerKey];
                if (!layer) return;
                function applyLayerState() {
                    if (toggle.checked) layer.addTo(instance.map);
                    else instance.map.removeLayer(layer);
                }
                applyLayerState();
                toggle.addEventListener('change', applyLayerState);
            });

            container.dataset.mapInitialized = 'true';
            clearStatus(container);
            window.requestAnimationFrame(function () { instance.map.invalidateSize(); });
            window.addEventListener('resize', function () { instance.map.invalidateSize(); }, { passive: true });
        } catch (error) {
            if (container._documentMapInstance) {
                container._documentMapInstance.map.remove();
                delete container._documentMapInstance;
            }
            var message = error && error.message && error.message.indexOf('Map data request failed') === 0
                ? 'I dati delle particelle non sono disponibili. Controlla la connessione e riprova.'
                : 'La mappa catastale non è stata caricata. Riprova tra poco.';
            setStatus(container, message, true);
            if (window.console) console.error('[DocumentParcelMap] initialization failed', error);
        } finally {
            delete container.dataset.mapLoading;
        }
    }

    function initializeMaps() {
        document.querySelectorAll('[data-document-parcel-map]').forEach(function (container) {
            var parts = elementsFor(container);
            if (parts.retry) {
                parts.retry.addEventListener('click', function () {
                    if (parts.retry.dataset.documentMapScriptRetry === 'true') {
                        window.location.reload();
                        return;
                    }
                    initializeMap(container);
                });
            }
            initializeMap(container);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initializeMaps, { once: true });
    } else {
        initializeMaps();
    }
})();
