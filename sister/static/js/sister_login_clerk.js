/**
 * "Accedi con Google" button of sister/templates/auth_login.html (Clerk SSO next to the local accounts).
 * clerk-js itself is loaded by aecs4u-theme's auth_base.html; the signed-in session is created by aecs4u-auth's
 * GET /auth/clerk/callback, which exchanges the Clerk session for SISTER's own cookie.
 * Expects window.SisterLoginClerk = { nextUrl: "/", callbackUrl: "/auth/clerk/callback" }.
 */
(function () {
  'use strict';

  var cfg = window.SisterLoginClerk || {};
  var button = document.getElementById('clerkSignIn');
  if (!button) return;
  var label = button.querySelector('.btn-text');
  var spinner = button.querySelector('.btn-spinner');
  var errorBox = document.getElementById('loginError');
  var idleLabel = label ? label.textContent : '';

  function state(disabled, text, loading) {
    button.disabled = disabled;
    if (label) label.textContent = text;
    if (spinner) spinner.classList.toggle('d-none', !loading);
  }

  function fail(message) {
    state(false, idleLabel, false);
    if (errorBox) {
      errorBox.textContent = message;
      errorBox.classList.remove('d-none');
      errorBox.focus();
    }
  }

  function waitForClerk(attempts, delayMs) {
    return new Promise(function (resolve) {
      var tries = 0;
      (function check() {
        if (window.Clerk) return resolve(window.Clerk);
        if (++tries >= attempts) return resolve(null);
        setTimeout(check, delayMs);
      })();
    });
  }

  function callbackUrl() {
    return (cfg.callbackUrl || '/auth/clerk/callback') + '?next=' + encodeURIComponent(cfg.nextUrl || '/');
  }

  button.addEventListener('click', function () {
    if (errorBox) errorBox.classList.add('d-none');
    state(true, 'Apertura accesso Google…', true);
    waitForClerk(100, 100)
      .then(function (clerk) {
        if (!clerk) throw new Error('Clerk non disponibile');
        return Promise.resolve(clerk.loaded ? null : clerk.load()).then(function () {
          if (clerk.user) {
            state(true, 'Reindirizzamento…', true);
            window.location.replace(callbackUrl());
            return null;
          }
          return clerk.redirectToSignIn({
            signInForceRedirectUrl: callbackUrl(),
            signUpForceRedirectUrl: callbackUrl(),
          });
        });
      })
      .catch(function (error) {
        console.error('Clerk sign-in error:', error);
        fail('Accesso con Google non disponibile. Usa nome utente e password.');
      });
  });
})();
