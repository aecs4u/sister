(function () {
    'use strict';

    async function copyLink(button, data) {
        try {
            await navigator.clipboard.writeText(data.url);
        } catch (error) {
            window.prompt('Copia questo link per condividerlo:', data.url);
            return;
        }

        var originalMarkup = button.innerHTML;
        var originalTitle = button.title;
        button.textContent = 'Link copiato';
        button.title = 'Link copiato';
        button.setAttribute('aria-label', 'Link copiato');
        window.setTimeout(function () {
            button.innerHTML = originalMarkup;
            button.title = originalTitle;
            button.removeAttribute('aria-label');
        }, 1800);
    }

    // Saves the report to the company Drive (shared with the recipient only) and opens a Gmail draft with the link.
    async function shareViaDrive(button, data) {
        var recipient = (window.prompt('Indirizzo email del destinatario (il report su Google Drive sarà condiviso solo con questo indirizzo):') || '').trim();
        if (!recipient) return;
        var composer = window.open('', '_blank');  // opened now, inside the click, so the popup is not blocked
        var originalMarkup = button.innerHTML;
        button.disabled = true;
        button.textContent = 'Salvataggio su Drive…';
        try {
            var response = await fetch(button.dataset.driveShareUrl, {
                method: 'POST', credentials: 'same-origin',
                headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                body: JSON.stringify({recipient: recipient})
            });
            var result = await response.json().catch(function () { return {}; });
            if (!response.ok || !result.url) throw new Error(result.detail || 'Salvataggio non riuscito');
            var subject = 'Report: ' + data.title;
            var text = 'Ciao,\n\nho condiviso con te il report su Google Drive (accesso solo con questo indirizzo email):\n'
                + result.url + '\n';
            var gmail = 'https://mail.google.com/mail/?view=cm&fs=1&to=' + encodeURIComponent(recipient)
                + '&su=' + encodeURIComponent(subject) + '&body=' + encodeURIComponent(text);
            if (composer) composer.location.href = gmail; else window.location.href = gmail;
        } catch (error) {
            if (composer) composer.close();
            window.alert('Impossibile condividere il report: ' + (error && error.message ? error.message : error));
        } finally {
            button.disabled = false;
            button.innerHTML = originalMarkup;
        }
    }

    document.addEventListener('click', function (event) {
        var button = event.target.closest('[data-aecs4u-share]');
        if (!button) return;

        var title = button.dataset.shareTitle || document.title;
        var data = {title: title, text: title, url: window.location.href};
        if (button.dataset.aecs4uShare === 'email' && button.dataset.driveShareUrl) {
            shareViaDrive(button, data);
            return;
        }
        if (button.dataset.aecs4uShare === 'email') {
            var body = 'Ciao,\n\npuoi consultare il documento qui: ' + data.url;
            window.location.href = 'mailto:?subject=' + encodeURIComponent('Visura: ' + title)
                + '&body=' + encodeURIComponent(body);
            return;
        }

        if (button.dataset.aecs4uShare !== 'native') return;
        if (navigator.share) {
            try {
                Promise.resolve(navigator.share(data)).catch(function (error) {
                    if (!error || error.name !== 'AbortError') copyLink(button, data);
                });
            } catch (error) {
                if (!error || error.name !== 'AbortError') copyLink(button, data);
            }
            return;
        }
        copyLink(button, data);
    });
})();
