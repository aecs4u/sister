/**
 * Sign-in / sign-out behaviour for sister/templates/auth_login.html and auth_logout.html.
 * The forms are real <form method="post"> elements; this only upgrades them (no credentials ever go in the URL).
 */
(function () {
  'use strict';

  function show(box, message) {
    box.textContent = message;
    box.classList.remove('d-none');
    box.focus();
  }

  function busy(button, on) {
    button.disabled = on;
    var text = button.querySelector('.btn-text');
    var spinner = button.querySelector('.btn-spinner');
    if (text && spinner) {
      text.classList.toggle('d-none', on);
      spinner.classList.toggle('d-none', !on);
    }
  }

  function failureMessage(response) {
    if (response.status === 429) {
      var wait = parseInt(response.headers.get('Retry-After') || '60', 10);
      return 'Troppi tentativi. Riprova tra ' + wait + ' secondi.';
    }
    if (response.status === 401 || response.status === 403) return 'Nome utente o password non corretti.';
    return 'Accesso non riuscito (' + response.status + '). Riprova più tardi.';
  }

  var login = document.getElementById('loginForm');
  if (login) {
    var errorBox = document.getElementById('loginError');
    var submit = document.getElementById('loginSubmit');
    var password = document.getElementById('password');
    var toggle = document.getElementById('togglePassword');

    toggle.addEventListener('click', function () {
      var reveal = password.type === 'password';
      password.type = reveal ? 'text' : 'password';
      toggle.setAttribute('aria-pressed', reveal ? 'true' : 'false');
      toggle.setAttribute('aria-label', reveal ? 'Nascondi la password' : 'Mostra la password');
      toggle.querySelector('i').className = reveal ? 'fas fa-eye-slash' : 'fas fa-eye';
    });

    login.addEventListener('submit', function (event) {
      event.preventDefault();
      errorBox.classList.add('d-none');
      var username = login.elements.username.value.trim();
      if (!username || !password.value) {
        show(errorBox, 'Inserisci nome utente e password.');
        (username ? password : login.elements.username).focus();
        return;
      }
      busy(submit, true);
      fetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ username: username, password: password.value, next: login.elements.next.value }),
        credentials: 'same-origin',
      })
        .then(function (response) {
          if (!response.ok) throw response;
          return response.json();
        })
        .then(function (data) {
          window.location.assign(data.redirect_url || login.elements.next.value || '/web/');
        })
        .catch(function (failure) {
          busy(submit, false);
          password.value = '';
          show(errorBox, failure && failure.status ? failureMessage(failure) : 'Connessione non riuscita. Riprova.');
          password.focus();
          errorBox.focus();
        });
    });
  }

  var logout = document.getElementById('logoutForm');
  if (logout) {
    var logoutError = document.getElementById('logoutError');
    logout.addEventListener('submit', function (event) {
      event.preventDefault();
      var button = document.getElementById('logoutSubmit');
      button.disabled = true;
      fetch('/auth/logout', { method: 'POST', headers: { Accept: 'application/json' }, credentials: 'same-origin' })
        .then(function (response) {
          if (!response.ok) throw response;
          window.location.assign('/auth/login');
        })
        .catch(function () {
          button.disabled = false;
          show(logoutError, 'Uscita non riuscita. Riprova.');
        });
    });
  }
})();
