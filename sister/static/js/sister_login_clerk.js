/**
 * Clerk's embedded sign-in widget on sister/templates/auth_login.html (same behaviour as OpenData's login).
 * clerk-js and its UI bundle are loaded by the page; once signed in, aecs4u-auth's POST /auth/clerk/session turns the
 * Clerk session into SISTER's own cookie. Configuration comes from data attributes of #sisterLoginClerk (no inline
 * script, so the CSP nonce is not needed); every message is set with textContent.
 */
(function () {
  'use strict';

  var cfg = document.getElementById('sisterLoginClerk');
  var statusEl = document.getElementById('clerk-status');
  var mountEl = document.getElementById('clerk-sign-in');
  if (!cfg || !statusEl || !mountEl) return;

  var nextUrl = cfg.dataset.nextUrl || '/';
  var callbackUrl = cfg.dataset.callbackUrl || '/auth/clerk/callback';
  var sessionUrl = cfg.dataset.sessionUrl || '/auth/clerk/session';

  function say(cls, text) {
    statusEl.className = 'text-center small mb-2 ' + cls;
    statusEl.textContent = text;
  }

  function waitForGlobals(max, ms) {
    return new Promise(function (resolve) {
      var tries = 0;
      (function check() {
        tries += 1;
        if (window.Clerk && window.__internal_ClerkUICtor) {
          resolve({ Clerk: window.Clerk, ClerkUI: window.__internal_ClerkUICtor });
        } else if (tries >= max) {
          resolve(null);
        } else {
          setTimeout(check, ms);
        }
      })();
    });
  }

  function showSignedIn(Clerk) {
    var user = Clerk.user;
    var who = user.fullName || (user.primaryEmailAddress && user.primaryEmailAddress.emailAddress) || '';
    var info = document.createElement('p');
    info.className = 'mb-2';
    info.textContent = 'Sessione attiva come ' + who;
    var go = document.createElement('a');
    go.className = 'btn btn-primary btn-sm me-2';
    go.href = nextUrl;
    go.textContent = 'Continua';
    var other = document.createElement('button');
    other.type = 'button';
    other.className = 'btn btn-outline-secondary btn-sm';
    other.textContent = 'Cambia account';
    other.addEventListener('click', function () {
      Clerk.signOut().then(function () { window.location.reload(); });
    });
    statusEl.className = 'text-center mb-2';
    statusEl.replaceChildren(info, go, other);
  }

  waitForGlobals(120, 100).then(function (globals) {
    if (!globals) return say('text-warning', 'Accesso con Google non disponibile — usa email e password');
    return Promise.resolve(globals.Clerk.load({ ui: { ClerkUI: globals.ClerkUI } }))
      .then(function () {
        var Clerk = globals.Clerk;
        if (Clerk.user) {
          var token = Clerk.session ? Clerk.session.getToken() : Promise.resolve(null);
          return Promise.resolve(token)
            .then(function (value) {
              if (!value) return null;
              return fetch(sessionUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + value },
                body: JSON.stringify({ token: value }),
                credentials: 'same-origin',
              });
            })
            .then(function () { showSignedIn(Clerk); });
        }
        statusEl.style.display = 'none';
        return Clerk.mountSignIn(mountEl, {
          fallbackRedirectUrl: callbackUrl + '?next=' + encodeURIComponent(nextUrl),
        });
      });
  }).catch(function (error) {
    console.error('Clerk init failed', error);
    say('text-warning', 'Accesso con Google non disponibile — usa email e password');
  });
})();
