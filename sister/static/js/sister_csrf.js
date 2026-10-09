/**
 * Adds the session CSRF token to every same-origin state-changing fetch()/XHR made by the page
 * (the token comes from <meta name="csrf-token">; see aecs4u_auth.middleware.CSRFMiddleware).
 */
(function () {
  'use strict';
  var meta = document.querySelector('meta[name="csrf-token"]');
  var token = meta && meta.getAttribute('content');
  if (!token) return;

  var UNSAFE = /^(POST|PUT|PATCH|DELETE)$/i;
  function sameOrigin(url) {
    try { return new URL(url, window.location.href).origin === window.location.origin; } catch (e) { return false; }
  }

  var nativeFetch = window.fetch;
  window.fetch = function (input, init) {
    var method = (init && init.method) || (input && input.method) || 'GET';
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    if (UNSAFE.test(method) && sameOrigin(url)) {
      init = Object.assign({}, init || {});
      var headers = new Headers(init.headers || (input && input.headers) || {});
      if (!headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', token);
      init.headers = headers;
    }
    return nativeFetch.call(this, input, init);
  };

  var nativeOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    this._sisterCsrf = UNSAFE.test(method) && sameOrigin(url);
    return nativeOpen.apply(this, arguments);
  };
  var nativeSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () {
    if (this._sisterCsrf) this.setRequestHeader('X-CSRF-Token', token);
    return nativeSend.apply(this, arguments);
  };
})();
