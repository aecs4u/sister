(function () {
    'use strict';

    var cfg = JSON.parse(document.getElementById('plan-config').textContent);
    var DOC_ID = cfg.doc_id;
    var PROJ4_STR = cfg.proj4;

    // ── Projection setup ─────────────────────────────────────────────────────
    proj4.defs('CUSTOM', PROJ4_STR);

    function toWgs84(coord) {
        var pt = proj4('CUSTOM', 'WGS84', [coord[0], coord[1]]);
        return [pt[1], pt[0]];
    }

    function projectGeometry(geom) {
        if (!geom) return null;
        if (geom.type === 'Point') {
            return { type: 'Point', coordinates: toWgs84(geom.coordinates) };
        }
        if (geom.type === 'LineString') {
            return { type: 'LineString', coordinates: geom.coordinates.map(toWgs84) };
        }
        if (geom.type === 'Polygon') {
            return { type: 'Polygon', coordinates: geom.coordinates.map(function (ring) { return ring.map(toWgs84); }) };
        }
        if (geom.type === 'MultiPolygon') {
            return {
                type: 'MultiPolygon', coordinates: geom.coordinates.map(function (poly) {
                    return poly.map(function (ring) { return ring.map(toWgs84); });
                })
            };
        }
        if (geom.type === 'MultiLineString') {
            return { type: 'MultiLineString', coordinates: geom.coordinates.map(function (line) { return line.map(toWgs84); }) };
        }
        return geom;
    }

    // ── Map setup ────────────────────────────────────────────────────────────
    var map = L.map('plan-map', { preferCanvas: true });

    var baseLayers = {
        osm: L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© <a href="https://osm.org">OpenStreetMap</a>',
            maxZoom: 20
        }),
        satellite: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
            attribution: '© Esri',
            maxZoom: 20
        }),
    };
    baseLayers.osm.addTo(map);

    document.querySelectorAll('#basemap-switch .layer-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            document.querySelectorAll('#basemap-switch .layer-btn').forEach(function (b) { b.classList.remove('active'); });
            btn.classList.add('active');
            var layer = btn.dataset.layer;
            Object.values(baseLayers).forEach(function (l) { map.removeLayer(l); });
            if (baseLayers[layer]) baseLayers[layer].addTo(map);
        });
    });

    // ── Layer palette (by LIVELLO property) ─────────────────────────────────
    var PALETTE = [
        '#0d6efd', '#198754', '#dc3545', '#fd7e14', '#6610f2',
        '#20c997', '#d63384', '#0dcaf0', '#6c757d', '#ffc107',
    ];
    var colorMap = {};
    var colorIdx = 0;

    function colorFor(level) {
        if (!level) return '#6c757d';
        if (!colorMap[level]) {
            colorMap[level] = PALETTE[colorIdx % PALETTE.length];
            colorIdx++;
        }
        return colorMap[level];
    }

    // ── Feature info panel ───────────────────────────────────────────────────
    var infoDiv = document.getElementById('feature-info');
    function showFeatureInfo(props) {
        if (!props || !Object.keys(props).length) {
            infoDiv.textContent = 'Nessuna proprietà';
            return;
        }
        var rows = Object.keys(props).map(function (k) {
            return '<tr><th>' + k + '</th><td>' + (props[k] !== null ? props[k] : '–') + '</td></tr>';
        }).join('');
        infoDiv.innerHTML = '<table class="table table-sm table-bordered mb-0">' + rows + '</table>';
    }

    // ── Load GeoJSON ─────────────────────────────────────────────────────────
    var statusDiv = document.getElementById('plan-status');
    var allLayers = [];
    var geojsonGroup = L.featureGroup().addTo(map);
    var selectedLayer = null;

    fetch('/web/documents/' + DOC_ID + '/geojson')
        .then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        })
        .then(function (data) {
            var features = (data.geojson && data.geojson.features) || [];
            statusDiv.textContent = features.length + ' elementi · EPSG:' + (data.epsg || '?');

            features.forEach(function (feat) {
                var projGeom = projectGeometry(feat.geometry);
                if (!projGeom) return;

                var props = feat.properties || {};
                var level = props.LIVELLO || '';
                var color = colorFor(level);

                var opts = {
                    color: color,
                    weight: level === 'CONFINI' ? 2.5 : 1,
                    opacity: 0.85,
                    fillColor: color,
                    fillOpacity: level === 'CONFINI' ? 0.08 : 0.2,
                };

                var projFeat = { type: 'Feature', geometry: projGeom, properties: props };
                try {
                    var layer = L.geoJSON(projFeat, {
                        style: function () { return opts; },
                        pointToLayer: function (f, latlng) {
                            return L.circleMarker(latlng, { radius: 4, color: color, fillColor: color, fillOpacity: 0.8 });
                        },
                    });

                    layer.on('click', function (e) {
                        L.DomEvent.stopPropagation(e);
                        if (selectedLayer) {
                            try { selectedLayer.resetStyle(); } catch (ex) { }
                        }
                        selectedLayer = layer;
                        layer.setStyle({ weight: 3, color: '#ff6b35', fillOpacity: 0.4 });
                        showFeatureInfo(props);
                    });

                    layer.addTo(geojsonGroup);
                    allLayers.push({ layer: layer, level: level, color: color });
                } catch (ex) { }
            });

            if (geojsonGroup.getLayers().length > 0) {
                map.fitBounds(geojsonGroup.getBounds(), { padding: [20, 20] });
            }

            var legendDiv = document.getElementById('layer-legend');
            var shown = {};
            allLayers.forEach(function (item) {
                if (shown[item.level]) return;
                shown[item.level] = true;
                var div = document.createElement('div');
                div.className = 'd-flex align-items-center gap-2 small';
                div.innerHTML = '<span class="legend-dot" style="background:' + item.color + ';border:1px solid rgba(0,0,0,.2)"></span>'
                    + '<span>' + (item.level || '(senza livello)') + '</span>';
                legendDiv.appendChild(div);
            });

            document.getElementById('btn-fit').addEventListener('click', function () {
                if (geojsonGroup.getLayers().length > 0) {
                    map.fitBounds(geojsonGroup.getBounds(), { padding: [20, 20] });
                }
            });
        })
        .catch(function (err) {
            statusDiv.textContent = 'Errore caricamento: ' + err.message;
            statusDiv.classList.add('text-danger');
        });

    map.on('click', function () {
        if (selectedLayer) {
            try { selectedLayer.resetStyle(); } catch (ex) { }
            selectedLayer = null;
        }
        infoDiv.textContent = 'Click su un elemento';
    });
})();
