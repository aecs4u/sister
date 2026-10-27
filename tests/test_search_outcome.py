"""How a subject search outcome is told from the error text."""

import pytest

from sister import search_outcome as so


@pytest.mark.parametrize(
    "status, error, expected",
    [
        ("completed", None, so.FOUND),
        ("error", "soggetto_E_x: NESSUNA CORRISPONDENZA TROVATA", so.NO_MATCH),
        ("error", "NESSUNA CORRISPONDENZA TROVATA (codice fiscale non valido secondo SISTER)", so.INVALID_CODE),
        ("error", "Sessione scaduta o errore caricamento pagina - URL: https://x/Servizi/sessione_scaduta.jsp", so.SESSION_EXPIRED),
        ("error", "Sessione SISTER scaduta o non valida: serve un nuovo accesso (scripts/ade_login.py)", so.SESSION_EXPIRED),
        ("error", "Pagina inattesa dopo la ricerca (https://x/RicercaPF.do): Error 500: java.lang.NullPointerException", so.PORTAL_ERROR),
        ("error", 'Locator.click: Timeout 15000ms exceeded. Call log: - waiting for locator("input")', so.FORM_TIMEOUT),
        ("error", "Portal submission outcome is unknown after worker interruption", so.OTHER_ERROR),
        ("error", "Pagina inattesa dopo la ricerca (https://x/RicercaVisPNF.do): Superato il limite massimo di 400 elementi.", so.TOO_MANY),
        ("queued", None, so.PENDING),
    ],
)
def test_classify(status, error, expected):
    assert so.classify(status, error) == expected


def test_final_outcomes_are_the_ones_a_repeat_cannot_change():
    assert so.FINAL == {so.FOUND, so.NO_MATCH, so.INVALID_CODE, so.TOO_MANY}
