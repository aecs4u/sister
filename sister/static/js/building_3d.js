/**
 * 3D building view: Google Maps JavaScript API "Photorealistic 3D Maps" (<gmp-map-3d>), the data behind Google Earth.
 * Reads its settings from #building3dStage data-attributes (no inline script, see CSP notes in CLAUDE.md) and exposes
 * window.building3d.{orbit,stop,reset,toggleMode} for the data-click buttons (static/js/sister_actions.js).
 */
(function () {
  'use strict';

  var stage = document.getElementById('building3dStage');
  if (!stage) return;
  var status = document.getElementById('building3dStatus');
  var lat = parseFloat(stage.dataset.lat);
  var lng = parseFloat(stage.dataset.lng);
  var CAMERA = { center: { lat: lat, lng: lng, altitude: 20 }, range: 220, tilt: 62, heading: 0 };
  var map = null;
  var hybrid = true;

  function fail(message) {
    if (status) status.textContent = message;
  }

  function applyCamera(map3d) {
    map3d.center = CAMERA.center;
    map3d.range = CAMERA.range;
    map3d.tilt = CAMERA.tilt;
    map3d.heading = CAMERA.heading;
  }

  async function init() {
    var maps3d;
    try {
      maps3d = await google.maps.importLibrary('maps3d');
    } catch (err) {
      fail('Libreria Google Maps 3D non disponibile (chiave non valida o API non abilitata).');
      return;
    }
    map = new maps3d.Map3DElement({ mode: 'HYBRID' });
    applyCamera(map);
    var marker = new maps3d.Marker3DElement({
      position: { lat: lat, lng: lng, altitude: 0 },
      altitudeMode: 'RELATIVE_TO_GROUND',
      extruded: true,
      label: stage.dataset.label.split(',')[0]
    });
    map.append(marker);
    stage.appendChild(map);
    if (status) status.remove();
    // The element does not report a missing key/API through the promise: surface it when no tiles arrive.
    map.addEventListener('gmp-error', function () {
      fail('Google Maps 3D ha restituito un errore (verifica chiave e restrizioni).');
    });
  }

  function load() {
    var script = document.createElement('script');
    script.async = true;
    script.nonce = stage.dataset.nonce;
    script.src = 'https://maps.googleapis.com/maps/api/js?' + new URLSearchParams({
      key: stage.dataset.apiKey, v: 'beta', loading: 'async', libraries: 'maps3d', callback: 'building3dReady'
    });
    script.onerror = function () { fail('Impossibile caricare Google Maps (rete o restrizioni di sicurezza).'); };
    window.building3dReady = init;
    document.head.appendChild(script);
  }

  window.building3d = {
    orbit: function () {
      if (map && map.flyCameraAround) map.flyCameraAround({ camera: CAMERA, durationMillis: 30000, rounds: 1 });
    },
    stop: function () {
      if (map && map.stopCameraAnimation) map.stopCameraAnimation();
    },
    reset: function () {
      if (!map) return;
      if (map.stopCameraAnimation) map.stopCameraAnimation();
      applyCamera(map);
    },
    toggleMode: function () {
      if (!map) return;
      hybrid = !hybrid;
      map.mode = hybrid ? 'HYBRID' : 'SATELLITE';
    }
  };

  load();
})();
