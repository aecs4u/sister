"""Archived inspection documents are shown on their formality row, not twice (subject page)."""

from pathlib import Path

import aecs4u_theme
from jinja2 import ChoiceLoader, Environment, FileSystemLoader

from sister.web import _merge_inspections_into_formalities

TEMPLATES = Path(__file__).resolve().parents[1] / "sister" / "templates"


def _hub():
    return {
        "subject_inspections": [
            {"id": 10, "title": "Nota RP 8036", "date": "2026-11-11", "formats": ["pdf"],
             "extraction": {"kind_label": "Nota di trascrizione", "registration": "RG 11046 · RP 8036",
                            "property_count": 1, "party_count": 2, "party_favore_count": 1, "party_contro_count": 1}},
            {"id": 11, "title": "Titolo RP 10468", "date": "2026-11-11", "formats": ["pdf"],
             "extraction": {"kind_label": "Ispezione ipotecaria", "property_count": 0, "party_count": 0, "id": 226}},
            {"id": 12, "title": "Ispezione su immobile", "date": "2025-01-30", "formats": ["pdf"]},
        ]
    }


def _profile(hub):
    """The fields ``subject_hub.summary`` reads, around the inspections under test."""
    return {
        "code": "MSSLNE82D48Z211J", "name": "", "is_company": False, "searched_at_display": "", "request_id": "r0",
        "totals": {"units": 0, "parcels": 0, "fabbricati": 0, "terreni": 0, "with_visura": 0, "with_map": 0},
        "coverage": {}, "provinces": [], "missing": [], "other_queries": [], "subject_visure": [], **hub,
    }


def _formalities():
    return [
        {
            "request_ref": "r1",
            "inspection": {"number": "T 91777", "date": "2026-11-11", "office": "PALERMO"},
            "note_request": {},
            "formalities": [
                {"numero": 1, "tipo": "TRASCRIZIONE", "registro_particolare": "8036", "registro_generale": "11046",
                 "note_document_id": 10},
                {"numero": 3, "tipo": "ISCRIZIONE", "registro_particolare": "10468", "registro_generale": "13722",
                 "note_document_id": 99, "title_document_id": "11"},
                {"numero": 4, "tipo": "TRASCRIZIONE", "registro_particolare": "1", "registro_generale": "2"},
            ],
        }
    ]


def test_linked_documents_move_to_their_rows_and_leave_the_hub():
    hub, formalities = _hub(), _formalities()

    assert _merge_inspections_into_formalities(hub, formalities) == 2

    rows = formalities[0]["formalities"]
    assert [(e["role"], e["document"]["id"]) for e in rows[0]["archive_documents"]] == [("Nota", 10)]
    assert "archive_documents" not in rows[1]  # the title is linked by the row; its empty extraction adds no detail row
    assert "archive_documents" not in rows[2]
    assert [d["id"] for d in hub["subject_inspections"]] == [12]
    assert hub["subject_inspections_merged"] == 2


def test_nothing_linked_leaves_the_hub_untouched():
    hub = _hub()
    assert _merge_inspections_into_formalities(hub, [{"formalities": [{"numero": 1}]}]) == 0
    assert len(hub["subject_inspections"]) == 3
    assert hub["subject_inspections_merged"] == 0


def test_panel_shows_the_archived_documents_under_the_row():
    hub, formalities = _hub(), _formalities()
    _merge_inspections_into_formalities(hub, formalities)
    theme_root = Path(aecs4u_theme.__file__).parent / "templates"
    env = Environment(
        loader=ChoiceLoader([FileSystemLoader(str(TEMPLATES)), FileSystemLoader(str(theme_root))]), autoescape=True
    )
    env.globals["csrf_input"] = lambda: ""
    html = env.from_string(
        '{% from "components/subject_formalities.html" import subject_formalities_panel %}{{ subject_formalities_panel(f) }}'
    ).render(f=formalities)
    hub_html = env.from_string(
        '{% from "components/subject_hub.html" import summary %}{{ summary(profile, "/persons", true) }}'
    ).render(profile=_profile(hub))
    assert 'data-formality-archive="1"' in html and 'data-formality-archive="3"' not in html
    assert 'data-formality-archive="4"' not in html
    assert 'data-mortgage-extraction-document="10"' in html
    assert "Altre ispezioni ipotecarie del soggetto (1)" in hub_html
    assert "2 documenti sono riportati" in hub_html and 'href="/documents/10"' not in hub_html
