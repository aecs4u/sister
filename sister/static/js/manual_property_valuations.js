// User-entered provider valuations and per-user preferred sources for cadastral properties.
(function () {
    'use strict';

    var root = document.querySelector('[data-manual-property-valuations-root]');
    var source = document.querySelector('[data-manual-property-valuations]');
    if (!root || !source) return;

    var PROVIDERS = {
        immobiliare: 'Immobiliare.it',
        idealista: 'idealista',
        other: 'Altro'
    };
    var entries;
    try { entries = JSON.parse(source.textContent || '[]'); } catch (error) { entries = []; }
    if (!Array.isArray(entries)) return;

    var cards = root.querySelector('[data-manual-property-valuation-items]');
    var errorBox = root.querySelector('[data-manual-valuation-error]');

    function node(parent, tag, className, value) {
        var element = document.createElement(tag);
        if (className) element.className = className;
        if (value !== undefined && value !== null) element.textContent = String(value);
        parent.appendChild(element);
        return element;
    }

    function propertyLabel(item) {
        var identity = item.identity || {};
        var kind = item.kind === 'lands' ? 'Terreno' : 'Fabbricato';
        var places = [identity.municipality, identity.province].filter(Boolean).join(' · ');
        var details = [];
        if (identity.section) details.push('Sez. ' + identity.section);
        if (identity.sheet) details.push('Fg. ' + identity.sheet);
        if (identity.parcel) details.push('Part. ' + identity.parcel);
        if (identity.subunit) details.push('Sub. ' + identity.subunit);
        return [kind, places, details.join(' · ')].filter(Boolean).join(' · ');
    }

    function showError(message) {
        errorBox.textContent = message;
        errorBox.hidden = false;
    }

    async function save(url, body) {
        var response = await fetch(url, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        var result = {};
        try { result = await response.json(); } catch (error) { /* the status below is enough */ }
        if (!response.ok) throw new Error(result.detail || 'Salvataggio non riuscito.');
        window.location.reload();
    }

    function addProviderRecord(list, valuation) {
        var row = node(list, 'li', 'mb-1');
        node(row, 'strong', '', valuation.provider_label + ': ');
        node(row, 'span', '', valuation.range);
        if (valuation.source_date) {
            var date = new Date(valuation.source_date + 'T00:00:00Z');
            node(row, 'span', 'text-muted small ms-2', 'rilevata il ' + date.toLocaleDateString('it-IT', {timeZone: 'UTC'}));
        }
        if (valuation.source_url) {
            var link = node(row, 'a', 'small ms-2', 'Apri fonte');
            link.href = valuation.source_url;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
        }
        if (valuation.notes) node(list, 'li', 'small text-muted ms-3 mb-2', valuation.notes);
    }

    function renderItem(item) {
        var disclosure = node(cards, 'details', 'col-12 border rounded p-3');
        var current = item.preferred_source || '';
        var preferred = (item.valuations || []).find(function (valuation) { return valuation.provider === current; });
        if (current === 'omi' && item.omi_available) {
            preferred = {provider_label: 'OMI', range: item.omi_range};
        }
        var summary = propertyLabel(item);
        if (preferred) summary += ' · ' + preferred.provider_label + ' · ' + preferred.range;
        if (item.omi_override) summary += ' · intervallo personalizzato ' + item.omi_override.range;
        node(disclosure, 'summary', 'fw-semibold', summary);
        var card = node(disclosure, 'div', 'pt-3');

        var choiceLabel = node(card, 'label', 'form-label small fw-semibold mb-1', 'Fonte preferita per questo bene');
        var select = node(card, 'select', 'form-select form-select-sm mb-2');
        choiceLabel.htmlFor = 'preferred-source-' + item.asset_key;
        select.id = choiceLabel.htmlFor;
        select.setAttribute('aria-label', 'Fonte preferita per ' + propertyLabel(item));
        var hasChoices = false;
        if (item.omi_available) {
            var omi = node(select, 'option', '', 'OMI · ' + item.omi_range);
            omi.value = 'omi';
            hasChoices = true;
        }
        (item.valuations || []).forEach(function (valuation) {
            if (!PROVIDERS[valuation.provider]) return;
            var option = node(select, 'option', '', PROVIDERS[valuation.provider] + ' · ' + valuation.range);
            option.value = valuation.provider;
            hasChoices = true;
        });
        if (!hasChoices) {
            var placeholder = node(select, 'option', '', 'Aggiungi una stima per scegliere una fonte');
            placeholder.value = '';
            placeholder.selected = true;
            select.disabled = true;
        } else {
            select.value = current;
            if (!select.value) select.selectedIndex = 0;
        }

        var chosen = node(card, 'p', 'mb-2', 'Valore selezionato: ' + (preferred ? preferred.range : 'nessuno'));
        chosen.dataset.preferredValuation = 'true';
        if (item.omi_central) {
            var centralDescription = item.omi_zone
                ? 'Valore centrale dalla zona OMI ' + item.omi_zone + ': ' + item.omi_central + ' · fascia pubblicata ' + item.omi_range + '.'
                : 'Valore centrale dalla mediana delle quotazioni OMI delle zone comunali: ' + item.omi_central + ' · fascia comunale ' + item.omi_range + '.';
            node(card, 'p', 'small text-muted mb-2', centralDescription);
        }

        if (item.omi_available) {
            var omiSection = node(card, 'div', 'border-top mt-3 pt-3');
            node(omiSection, 'div', 'small fw-semibold mb-1', 'Intervallo personalizzato per questo fabbricato');
            node(omiSection, 'p', 'small text-muted mb-2',
                'Fascia OMI originale: ' + item.omi_range + '. La fascia personalizzata alimenta il totale personalizzato qui sopra; il totale OMI originale resta invariato.');
            var omiForm = node(omiSection, 'form', 'row g-2 align-items-end');
            var omiMinWrap = node(omiForm, 'div', 'col-6 col-sm-4');
            var omiMinLabel = node(omiMinWrap, 'label', 'form-label small mb-1', 'Valore minimo (€)');
            var omiMinInput = node(omiMinWrap, 'input', 'form-control form-control-sm');
            omiMinInput.type = 'number';
            omiMinInput.name = 'omi_value_min';
            omiMinInput.min = '0.01';
            omiMinInput.step = '0.01';
            omiMinInput.required = true;
            omiMinInput.value = item.omi_override ? item.omi_override.value_min : item.omi_base_min;
            omiMinLabel.htmlFor = 'omi-value-min-' + item.asset_key;
            omiMinInput.id = omiMinLabel.htmlFor;

            var omiMaxWrap = node(omiForm, 'div', 'col-6 col-sm-4');
            var omiMaxLabel = node(omiMaxWrap, 'label', 'form-label small mb-1', 'Valore massimo (€)');
            var omiMaxInput = node(omiMaxWrap, 'input', 'form-control form-control-sm');
            omiMaxInput.type = 'number';
            omiMaxInput.name = 'omi_value_max';
            omiMaxInput.min = '0.01';
            omiMaxInput.step = '0.01';
            omiMaxInput.value = item.omi_override ? item.omi_override.value_max : item.omi_base_max;
            omiMaxLabel.htmlFor = 'omi-value-max-' + item.asset_key;
            omiMaxInput.id = omiMaxLabel.htmlFor;
            node(omiForm, 'div', 'col-12 small text-muted', 'Lascia il massimo vuoto per salvare un importo singolo.');

            var omiSaveWrap = node(omiForm, 'div', 'col-6 col-sm-3');
            var omiSave = node(omiSaveWrap, 'button', 'btn btn-sm btn-primary w-100', 'Salva intervallo');
            omiSave.type = 'submit';
            var omiResetWrap = node(omiForm, 'div', 'col-6 col-sm-3');
            var omiReset = node(omiResetWrap, 'button', 'btn btn-sm btn-outline-secondary w-100', 'Ripristina OMI');
            omiReset.type = 'button';
            omiReset.disabled = !item.omi_override;

            omiForm.addEventListener('submit', function (event) {
                event.preventDefault();
                if (!omiForm.reportValidity()) return;
                omiSave.disabled = true;
                omiReset.disabled = true;
                errorBox.hidden = true;
                save('/manual-property-valuations/' + encodeURIComponent(item.asset_key) + '/omi-override', {
                    value_min: omiMinInput.value,
                    value_max: omiMaxInput.value || null
                }).catch(function (error) {
                    omiSave.disabled = false;
                    omiReset.disabled = !item.omi_override;
                    showError(error.message);
                });
            });

            omiReset.addEventListener('click', function () {
                omiReset.disabled = true;
                omiSave.disabled = true;
                errorBox.hidden = true;
                save('/manual-property-valuations/' + encodeURIComponent(item.asset_key) + '/omi-override', {
                    value_min: null,
                    value_max: null
                }).catch(function (error) {
                    omiReset.disabled = false;
                    omiSave.disabled = false;
                    showError(error.message);
                });
            });
        }

        if (item.affrancation) {
            var affrancation = item.affrancation;
            var affrancationSection = node(card, 'div', 'border-top mt-3 pt-3');
            node(affrancationSection, 'div', 'small fw-semibold mb-1', 'Importo di affrancazione per l’intero bene');
            node(affrancationSection, 'p', 'small text-muted mb-2', affrancation.source_note + ' ' + affrancation.basis_note);
            var affrancationForm = node(affrancationSection, 'form', 'row g-2 align-items-end');
            var amountWrap = node(affrancationForm, 'div', 'col-12 col-sm-6 col-md-5');
            var amountLabel = node(amountWrap, 'label', 'form-label small mb-1', 'Importo (€)');
            var amountInput = node(amountWrap, 'input', 'form-control form-control-sm');
            amountInput.type = 'number';
            amountInput.name = 'affrancation_amount';
            amountInput.min = '0';
            amountInput.step = '0.01';
            amountInput.required = true;
            amountInput.value = affrancation.amount;
            amountLabel.htmlFor = 'affrancation-amount-' + item.asset_key;
            amountInput.id = amountLabel.htmlFor;

            var saveWrap = node(affrancationForm, 'div', 'col-6 col-sm-3');
            var saveAmount = node(saveWrap, 'button', 'btn btn-sm btn-primary w-100', 'Salva importo');
            saveAmount.type = 'submit';
            var resetWrap = node(affrancationForm, 'div', 'col-6 col-sm-3');
            var resetAmount = node(resetWrap, 'button', 'btn btn-sm btn-outline-secondary w-100', 'Ripristina stima');
            resetAmount.type = 'button';
            resetAmount.disabled = !affrancation.is_override;

            affrancationForm.addEventListener('submit', function (event) {
                event.preventDefault();
                if (!affrancationForm.reportValidity()) return;
                saveAmount.disabled = true;
                resetAmount.disabled = true;
                errorBox.hidden = true;
                save('/manual-property-valuations/' + encodeURIComponent(item.asset_key) + '/affrancation', {
                    amount: amountInput.value
                }).catch(function (error) {
                    saveAmount.disabled = false;
                    resetAmount.disabled = !affrancation.is_override;
                    showError(error.message);
                });
            });

            resetAmount.addEventListener('click', function () {
                resetAmount.disabled = true;
                saveAmount.disabled = true;
                errorBox.hidden = true;
                save('/manual-property-valuations/' + encodeURIComponent(item.asset_key) + '/affrancation', {
                    amount: null
                }).catch(function (error) {
                    resetAmount.disabled = false;
                    saveAmount.disabled = false;
                    showError(error.message);
                });
            });
        }

        if ((item.valuations || []).length) {
            node(card, 'div', 'small fw-semibold', 'Stime manuali salvate');
            var list = node(card, 'ul', 'small mb-2 ps-3');
            item.valuations.forEach(function (valuation) { addProviderRecord(list, valuation); });
        } else {
            node(card, 'p', 'small text-muted mb-2', 'Non hai ancora inserito una stima esterna per questo bene.');
        }
        node(card, 'div', 'small text-muted mb-2', 'I valori inseriti sono privati del tuo account e non vengono importati dal portale.');

        select.addEventListener('change', function () {
            if (!select.value) return;
            save('/manual-property-valuations/' + encodeURIComponent(item.asset_key) + '/preference', {source: select.value})
                .catch(function (error) { showError(error.message); });
        });

        var details = node(card, 'details', 'mt-2');
        node(details, 'summary', 'small fw-semibold', 'Aggiungi o aggiorna una stima');
        var form = node(details, 'form', 'row g-2 mt-1');
        form.noValidate = false;

        var providerWrap = node(form, 'div', 'col-12 col-md-4');
        var providerLabel = node(providerWrap, 'label', 'form-label small mb-1', 'Fonte');
        var provider = node(providerWrap, 'select', 'form-select form-select-sm');
        provider.required = true;
        providerLabel.htmlFor = 'provider-' + item.asset_key;
        provider.id = providerLabel.htmlFor;
        Object.keys(PROVIDERS).forEach(function (key) {
            var option = node(provider, 'option', '', PROVIDERS[key]);
            option.value = key;
        });

        function addInput(label, type, name, wrapperClass) {
            var wrapper = node(form, 'div', wrapperClass);
            var inputLabel = node(wrapper, 'label', 'form-label small mb-1', label);
            var input = node(wrapper, 'input', 'form-control form-control-sm');
            input.type = type;
            input.name = name;
            inputLabel.htmlFor = name + '-' + item.asset_key;
            input.id = inputLabel.htmlFor;
            return input;
        }

        var minimum = addInput('Valore minimo (€)', 'number', 'value_min', 'col-6 col-md-4');
        minimum.required = true;
        minimum.min = '0.01';
        minimum.step = '0.01';
        var maximum = addInput('Valore massimo (€)', 'number', 'value_max', 'col-6 col-md-4');
        maximum.min = '0.01';
        maximum.step = '0.01';
        var sourceDate = addInput('Data rilevazione', 'date', 'source_date', 'col-6 col-md-4');
        var url = addInput('Link annuncio o fonte', 'url', 'source_url', 'col-12 col-md-8');
        url.maxLength = 2000;
        var notes = addInput('Note', 'text', 'notes', 'col-12 col-md-4');
        notes.maxLength = 1000;

        var submitWrap = node(form, 'div', 'col-12');
        var submit = node(submitWrap, 'button', 'btn btn-sm btn-primary', 'Salva stima');
        submit.type = 'submit';
        form.addEventListener('submit', function (event) {
            event.preventDefault();
            if (!form.reportValidity()) return;
            submit.disabled = true;
            errorBox.hidden = true;
            save('/manual-property-valuations/' + encodeURIComponent(item.asset_key), {
                provider: provider.value,
                value_min: minimum.value,
                value_max: maximum.value || null,
                source_date: sourceDate.value || null,
                source_url: url.value || null,
                notes: notes.value || null
            }).catch(function (error) {
                submit.disabled = false;
                showError(error.message);
            });
        });
    }

    entries.forEach(renderItem);

    function attachAffrancationLinks(target, assetKeys, assetsByKey) {
        if (!target || !target.textContent.includes('Importo di affrancazione non disponibile')) return;
        if (target.querySelector('[data-affrancation-links]')) return;
        var available = (assetKeys || []).map(function (assetKey) {
            return assetsByKey[assetKey];
        }).filter(function (item) {
            return item && document.getElementById('affrancation-amount-' + item.asset_key);
        });
        if (!available.length) return;

        var links = node(target, 'span', 'd-inline-flex flex-wrap align-items-baseline gap-2 ms-2');
        links.dataset.affrancationLinks = 'true';
        links.setAttribute('aria-label', 'Importi di affrancazione da definire');
        available.forEach(function (item) {
            var amountInput = document.getElementById('affrancation-amount-' + item.asset_key);
            var link = node(links, 'a', 'small', propertyLabel(item));
            link.href = '#' + amountInput.id;
            link.title = 'Definisci l’importo di affrancazione per ' + propertyLabel(item);
            link.setAttribute('aria-label', link.title);
            link.addEventListener('click', function () {
                var disclosure = amountInput.closest('details');
                while (disclosure) {
                    disclosure.open = true;
                    disclosure = disclosure.parentElement && disclosure.parentElement.closest('details');
                }
                window.setTimeout(function () {
                    try { amountInput.focus({preventScroll: true}); } catch (error) { amountInput.focus(); }
                    amountInput.scrollIntoView({behavior: 'smooth', block: 'center'});
                }, 0);
            });
        });
    }

    function addAssessmentAffrancationLinks() {
        var assessmentSource = document.querySelector('[data-land-map-owner-assessment]');
        var section = document.querySelector('[data-owner-financial-assessment]');
        if (!assessmentSource || !section || section.dataset.affrancationLinksApplied) return false;

        var assessment;
        try { assessment = JSON.parse(assessmentSource.textContent || '{}'); } catch (error) { return false; }
        var assetsByKey = {};
        entries.forEach(function (item) {
            if (item.affrancation && item.asset_key) assetsByKey[item.asset_key] = item;
        });

        var columns = section.querySelectorAll('.row-cols-2.row-cols-md-3 > .col');
        (assessment.tiles || []).forEach(function (metric, index) {
            var tile = columns[index] && columns[index].querySelector('.h-100');
            if (tile && tile.children[1]) {
                attachAffrancationLinks(tile.children[1], metric.affrancation_asset_keys, assetsByKey);
            }
        });

        var rows = section.querySelectorAll('table tbody tr');
        (assessment.rights || []).forEach(function (right, index) {
            var cells = rows[index] && rows[index].querySelectorAll('td');
            if (!cells) return;
            attachAffrancationLinks(cells[0], right.missing_affrancation_asset_keys, assetsByKey);
            attachAffrancationLinks(cells[1], right.missing_affrancation_asset_keys, assetsByKey);
        });
        section.dataset.affrancationLinksApplied = 'true';
        return true;
    }

    function waitForAssessment() {
        if (addAssessmentAffrancationLinks()) return;
        var observer = new MutationObserver(function () {
            if (addAssessmentAffrancationLinks()) observer.disconnect();
        });
        observer.observe(document.documentElement, {childList: true, subtree: true});
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', waitForAssessment, {once: true});
    } else {
        waitForAssessment();
    }
})();
