/* The shared theme's inline preload handler can miss a fast or cached load. Apply
 * the already-preloaded stylesheet from this deferred SISTER script as a fallback. */
(function () {
  var link = document.getElementById('theme-fonts-link');
  if (!link || link.rel !== 'preload' || link.as !== 'style') return;

  link.rel = 'stylesheet';
  link.removeAttribute('as');
})();
