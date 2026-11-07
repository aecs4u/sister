"""``ipotecaria-riepilogo``: the list of ispezioni ipotecarie already run (Riepilogo Ispezioni, read only)."""

from sister import browser
from sister.query_forms import QUERY_FORMS, normalize_query
from sister.utils import _extract_riepilogo_ispezioni, run_ispezioni_ipotecarie_riepilogo

# Layout table around a header-less menu, then the real list: only the latter must be returned.
_PAGE = """
<table><tr><td>
  <table><tr><td><a href="#">Persona fisica</a></td></tr></table>
  <table>
    <tr><th>Ispezione</th><th>Data</th><th>Codice fiscale</th><th>Importo</th></tr>
    <tr><td>T 131857</td><td>06/11/2026</td><td>CNNSFN75P53G273O</td><td>22,40</td></tr>
    <tr><td>T 129836</td><td>06/11/2026</td><td>LRNBBR74A61Z149H</td><td>10,40</td></tr>
  </table>
</td></tr></table>
"""


def test_query_is_defined_without_parameters():
    form = QUERY_FORMS["ipotecaria-riepilogo"]
    assert form.path == "/visura/ipotecaria-riepilogo"
    assert form.fields == ()  # read only: nothing to send, nothing to confirm
    assert not form.documents  # it never requests documents
    assert normalize_query("ipotecaria_riepilogo") == "ipotecaria-riepilogo"


def test_search_type_reaches_the_automation():
    # the generic route turns ``ipotecaria-riepilogo`` into the dispatcher key with underscores
    assert browser._NOARGS_DISPATCHERS["ipotecaria_riepilogo"] is run_ispezioni_ipotecarie_riepilogo


def test_rows_are_keyed_by_the_portal_headers():
    rows = _extract_riepilogo_ispezioni(_PAGE)
    assert rows == [
        {"Ispezione": "T 131857", "Data": "06/11/2026", "Codice fiscale": "CNNSFN75P53G273O", "Importo": "22,40"},
        {"Ispezione": "T 129836", "Data": "06/11/2026", "Codice fiscale": "LRNBBR74A61Z149H", "Importo": "10,40"},
    ]


def test_page_without_a_list_gives_no_rows():
    assert _extract_riepilogo_ispezioni("<html><body><p>Nessuna ispezione</p></body></html>") == []
