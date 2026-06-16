(function () {
    'use strict';

    var cfg = JSON.parse(document.getElementById('plan-config').textContent);
    var DOC_ID = cfg.doc_id;

    var TYPE_STYLE = {
        confine:    { fill: 'none',    stroke: '#495057', width: 1.5, opacity: 1.0 },
        particella: { fill: '#dce8f5', stroke: '#0d6efd', width: 0.8, opacity: 0.7 },
        fabbricato: { fill: '#fde8cc', stroke: '#fd7e14', width: 0.8, opacity: 0.8 },
        strada:     { fill: '#e8e8e8', stroke: '#6c757d', width: 0.8, opacity: 0.7 },
        acqua:      { fill: '#cce5ff', stroke: '#0dcaf0', width: 0.8, opacity: 0.8 },
        lineevarie: { fill: 'none',    stroke: '#adb5bd', width: 0.5, opacity: 0.6 },
        simboli:    { fill: '#f8d7da', stroke: '#dc3545', width: 0.5, opacity: 0.5 },
    };
    var TYPE_LABEL = {
        confine: 'Confine', particella: 'Particelle', fabbricato: 'Fabbricati',
        strada: 'Strade', acqua: 'Acque', lineevarie: 'Linee varie', simboli: 'Simboli',
    };

    var statusDiv = document.getElementById('plan-status');
    var infoDiv   = document.getElementById('cad-feature-info');
    var svg       = document.getElementById('plan-svg');
    var container = document.getElementById('plan-cad');

    // ── Pan/zoom ──────────────────────────────────────────────────────────────
    var tx = 0, ty = 0, scale = 1;
    var dragging = false, lastX = 0, lastY = 0;
    var gMain = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    svg.appendChild(gMain);

    function applyTransform() {
        gMain.setAttribute('transform', 'translate(' + tx + ',' + ty + ') scale(' + scale + ')');
    }

    container.addEventListener('mousedown', function (e) {
        if (e.button !== 0) return;
        dragging = true; lastX = e.clientX; lastY = e.clientY;
        container.classList.add('dragging');
        e.preventDefault();
    });
    window.addEventListener('mousemove', function (e) {
        if (!dragging) return;
        tx += e.clientX - lastX; ty += e.clientY - lastY;
        lastX = e.clientX; lastY = e.clientY;
        applyTransform();
    });
    window.addEventListener('mouseup', function () {
        dragging = false;
        container.classList.remove('dragging');
    });
    container.addEventListener('wheel', function (e) {
        e.preventDefault();
        var factor = e.deltaY < 0 ? 1.12 : 0.89;
        var rect = container.getBoundingClientRect();
        var cx = e.clientX - rect.left, cy = e.clientY - rect.top;
        tx = cx + (tx - cx) * factor;
        ty = cy + (ty - cy) * factor;
        scale *= factor;
        applyTransform();
    }, { passive: false });

    // ── Coordinate → screen ───────────────────────────────────────────────────
    var bbox, W, H, dataScale, offX, offY;

    function initProjection(data) {
        bbox = data.bbox;
        W = container.clientWidth || 800;
        H = container.clientHeight || 600;
        var margin = 40;
        var sx = (W - 2 * margin) / (bbox.maxx - bbox.minx || 1);
        var sy = (H - 2 * margin) / (bbox.maxy - bbox.miny || 1);
        dataScale = Math.min(sx, sy);
        offX = margin + ((W - 2 * margin) - (bbox.maxx - bbox.minx) * dataScale) / 2;
        offY = margin + ((H - 2 * margin) - (bbox.maxy - bbox.miny) * dataScale) / 2;
    }

    function toSvg(coord) {
        return [
            offX + (coord[0] - bbox.minx) * dataScale,
            H - offY - (coord[1] - bbox.miny) * dataScale,
        ];
    }

    function coordsToPath(ring) {
        if (!ring || !ring.length) return '';
        var pts = ring.map(toSvg);
        return 'M' + pts.map(function (p) { return p[0].toFixed(2) + ',' + p[1].toFixed(2); }).join('L') + 'Z';
    }

    function fitToView() {
        tx = 0; ty = 0; scale = 1;
        applyTransform();
    }

    // ── Legend ────────────────────────────────────────────────────────────────
    function buildLegend(types) {
        var div = document.getElementById('cad-layer-legend');
        div.innerHTML = '';
        var seen = {};
        types.forEach(function (t) {
            if (seen[t]) return;
            seen[t] = true;
            var s = TYPE_STYLE[t] || { fill: '#dee2e6', stroke: '#6c757d' };
            var item = document.createElement('div');
            item.className = 'd-flex align-items-center gap-2 small';
            item.innerHTML = '<span class="legend-dot" style="background:' + (s.fill === 'none' ? 'transparent' : s.fill) + ';border:1.5px solid ' + s.stroke + '"></span>'
                + '<span>' + (TYPE_LABEL[t] || t) + '</span>';
            div.appendChild(item);
        });
    }

    // ── Render entities ──────────────────────────────────────────────────────
    var selectedEl = null;
    var selectedOrigStyle = null;

    function renderEntities(entities) {
        gMain.innerHTML = '';
        var ORDER = ['simboli', 'lineevarie', 'acqua', 'strada', 'fabbricato', 'particella', 'confine'];
        var byType = {};
        entities.forEach(function (e) { (byType[e.type] = byType[e.type] || []).push(e); });
        var typesFound = [];

        ORDER.forEach(function (t) {
            if (!byType[t]) return;
            typesFound.push(t);
            var s = TYPE_STYLE[t] || { fill: '#dee2e6', stroke: '#6c757d', width: 0.8, opacity: 0.8 };
            byType[t].forEach(function (ent) {
                var pathData = coordsToPath(ent.coords);
                ent.holes.forEach(function (h) { pathData += coordsToPath(h); });
                if (!pathData) return;
                var el = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                el.setAttribute('d', pathData);
                el.setAttribute('fill', s.fill);
                el.setAttribute('fill-rule', 'evenodd');
                el.setAttribute('stroke', s.stroke);
                el.setAttribute('stroke-width', (s.width / dataScale).toFixed(4));
                el.setAttribute('opacity', s.opacity);
                el.addEventListener('click', function (e) {
                    e.stopPropagation();
                    if (selectedEl) {
                        selectedEl.setAttribute('fill', selectedOrigStyle.fill);
                        selectedEl.setAttribute('stroke', selectedOrigStyle.stroke);
                        selectedEl.setAttribute('stroke-width', selectedOrigStyle.sw);
                    }
                    selectedEl = el;
                    selectedOrigStyle = { fill: s.fill, stroke: s.stroke, sw: el.getAttribute('stroke-width') };
                    el.setAttribute('fill', s.fill === 'none' ? 'rgba(255,107,53,0.15)' : '#ff6b35');
                    el.setAttribute('stroke', '#ff6b35');
                    el.setAttribute('stroke-width', (2 / dataScale).toFixed(4));
                    infoDiv.innerHTML = '<strong>' + (TYPE_LABEL[ent.type] || ent.type) + '</strong>'
                        + (ent.label ? '<br><code class="small">' + ent.label + '</code>' : '')
                        + '<br><span class="text-muted small">' + ent.coords.length + ' vertici</span>';
                });
                gMain.appendChild(el);
            });
        });

        buildLegend(typesFound);
    }

    // ── Fetch and display ─────────────────────────────────────────────────────
    fetch('/web/documents/' + DOC_ID + '/plan')
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(function (data) {
            initProjection(data);
            renderEntities(data.entities);
            statusDiv.textContent = data.entities.length + ' entità · ' + data.format.toUpperCase();
            document.getElementById('badge-count').textContent = data.entities.length + ' entità';
            fitToView();
        })
        .catch(function (err) {
            statusDiv.textContent = 'Errore caricamento: ' + err.message;
            statusDiv.classList.add('text-danger');
        });

    document.getElementById('btn-fit').addEventListener('click', fitToView);

    svg.addEventListener('click', function () {
        if (selectedEl) {
            selectedEl.setAttribute('fill', selectedOrigStyle.fill);
            selectedEl.setAttribute('stroke', selectedOrigStyle.stroke);
            selectedEl.setAttribute('stroke-width', selectedOrigStyle.sw);
            selectedEl = null;
        }
        infoDiv.textContent = 'Click su un elemento';
    });
})();
