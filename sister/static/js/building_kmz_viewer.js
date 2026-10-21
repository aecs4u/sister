/**
 * KMZ/KML viewer for /3d: reads a KMZ (zip) or KML in the browser and draws its extruded polygons, lines and
 * points with the self-hosted three.js (static/vendor/three-0.160.1). Nothing is sent to a third party.
 *
 * Source: #kmzViewer[data-kmz-url] on load, or a file chosen/dropped by the user. Files are untrusted: text from the
 * KML is only ever inserted with textContent, never as HTML.
 *
 * Buttons use data-click / data-change (static/js/sister_actions.js): kmzViewer.reset | top | openFile.
 */
import * as THREE from '/static/vendor/three-0.160.1/three.module.min.js';
import { makeWallTexture, makeRoofTexture, makePlasterTexture } from '/static/js/building_textures.js?v=20261017a';

const stage = document.getElementById('kmzViewer');
if (stage) {
  const statusEl = document.getElementById('kmzStatus');
  const infoEl = document.getElementById('kmzInfo');
  const legendEl = document.getElementById('kmzLegend');
  const M_PER_DEG = 111320;
  const MAX_BYTES = 25 * 1024 * 1024;

  let renderer = null;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  } catch (err) {
    setStatus('WebGL non disponibile in questo browser: impossibile mostrare il modello 3D.');
  }

  function setStatus(text) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.hidden = !text;
  }

  // ---- zip / kmz ---------------------------------------------------------------------------------------------------
  async function inflateRaw(bytes) {
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }

  async function readKmlText(buffer, fileName) {
    const bytes = new Uint8Array(buffer);
    const decoder = new TextDecoder('utf-8');
    if (!(bytes[0] === 0x50 && bytes[1] === 0x4b)) return decoder.decode(bytes); // plain KML
    const view = new DataView(buffer);
    let eocd = -1;
    for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i--) {
      if (view.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error('Archivio KMZ non valido');
    const count = view.getUint16(eocd + 10, true);
    let p = view.getUint32(eocd + 16, true);
    const entries = [];
    for (let n = 0; n < count; n++) {
      if (view.getUint32(p, true) !== 0x02014b50) throw new Error('Archivio KMZ danneggiato');
      const method = view.getUint16(p + 10, true);
      const size = view.getUint32(p + 20, true);
      const nameLen = view.getUint16(p + 28, true);
      const extraLen = view.getUint16(p + 30, true);
      const commentLen = view.getUint16(p + 32, true);
      const offset = view.getUint32(p + 42, true);
      entries.push({ name: decoder.decode(bytes.subarray(p + 46, p + 46 + nameLen)), method, size, offset });
      p += 46 + nameLen + extraLen + commentLen;
    }
    const kmls = entries.filter((e) => /\.kml$/i.test(e.name));
    const entry = kmls.find((e) => /(^|\/)doc\.kml$/i.test(e.name)) || kmls[0];
    if (!entry) throw new Error('Nessun file .kml nel KMZ' + (fileName ? ' (' + fileName + ')' : ''));
    const localNameLen = view.getUint16(entry.offset + 26, true);
    const localExtraLen = view.getUint16(entry.offset + 28, true);
    const start = entry.offset + 30 + localNameLen + localExtraLen;
    const raw = bytes.subarray(start, start + entry.size);
    if (entry.method === 0) return decoder.decode(raw);
    if (entry.method === 8) return decoder.decode(await inflateRaw(raw));
    throw new Error('Compressione zip non supportata (' + entry.method + ')');
  }

  // ---- kml ---------------------------------------------------------------------------------------------------------
  function kmlColor(hex, fallback) {
    if (!hex || !/^[0-9a-f]{8}$/i.test(hex)) return fallback;
    return { color: '#' + hex.slice(6, 8) + hex.slice(4, 6) + hex.slice(2, 4), alpha: parseInt(hex.slice(0, 2), 16) / 255 };
  }

  const parseJson = (text) => { try { return text ? JSON.parse(text) : {}; } catch (e) { return {}; } };
  const first = (el, tag) => (el ? el.getElementsByTagName(tag)[0] || null : null);
  const textOf = (el, tag) => { const n = first(el, tag); return n ? n.textContent.trim() : ''; };

  function parseCoords(el) {
    return (el ? el.textContent : '').trim().split(/\s+/).map((t) => t.split(',').map(Number))
      .filter((c) => c.length >= 2 && Number.isFinite(c[0]) && Number.isFinite(c[1]))
      .map((c) => ({ lng: c[0], lat: c[1], alt: Number.isFinite(c[2]) ? c[2] : 0 }));
  }

  function plainText(html) {
    if (!html) return '';
    const doc = new DOMParser().parseFromString(html, 'text/html');
    doc.querySelectorAll('br,p,tr').forEach((n) => n.append('\n'));
    doc.querySelectorAll('td').forEach((n) => n.append('  '));
    return (doc.body.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
  }

  function parseKml(text) {
    const xml = new DOMParser().parseFromString(text, 'application/xml');
    if (xml.getElementsByTagName('parsererror').length) throw new Error('KML non valido');
    const styles = {};
    for (const s of xml.getElementsByTagName('Style')) {
      const poly = first(s, 'PolyStyle');
      const line = first(s, 'LineStyle');
      styles[s.getAttribute('id') || ''] = {
        fill: kmlColor(textOf(poly, 'color'), null),
        line: kmlColor(textOf(line, 'color'), null),
      };
    }
    const items = [];
    for (const pm of xml.getElementsByTagName('Placemark')) {
      const styleRef = textOf(pm, 'styleUrl').replace(/^#/, '');
      const inline = first(pm, 'Style');
      const style = styles[styleRef] || (inline && {
        fill: kmlColor(textOf(first(inline, 'PolyStyle'), 'color'), null),
        line: kmlColor(textOf(first(inline, 'LineStyle'), 'color'), null),
      }) || {};
      const data = {};
      for (const d of pm.getElementsByTagName('Data')) data[d.getAttribute('name')] = textOf(d, 'value');
      const base = { name: textOf(pm, 'name'), description: plainText(textOf(pm, 'description')), style,
        width: Number(data.width_m) || 0, target: styleRef === 'road_target', highlighted: styleRef === 'near',
        role: data.role || '', floors: Number(data.floors) || 0, facade: parseJson(data.facade) };
      for (const poly of pm.getElementsByTagName('Polygon')) {
        const outer = parseCoords(first(first(poly, 'outerBoundaryIs'), 'coordinates'));
        const holes = [...poly.getElementsByTagName('innerBoundaryIs')].map((h) => parseCoords(first(h, 'coordinates')));
        if (outer.length >= 3) {
          items.push({ ...base, kind: 'polygon', outer, holes, extrude: textOf(poly, 'extrude') === '1',
            mode: textOf(poly, 'altitudeMode') || 'clampToGround' });
        }
      }
      for (const line of pm.getElementsByTagName('LineString')) {
        const pts = parseCoords(first(line, 'coordinates'));
        if (pts.length >= 2) items.push({ ...base, kind: 'line', points: pts });
      }
      for (const pt of pm.getElementsByTagName('Point')) {
        const pts = parseCoords(first(pt, 'coordinates'));
        if (pts.length) items.push({ ...base, kind: 'point', point: pts[0] });
      }
    }
    return items;
  }

  // ---- scene -------------------------------------------------------------------------------------------------------
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x1b2631);
  const hemi = new THREE.HemisphereLight(0xffffff, 0x44505a, 1.1);
  scene.add(hemi);
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(-120, 220, 90);
  sun.shadow.mapSize.set(2048, 2048);
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 0.05;
  scene.add(sun, sun.target);
  const camera = new THREE.PerspectiveCamera(45, 1, 0.5, 5000);
  const content = new THREE.Group();
  scene.add(content);
  const layers = {};
  const raycaster = new THREE.Raycaster();
  const cam = { target: new THREE.Vector3(), radius: 200, azimuth: -0.6, polar: 1.0, home: null };
  let dirty = true;
  let pickables = [];
  let selected = null;
  let targetBox = new THREE.Box3();

  function placeCamera() {
    cam.polar = Math.min(Math.max(cam.polar, 0.02), Math.PI / 2 - 0.02);
    cam.radius = Math.min(Math.max(cam.radius, 5), 3000);
    camera.position.set(
      cam.target.x + cam.radius * Math.sin(cam.polar) * Math.sin(cam.azimuth),
      cam.target.y + cam.radius * Math.cos(cam.polar),
      cam.target.z + cam.radius * Math.sin(cam.polar) * Math.cos(cam.azimuth),
    );
    camera.lookAt(cam.target);
    dirty = true;
  }

  function resize() {
    if (!renderer) return;
    const w = stage.clientWidth || 600;
    const h = stage.clientHeight || 400;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    dirty = true;
  }

  // Objects that only exist in one of the two looks (schematic / realistic) and colours that change between them.
  const reg = { schematic: [], real: [], outlines: [], recolor: [], roads: [], ground: null, grid: null };
  const textures = new Map();
  let realisticOn = stage.dataset.realistic !== '0';
  let sceneExtent = 200;
  const SKY = 0xbfd8ec;

  function applyMode() {
    const on = realisticOn;
    for (const o of reg.schematic) o.visible = !on;
    for (const o of reg.real) o.visible = on;
    for (const o of reg.outlines) o.visible = !on;
    for (const r of reg.recolor) r.mat.color.set(on ? r.real : r.base);
    for (const r of reg.roads) r.mat.color.set(on ? r.real : r.base);
    if (reg.ground) reg.ground.visible = on;
    if (reg.grid) reg.grid.visible = !on;
    scene.background = new THREE.Color(on ? SKY : 0x1b2631);
    scene.fog = on ? new THREE.Fog(SKY, sceneExtent * 2.5, sceneExtent * 9) : null;
    hemi.color.set(on ? 0xdfeeff : 0xffffff);
    hemi.groundColor.set(on ? 0x8a8574 : 0x44505a);
    hemi.intensity = on ? 1.15 : 1.1;
    sun.intensity = on ? 2.6 : 1.6;
    sun.castShadow = on;
    if (renderer) {
      renderer.toneMapping = on ? THREE.ACESFilmicToneMapping : THREE.NoToneMapping;
      renderer.toneMappingExposure = 1.0;
    }
    content.traverse((o) => { if (o.material) o.material.needsUpdate = true; });
    dirty = true;
  }

  function cachedTexture(key, make) {
    if (!textures.has(key)) textures.set(key, make());
    return textures.get(key);
  }

  function disposeContent() {
    content.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) { if (o.material.map) o.material.map.dispose(); o.material.dispose(); }
    });
    content.clear();
    for (const t of textures.values()) t.dispose();
    textures.clear();
    Object.assign(reg, { schematic: [], real: [], outlines: [], recolor: [], roads: [], ground: null, grid: null });
    for (const name of ['buildings', 'roads', 'labels', 'realistic']) {
      layers[name] = new THREE.Group();
      content.add(layers[name]);
    }
    pickables = [];
    selected = null;
  }

  // Flat road as one rectangle per segment (so a bend never leaves a hole), lying just above the ground.
  function roadRibbon(points, width, y, color) {
    const positions = [];
    const indices = [];
    for (let i = 1; i < points.length; i++) {
      const a = points[i - 1];
      const b = points[i];
      const dir = new THREE.Vector3(b.x - a.x, 0, b.z - a.z);
      if (dir.lengthSq() < 1e-6) continue;
      dir.normalize();
      const nx = -dir.z * (width / 2);
      const nz = dir.x * (width / 2);
      const base = positions.length / 3;
      positions.push(a.x + nx, y, a.z + nz, a.x - nx, y, a.z - nz, b.x - nx, y, b.z - nz, b.x + nx, y, b.z + nz);
      indices.push(base, base + 1, base + 2, base, base + 2, base + 3);
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    geometry.setIndex(indices);
    return new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
      color, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2,
    }));
  }

  // Street name as a sprite at the middle of the road, drawn on top so buildings never hide it.
  function roadLabel(name, points, target) {
    const mid = new THREE.Vector3();
    let walked = 0;
    const half = points.reduce((sum, p, i) => (i ? sum + p.distanceTo(points[i - 1]) : 0), 0) / 2;
    for (let i = 1; i < points.length; i++) {
      const seg = points[i].distanceTo(points[i - 1]);
      if (walked + seg >= half) { mid.lerpVectors(points[i - 1], points[i], seg ? (half - walked) / seg : 0); break; }
      walked += seg;
    }
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    const font = (target ? 'bold 52px ' : '600 40px ') + 'sans-serif';
    ctx.font = font;
    canvas.width = Math.ceil(ctx.measureText(name).width) + 28;
    canvas.height = 80;
    ctx.font = font;
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 8;
    ctx.strokeStyle = 'rgba(20,28,36,0.9)';
    ctx.strokeText(name, 14, 42);
    ctx.fillStyle = target ? '#ffd54f' : '#dfe6ec';
    ctx.fillText(name, 14, 42);
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true }));
    const k = target ? 0.11 : 0.085;
    sprite.scale.set(canvas.width * k, canvas.height * k, 1);
    sprite.position.set(mid.x, 4, mid.z);
    sprite.renderOrder = 10;
    return sprite;
  }

  const layerState = { buildings: true, roads: true, labels: true };
  function applyLayerState() {
    for (const name of Object.keys(layerState)) if (layers[name]) layers[name].visible = layerState[name];
    if (layers.realistic) layers.realistic.visible = layerState.buildings;
    dirty = true;
  }

  function build(items) {
    disposeContent();
    const coordsOf = (list) => list.flatMap((i) => i.outer || i.points || [i.point]);
    const polygons = items.filter((i) => i.kind === 'polygon');
    // Frame the buildings when there are any, so long roads do not push the camera away.
    const all = coordsOf(polygons.length ? polygons : items);
    if (!all.length) throw new Error('Il file non contiene geometrie');
    const lngs = all.map((c) => c.lng);
    const lats = all.map((c) => c.lat);
    const lng0 = (Math.min(...lngs) + Math.max(...lngs)) / 2;
    const lat0 = (Math.min(...lats) + Math.max(...lats)) / 2;
    const kx = M_PER_DEG * Math.cos((lat0 * Math.PI) / 180);
    const east = (c) => (c.lng - lng0) * kx;
    const north = (c) => (c.lat - lat0) * M_PER_DEG;
    let maxHeight = 0;
    let buildings = 0;
    let roads = 0;
    targetBox = new THREE.Box3();
    const labelled = new Map();

    // Road samples (east/north metres) to tell which walls face a street.
    const roadSamples = [];
    for (const item of items) {
      if (item.kind !== 'line' || !(item.width > 0)) continue;
      for (let i = 1; i < item.points.length; i++) {
        const a = { x: east(item.points[i - 1]), y: north(item.points[i - 1]) };
        const b = { x: east(item.points[i]), y: north(item.points[i]) };
        const steps = Math.max(1, Math.ceil(Math.hypot(b.x - a.x, b.y - a.y) / 2));
        for (let k = 0; k <= steps; k++) roadSamples.push({ x: a.x + ((b.x - a.x) * k) / steps, y: a.y + ((b.y - a.y) * k) / steps });
      }
    }
    const facesStreet = (mid, normal) => roadSamples.some((p) => Math.hypot(p.x - mid.x, p.y - mid.y) < 16
      && (p.x - mid.x) * normal.x + (p.y - mid.y) * normal.y > 0);

    function realisticWalls(item, userData) {
      const closed = item.outer[0].lng === item.outer[item.outer.length - 1].lng
        && item.outer[0].lat === item.outer[item.outer.length - 1].lat;
      const ring = (closed ? item.outer.slice(0, -1) : item.outer).map((c) => ({ x: east(c), y: north(c) }));
      const height = Math.max(...item.outer.map((c) => c.alt));
      const floors = item.floors || Math.max(1, Math.round(height / 3));
      let area = 0;
      ring.forEach((p, i) => { const q = ring[(i + 1) % ring.length]; area += p.x * q.y - q.x * p.y; });
      const ccw = area > 0;
      ring.forEach((p, i) => {
        let a = p;
        let b = ring[(i + 1) % ring.length];
        const length = Math.hypot(b.x - a.x, b.y - a.y);
        if (length < 0.6) return;
        if (!ccw) [a, b] = [b, a]; // keep the outside on the right of a->b so the texture is not mirrored
        const dx = (b.x - a.x) / length;
        const dy = (b.y - a.y) / length;
        const normal = { x: dy, y: -dx };
        const street = facesStreet({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, normal);
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute('position', new THREE.Float32BufferAttribute(
          [a.x, 0, -a.y, b.x, 0, -b.y, b.x, height, -b.y, a.x, height, -a.y], 3));
        geometry.setAttribute('uv', new THREE.Float32BufferAttribute([0, 0, 1, 0, 1, 1, 0, 1], 2));
        geometry.setIndex([0, 1, 2, 0, 2, 3]);
        geometry.computeVertexNormals();
        const map = makeWallTexture({ length, height, floors, facade: item.facade, street, seed: i + Math.round(area) });
        const wall = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ map, roughness: 0.93, side: THREE.DoubleSide }));
        wall.castShadow = wall.receiveShadow = true;
        wall.userData = userData;
        layers.realistic.add(wall);
        reg.real.push(wall);
        pickables.push(wall);
      });
    }

    function realisticFacet(item, positions, userData) {
      const points = [];
      for (let i = 0; i < positions.length; i += 3) points.push(new THREE.Vector3(positions[i], positions[i + 1], positions[i + 2]));
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
      const index = [];
      for (let i = 1; i < points.length - 1; i++) index.push(0, i, i + 1);
      geometry.setIndex(index);
      let material;
      if (item.role === 'roof') {
        // UVs in metres on the slope plane: u along the eave edge, v up the slope.
        const along = points[1].clone().sub(points[0]);
        const u = along.clone().normalize();
        const normal = new THREE.Vector3().crossVectors(along, points[points.length - 1].clone().sub(points[0])).normalize();
        const v = new THREE.Vector3().crossVectors(normal, u).normalize();
        geometry.setAttribute('uv', new THREE.Float32BufferAttribute(
          points.flatMap((p) => { const d = p.clone().sub(points[0]); return [d.dot(u), d.dot(v)]; }), 2));
        const color = (item.facade && item.facade.roof) || '#b5573a';
        material = new THREE.MeshStandardMaterial({
          map: cachedTexture('roof' + color, () => makeRoofTexture(color)), roughness: 0.85, side: THREE.DoubleSide,
        });
      } else {
        const color = (item.facade && item.facade.band) || '#e8dcb0';
        const d0 = points[1].clone().sub(points[0]);
        geometry.setAttribute('uv', new THREE.Float32BufferAttribute(
          points.flatMap((p) => { const d = p.clone().sub(points[0]); return [Math.hypot(d.x, d.z) * Math.sign(d.dot(d0) || 1), d.y]; }), 2));
        material = new THREE.MeshStandardMaterial({
          map: cachedTexture('plaster' + color, () => makePlasterTexture(color)), roughness: 0.95, side: THREE.DoubleSide,
        });
      }
      geometry.computeVertexNormals();
      const mesh = new THREE.Mesh(geometry, material);
      mesh.castShadow = mesh.receiveShadow = true;
      mesh.userData = userData;
      layers.realistic.add(mesh);
      reg.real.push(mesh);
      pickables.push(mesh);
    }

    for (const item of items) {
      const fill = (item.style && item.style.fill) || { color: '#9aa7b4', alpha: 1 };
      const edge = (item.style && item.style.line) || { color: '#404040', alpha: 1 };
      if (item.kind === 'polygon') {
        const alts = item.outer.map((c) => c.alt);
        if (!item.extrude && item.mode !== 'clampToGround' && Math.max(...alts) - Math.min(...alts) > 0.01) {
          // Roof slope or gable triangle: a planar polygon with its own height at every vertex (fan-triangulated).
          const ring = item.outer.slice(0, item.outer[0].lng === item.outer[item.outer.length - 1].lng
            && item.outer[0].lat === item.outer[item.outer.length - 1].lat ? -1 : undefined);
          const positions = ring.flatMap((c) => [east(c), c.alt, -north(c)]);
          const indices = [];
          for (let i = 1; i < ring.length - 1; i++) indices.push(0, i, i + 1);
          const facet = new THREE.BufferGeometry();
          facet.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
          facet.setIndex(indices);
          facet.computeVertexNormals();
          const slope = new THREE.Mesh(facet, new THREE.MeshLambertMaterial({
            color: fill.color, transparent: fill.alpha < 1, opacity: fill.alpha, side: THREE.DoubleSide,
          }));
          slope.userData = { name: item.name, description: item.description, height: Math.max(...alts) };
          if (item.highlighted) targetBox.expandByObject(slope);
          const slopeOutline = new THREE.LineSegments(
            new THREE.EdgesGeometry(facet, 20), new THREE.LineBasicMaterial({ color: edge.color }));
          layers.buildings.add(slope, slopeOutline);
          pickables.push(slope);
          if (item.role === 'roof' || item.role === 'gable') {
            reg.schematic.push(slope);
            reg.outlines.push(slopeOutline);
            realisticFacet(item, positions.slice(), slope.userData);
          } else {
            reg.outlines.push(slopeOutline);
          }
          maxHeight = Math.max(maxHeight, Math.max(...alts));
          continue;
        }
        const alt = Math.max(...item.outer.map((c) => c.alt));
        const extruded = item.extrude && item.mode !== 'clampToGround' && alt > 0;
        const shape = new THREE.Shape(item.outer.map((c) => new THREE.Vector2(east(c), north(c))));
        for (const hole of item.holes) {
          if (hole.length >= 3) shape.holes.push(new THREE.Path(hole.map((c) => new THREE.Vector2(east(c), north(c)))));
        }
        const geometry = extruded
          ? new THREE.ExtrudeGeometry(shape, { depth: alt, bevelEnabled: false })
          : new THREE.ShapeGeometry(shape);
        geometry.rotateX(-Math.PI / 2);
        if (!extruded && item.mode !== 'clampToGround') geometry.translate(0, alt, 0);
        else if (!extruded) geometry.translate(0, 0.05, 0);
        const material = new THREE.MeshLambertMaterial({
          color: fill.color, transparent: fill.alpha < 1, opacity: fill.alpha, side: THREE.DoubleSide,
        });
        const mesh = new THREE.Mesh(geometry, material);
        mesh.userData = { name: item.name, description: item.description, height: extruded ? alt : 0 };
        const outline = new THREE.LineSegments(
          new THREE.EdgesGeometry(geometry, 20), new THREE.LineBasicMaterial({ color: edge.color }));
        layers.buildings.add(mesh, outline);
        pickables.push(mesh);
        reg.outlines.push(outline);
        if (extruded && item.role === 'wall') {
          reg.schematic.push(mesh);
          realisticWalls(item, mesh.userData);
        } else if (extruded) {
          reg.recolor.push({ mat: material, base: fill.color, real: '#d6cfc1' });
        }
        mesh.castShadow = mesh.receiveShadow = true;
        if (item.highlighted) targetBox.expandByObject(mesh);
        if (extruded) { buildings++; maxHeight = Math.max(maxHeight, alt); }
      } else if (item.kind === 'line') {
        const pts = item.points.map((c) => new THREE.Vector3(east(c), c.alt + 0.1, -north(c)));
        if (item.width > 0) {
          const road = roadRibbon(pts, item.width, item.target ? 0.14 : 0.1,
            (item.style.line && item.style.line.color) || (item.target ? '#ffd54f' : '#8795a3'));
          road.userData = { name: item.name, description: item.description, height: 0 };
          layers.roads.add(road);
          pickables.push(road);
          reg.roads.push({ mat: road.material, base: '#' + road.material.color.getHexString(), real: item.target ? '#6c7178' : '#474c52' });
          roads++;
          if (item.name && !/^(Strada|Percorso|Pista)/.test(item.name)) {
            const len = pts.reduce((sum, p, i) => (i ? sum + p.distanceTo(pts[i - 1]) : 0), 0);
            const best = labelled.get(item.name);
            if (!best || len > best.len) labelled.set(item.name, { len, pts, target: item.target });
          }
        } else {
          layers.roads.add(new THREE.Line(
            new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color: edge.color })));
        }
      } else if (item.kind === 'point') {
        const pole = new THREE.Mesh(
          new THREE.CylinderGeometry(0.4, 0.4, 14, 10), new THREE.MeshLambertMaterial({ color: 0xe53935 }));
        pole.position.set(east(item.point), 7, -north(item.point));
        pole.userData = { name: item.name, description: item.description, height: 0 };
        layers.buildings.add(pole);
        pickables.push(pole);
      }
    }

    for (const [name, { pts, target }] of labelled) layers.labels.add(roadLabel(name, pts, target));
    const framed = layers.buildings.children.length ? layers.buildings : content;
    const box = new THREE.Box3().setFromObject(framed);
    const size = box.getSize(new THREE.Vector3());
    const extent = Math.max(size.x, size.z, 40);
    const grid = new THREE.GridHelper(Math.ceil(extent * 1.6 / 10) * 10, Math.ceil(extent * 1.6 / 10), 0x5c6b78, 0x34424e);
    content.add(grid);
    reg.grid = grid;
    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(extent * 8, extent * 8).rotateX(-Math.PI / 2),
      new THREE.MeshStandardMaterial({ color: 0x8d9a86, roughness: 1 }));
    ground.position.y = -0.06;
    ground.receiveShadow = true;
    content.add(ground);
    reg.ground = ground;
    sceneExtent = extent;
    const half = extent * 0.9;
    Object.assign(sun.shadow.camera, { left: -half, right: half, top: half, bottom: -half, near: 1, far: extent * 6 });
    sun.shadow.camera.updateProjectionMatrix();
    sun.position.set(-0.55 * extent, 1.1 * extent, 0.8 * extent);
    sun.target.position.set(0, 0, 0);
    applyLayerState();
    applyMode();
    cam.target.set(0, Math.min(maxHeight / 3, 10), 0);
    cam.radius = extent * 1.5;
    cam.azimuth = -0.6;
    cam.polar = 1.0;
    cam.home = { radius: cam.radius, azimuth: cam.azimuth, polar: cam.polar };
    camera.far = extent * 20;
    camera.updateProjectionMatrix();
    placeCamera();
    showLegend(buildings, roads, maxHeight, extent);
    clearInfo();
  }

  // ---- UI ----------------------------------------------------------------------------------------------------------
  function showLegend(buildings, roads, maxHeight, extent) {
    if (!legendEl) return;
    legendEl.textContent = buildings + ' volumi · altezza massima ' + maxHeight.toFixed(0) + ' m · ' + roads
      + ' tratti stradali · estensione ~' + extent.toFixed(0) + ' m';
  }

  function clearInfo() {
    if (infoEl) infoEl.textContent = 'Clicca un edificio o una strada per vedere nome, altezza e attributi.';
  }

  function select(mesh) {
    if (selected && selected.material.emissive) selected.material.emissive.setHex(0x000000);
    selected = mesh;
    if (!mesh) { clearInfo(); dirty = true; return; }
    if (mesh.material.emissive) mesh.material.emissive.setHex(0x553300);
    const d = mesh.userData;
    if (infoEl) {
      infoEl.textContent = '';
      const title = document.createElement('strong');
      title.textContent = d.name || 'Senza nome';
      const body = document.createElement('div');
      body.className = 'kmz-info-body';
      body.textContent = (d.height ? 'Altezza: ' + d.height.toFixed(1) + ' m\n' : '') + (d.description || '');
      infoEl.append(title, body);
    }
    dirty = true;
  }

  // ---- controls: drag = rotate, shift/right-drag = pan, wheel/pinch = zoom ------------------------------------------
  const pointers = new Map();
  let downAt = null;
  let pinch = 0;
  const canvasHost = stage;
  stage.addEventListener('contextmenu', (e) => e.preventDefault());
  stage.addEventListener('pointerdown', (e) => {
    stage.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    downAt = { x: e.clientX, y: e.clientY, moved: false };
    pinch = pointers.size === 2 ? pointerGap() : 0;
  });
  stage.addEventListener('pointermove', (e) => {
    const prev = pointers.get(e.pointerId);
    if (!prev) return;
    const dx = e.clientX - prev.x;
    const dy = e.clientY - prev.y;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (downAt && Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y) > 4) downAt.moved = true;
    if (pointers.size === 2) {
      const gap = pointerGap();
      if (pinch) cam.radius *= pinch / gap;
      pinch = gap;
    } else if (e.shiftKey || e.buttons === 2) {
      const scale = cam.radius * 0.0016;
      const right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0);
      const forward = new THREE.Vector3(-Math.sin(cam.azimuth), 0, -Math.cos(cam.azimuth));
      cam.target.addScaledVector(right, -dx * scale).addScaledVector(forward, dy * scale);
    } else {
      cam.azimuth -= dx * 0.006;
      cam.polar -= dy * 0.006;
    }
    placeCamera();
  });
  const release = (e) => {
    pointers.delete(e.pointerId);
    pinch = 0;
    if (e.type === 'pointerup' && downAt && !downAt.moved && pointers.size === 0) pick(e);
  };
  stage.addEventListener('pointerup', release);
  stage.addEventListener('pointercancel', release);
  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    cam.radius *= Math.exp(e.deltaY * 0.0012);
    placeCamera();
  }, { passive: false });

  function pointerGap() {
    const [a, b] = [...pointers.values()];
    return Math.hypot(a.x - b.x, a.y - b.y) || 1;
  }

  function pick(e) {
    const rect = stage.getBoundingClientRect();
    const ndc = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    const shown = (o) => { for (let p = o; p; p = p.parent) if (!p.visible) return false; return true; };
    const hit = raycaster.intersectObjects(pickables, false).find((h) => shown(h.object));
    select(hit ? hit.object : null);
  }

  // ---- loading -----------------------------------------------------------------------------------------------------
  async function loadBuffer(buffer, label) {
    setStatus('Elaborazione del modello…');
    try {
      if (buffer.byteLength > MAX_BYTES) throw new Error('File troppo grande (max 25 MB)');
      const items = parseKml(await readKmlText(buffer, label));
      build(items);
      setStatus('');
    } catch (err) {
      setStatus('Impossibile leggere il modello: ' + (err && err.message ? err.message : err));
    }
  }

  async function loadUrl(url) {
    setStatus('Download degli edifici da OpenStreetMap… (può richiedere qualche secondo)');
    try {
      const response = await fetch(url, { credentials: 'same-origin' });
      if (!response.ok) {
        let detail = '';
        try { detail = (await response.json()).detail || ''; } catch (e) { /* not JSON */ }
        throw new Error(detail || 'HTTP ' + response.status);
      }
      await loadBuffer(await response.arrayBuffer(), 'KMZ');
    } catch (err) {
      setStatus('Modello non disponibile: ' + (err && err.message ? err.message : err));
    }
  }

  function loop() {
    requestAnimationFrame(loop);
    if (dirty && renderer) { dirty = false; renderer.render(scene, camera); }
  }

  if (renderer) {
    renderer.domElement.className = 'kmz-canvas';
    stage.prepend(renderer.domElement);
    new ResizeObserver(resize).observe(stage);
    resize();
    loop();

    stage.addEventListener('dragover', (e) => { e.preventDefault(); stage.classList.add('is-dragover'); });
    stage.addEventListener('dragleave', () => stage.classList.remove('is-dragover'));
    stage.addEventListener('drop', (e) => {
      e.preventDefault();
      stage.classList.remove('is-dragover');
      const file = e.dataTransfer && e.dataTransfer.files[0];
      if (file) file.arrayBuffer().then((b) => loadBuffer(b, file.name));
    });

    window.kmzViewer = {
      reset() { if (cam.home) { Object.assign(cam, cam.home); cam.target.set(0, 5, 0); placeCamera(); } },
      realistic(on) { realisticOn = !!on; applyMode(); },
      layer(name, visible) { if (name in layerState) { layerState[name] = !!visible; applyLayerState(); } },
      focus(azimuthDeg, polarDeg) {
        if (targetBox.isEmpty()) return window.kmzViewer.reset();
        const centre = targetBox.getCenter(new THREE.Vector3());
        const size = targetBox.getSize(new THREE.Vector3());
        cam.target.copy(centre);
        cam.radius = Math.max(size.x, size.y, size.z) * 2.4;
        cam.azimuth = ((Number.isFinite(azimuthDeg) ? azimuthDeg : 40) * Math.PI) / 180;
        cam.polar = ((Number.isFinite(polarDeg) ? polarDeg : 55) * Math.PI) / 180;
        placeCamera();
      },
      top() { cam.polar = 0.02; cam.azimuth = 0; placeCamera(); },
      openFile(input) {
        const file = input && input.files && input.files[0];
        if (file) file.arrayBuffer().then((b) => loadBuffer(b, file.name));
      },
    };
    if (stage.dataset.kmzUrl) loadUrl(stage.dataset.kmzUrl);
    else setStatus('Trascina qui un file KMZ o KML, oppure sceglilo con il pulsante.');
  }
}
