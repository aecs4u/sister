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

    document.addEventListener('click', function (event) {
        var button = event.target.closest('[data-aecs4u-share]');
        if (!button) return;

        var title = button.dataset.shareTitle || document.title;
        var data = {title: title, text: title, url: window.location.href};
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
