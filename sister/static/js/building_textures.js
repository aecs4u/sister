/**
 * Procedural textures for the "Vista realistica" mode of the KMZ viewer (building_kmz_viewer.js).
 *
 * Everything is drawn on <canvas> from a small palette (the `facade` entry of sister/building_overrides.json), so no
 * photograph, no third-party imagery and no network request is involved. The facade layout (brick panels, cream band at
 * every floor, dark plinth with garages, windows in cream columns, balconies) is an interpretation of what the building
 * looks like, not a picture of it.
 */
import * as THREE from '/static/vendor/three-0.160.1/three.module.min.js';

export const DEFAULT_FACADE = {
  brick: '#c49a72', band: '#e8dcb0', plinth: '#5a463f', roof: '#b5573a',
  window_bay_m: 3.3, garage_doors: true, balconies: true,
};

function rng(seed) {
  let s = (seed >>> 0) || 1;
  return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; };
}

function toTexture(canvas, repeat) {
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 8;
  if (repeat) {
    texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
    texture.repeat.set(repeat, repeat);
  }
  return texture;
}

function shade(hex, factor) {
  const c = new THREE.Color(hex);
  return '#' + c.multiplyScalar(factor).getHexString();
}

/** One wall: length x height metres, `floors` storeys; `street` walls get garages and balconies. */
export function makeWallTexture({ length, height, floors, facade, street, seed }) {
  const f = { ...DEFAULT_FACADE, ...(facade || {}) };
  const ppm = Math.max(10, Math.min(44, 2048 / Math.max(length, height)));
  const w = Math.max(8, Math.ceil(length * ppm));
  const h = Math.max(8, Math.ceil(height * ppm));
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const g = canvas.getContext('2d');
  const rand = rng(seed * 7919 + 13);
  const Y = (m) => h - m * ppm; // metres above the ground -> canvas y
  const floorH = height / floors;
  const plinthH = Math.min(2.3, floorH * 0.78);

  // Brick panels: courses of 7.75 cm, bricks of 24 cm, each with its own faint tint.
  g.fillStyle = f.brick;
  g.fillRect(0, 0, w, h);
  const course = 0.0775;
  const brick = 0.24;
  for (let y = 0; y < height; y += course) {
    const offset = (Math.round(y / course) % 2) * brick / 2;
    for (let x = -offset; x < length; x += brick) {
      const t = rand();
      g.fillStyle = t < 0.5 ? 'rgba(255,238,205,' + (0.06 * rand()).toFixed(3) + ')' : 'rgba(60,30,10,' + (0.1 * rand()).toFixed(3) + ')';
      g.fillRect(x * ppm, Y(y + course), brick * ppm, course * ppm);
    }
    g.fillStyle = 'rgba(70,45,30,0.2)';
    g.fillRect(0, Y(y) - 1, w, 1);
  }

  // Cream columns that carry the windows, then the cream band at every floor line and the cornice.
  const margin = 0.8;
  const bays = Math.max(1, Math.round((length - 2 * margin) / f.window_bay_m));
  const bayW = (length - 2 * margin) / bays;
  g.fillStyle = f.band;
  for (let b = 0; b < bays; b++) {
    const cx = margin + bayW * (b + 0.5);
    g.fillRect((cx - 0.8) * ppm, Y(height - 0.3), 1.6 * ppm, (height - 0.3 - plinthH) * ppm);
  }
  for (let k = 1; k <= floors; k++) {
    const top = k === floors;
    const bandH = top ? 0.7 : 0.45;
    const y = top ? height : k * floorH + 0.22;
    g.fillStyle = f.band;
    g.fillRect(0, Y(y), w, bandH * ppm);
    g.fillStyle = 'rgba(0,0,0,0.18)';
    g.fillRect(0, Y(y - bandH) - 1, w, 2);
  }

  // Dark plinth.
  g.fillStyle = f.plinth;
  g.fillRect(0, Y(plinthH), w, plinthH * ppm);
  g.fillStyle = 'rgba(0,0,0,0.12)';
  for (let y = 0.2; y < plinthH; y += 0.35) g.fillRect(0, Y(y), w, 1);

  const windowAt = (cx, yBottom, ww, wh, shutter) => {
    g.fillStyle = '#ecebe5';
    g.fillRect((cx - ww / 2 - 0.07) * ppm, Y(yBottom + wh + 0.07), (ww + 0.14) * ppm, (wh + 0.14) * ppm);
    const gradient = g.createLinearGradient(0, Y(yBottom + wh), 0, Y(yBottom));
    gradient.addColorStop(0, '#7f93a6');
    gradient.addColorStop(1, '#2d3a47');
    g.fillStyle = gradient;
    g.fillRect((cx - ww / 2) * ppm, Y(yBottom + wh), ww * ppm, wh * ppm);
    g.fillStyle = '#ecebe5';
    g.fillRect((cx - 0.02) * ppm, Y(yBottom + wh), 0.04 * ppm, wh * ppm);
    if (shutter > 0) {
      g.fillStyle = '#9aa0a4';
      const drop = wh * shutter;
      g.fillRect((cx - ww / 2) * ppm, Y(yBottom + wh), ww * ppm, drop * ppm);
      g.fillStyle = 'rgba(0,0,0,0.25)';
      for (let s = 0; s < drop; s += 0.05) g.fillRect((cx - ww / 2) * ppm, Y(yBottom + wh - s), ww * ppm, 1);
    }
  };

  for (let b = 0; b < bays; b++) {
    const cx = margin + bayW * (b + 0.5);
    // Ground floor: garage doors on street walls, a small barred window otherwise.
    if (street && f.garage_doors) {
      g.fillStyle = '#d8cfbd';
      g.fillRect((cx - 1.45) * ppm, Y(2.35), 2.9 * ppm, 2.35 * ppm);
      g.fillStyle = '#4b3326';
      g.fillRect((cx - 1.3) * ppm, Y(2.2), 2.6 * ppm, 2.2 * ppm);
      g.fillStyle = 'rgba(0,0,0,0.3)';
      for (let y = 0.15; y < 2.2; y += 0.2) g.fillRect((cx - 1.3) * ppm, Y(y), 2.6 * ppm, 1);
    } else {
      windowAt(cx, 1.0, 0.8, 0.9, 0);
    }
    for (let k = 1; k < floors; k++) {
      const floorY = k * floorH;
      const balcony = street && f.balconies && b % 2 === 0;
      if (balcony) {
        windowAt(cx, floorY + 0.1, 1.0, 2.0, rand() < 0.35 ? 0.3 : 0);
        g.fillStyle = '#cfc9bb';
        g.fillRect((cx - 1.15) * ppm, Y(floorY + 0.05), 2.3 * ppm, 0.22 * ppm);
        g.fillStyle = 'rgba(20,20,20,0.28)';
        g.fillRect((cx - 1.15) * ppm, Y(floorY + 1.15), 2.3 * ppm, 1.1 * ppm);
        g.strokeStyle = '#2b2b2b';
        g.lineWidth = Math.max(1, ppm * 0.03);
        g.strokeRect((cx - 1.15) * ppm, Y(floorY + 1.15), 2.3 * ppm, 1.1 * ppm);
        for (let x = -1.1; x <= 1.1; x += 0.14) {
          g.beginPath();
          g.moveTo((cx + x) * ppm, Y(floorY + 1.15));
          g.lineTo((cx + x) * ppm, Y(floorY + 0.05));
          g.stroke();
        }
      } else {
        windowAt(cx, floorY + 0.95, 1.1, 1.35, rand() < 0.3 ? 0.35 : 0);
      }
    }
  }

  // Weathering: soot at the foot, faint vertical streaks.
  const foot = g.createLinearGradient(0, Y(0), 0, Y(1.6));
  foot.addColorStop(0, 'rgba(0,0,0,0.28)');
  foot.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = foot;
  g.fillRect(0, Y(1.6), w, 1.6 * ppm);
  for (let i = 0; i < Math.round(length * 2); i++) {
    g.fillStyle = 'rgba(40,30,20,' + (0.02 + 0.04 * rand()).toFixed(3) + ')';
    g.fillRect(rand() * w, rand() * h * 0.4, 1 + rand() * 3, h * (0.3 + 0.6 * rand()));
  }
  return toTexture(canvas, 0);
}

