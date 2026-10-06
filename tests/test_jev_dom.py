"""DOM-derived SISTER page descriptions; source HTML never leaves the process."""

import json
from pathlib import Path

from sister.jev import describe_dom_context_from_html

FIXTURE = Path(__file__).parent / "fixtures" / "jev" / "sister_comune.html"


def test_dom_description_keeps_control_semantics_and_excludes_form_data():
    html = FIXTURE.read_text()
    description = describe_dom_context_from_html(html, "select[name='denomComune']")

    assert description == {
        "role": "combobox",
        "control_label": "Comune",
        "section_label": "Ricerca per località",
    }
    serialized = json.dumps(description)
    assert "PALERMO NORD" not in serialized
    assert "082053" not in serialized
    assert "sessionId" not in serialized
    assert "LVRSML93H08G273J" not in serialized
    assert "secret-session-token" not in serialized


def test_dom_description_rejects_hidden_control():
    html = '<fieldset hidden><legend>Comune</legend><select name="denomComune"></select></fieldset>'
    assert describe_dom_context_from_html(html, "select[name='denomComune']") == {}