/** Terracotta pantiles; the texture covers 2 x 2 m and repeats (UVs are in metres, repeat 0.5). */
export function makeRoofTexture(color) {
  const size = 256;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const g = canvas.getContext('2d');
  const rand = rng(4242);
  const rows = 10;
  const cols = 12;
  const rowH = size / rows;
  const colW = size / cols;
  g.fillStyle = color;
  g.fillRect(0, 0, size, size);
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const tint = 0.82 + 0.3 * rand();
      const gradient = g.createLinearGradient(c * colW, 0, (c + 1) * colW, 0);
      gradient.addColorStop(0, shade(color, tint * 0.78));
      gradient.addColorStop(0.5, shade(color, tint * 1.12));
      gradient.addColorStop(1, shade(color, tint * 0.8));
      g.fillStyle = gradient;
      g.fillRect(c * colW, r * rowH, colW, rowH);
    }
    g.fillStyle = 'rgba(30,10,0,0.38)';
    g.fillRect(0, (r + 1) * rowH - 3, size, 3);
    g.fillStyle = 'rgba(255,220,180,0.16)';
    g.fillRect(0, r * rowH, size, 1);
  }
  return toTexture(canvas, 0.5);
}

/** Plain lime plaster for gable ends. */
export function makePlasterTexture(color) {
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const g = canvas.getContext('2d');
  const rand = rng(99);
  g.fillStyle = color;
  g.fillRect(0, 0, size, size);
  for (let i = 0; i < 900; i++) {
    g.fillStyle = rand() < 0.5 ? 'rgba(255,255,255,0.07)' : 'rgba(80,60,30,0.06)';
    g.fillRect(rand() * size, rand() * size, 1 + rand() * 3, 1 + rand() * 3);
  }
  return toTexture(canvas, 0.5);
}
