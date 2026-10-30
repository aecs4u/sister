from __future__ import annotations

from pathlib import Path

import aecs4u_theme
import land_registry
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, PrefixLoader


def test_property_document_template_renders_without_subject_fiscal_code():
    project_templates = Path(__file__).parents[1] / "sister" / "templates"
    theme_templates = Path(aecs4u_theme.__file__).parent / "templates"
    land_registry_templates = Path(land_registry.__file__).parent / "templates"
    loader = ChoiceLoader(
        [
            DictLoader(
                {
                    "test_land_parent.html": (
                        "{% block extra_css %}{% endblock %}"
                        "{% block content %}<h1 id='profile-heading'>Profilo</h1>"
                        "{% block before_stats %}{% endblock %}{% endblock %}"
                        "{% block extra_js %}{% endblock %}"
                    ),
                    "catasto/visura_soggetto_archive.html": (
                        "{% block extra_css %}{% endblock %}"
                        "{% block content %}<h1 id='profile-heading'>Profilo</h1>"
                        "{% block before_stats %}{% endblock %}{% endblock %}"
                        "{% block extra_js %}{% endblock %}"
                    )
                }
            ),
            FileSystemLoader(str(project_templates)),
            FileSystemLoader(str(theme_templates)),
            PrefixLoader({"land_registry": FileSystemLoader(str(land_registry_templates))}),
        ]
    )
    template = Environment(loader=loader, autoescape=True).get_template("catasto/entity.html")

    rendered = template.render(
        land_map_parent="test_land_parent.html",
        land_map_slot="test",
        doc={"id": 1886, "query_only": False, "favorites_enabled": True, "is_favorite": False},
        pdf_url="/documents/1886/pdf",
        subject_fiscal_code=None,
        subject_route_prefix=None,
        subject_documents=[],
        subject_asset_documents=[],
        subject_portfolio=None,
        subject_hub=None,
        subject_archive=None,
        land_registry_map=None,
        land_registry_kpis=None,
        land_registry_history=None,
        manual_valuation_enabled=False,
        csp_nonce="test-nonce",
    )

    assert 'data-favorite-id="1886"' in rendered
    assert 'aria-label="Aggiungi ai preferiti"' in rendered
    assert '<span data-favorite-label>Aggiungi ai preferiti</span>' in rendered
    favorite_markup = rendered[rendered.index('data-favorite-id="1886"') :]
    favorite_button = favorite_markup[: favorite_markup.index(">")]
    assert "disabled" not in favorite_button
    assert "var completeSubjectReport = false;" in rendered

    notice_template = Environment(loader=loader, autoescape=True).from_string(
        '{% from "_doc_macros.html" import optional_data_notice %}'
        "{{ optional_data_notice(map_context) }}"
    )
    notice = notice_template.render(map_context={"adjacent_lookup_status": "unavailable"})
    assert "Le particelle confinanti non sono disponibili" in notice
    assert notice_template.render(map_context={"adjacent_lookup_status": "available"}).strip() == ""


def test_subject_actions_follow_heading_and_long_omi_note_is_compact():
    project_templates = Path(__file__).parents[1] / "sister" / "templates"
    theme_templates = Path(aecs4u_theme.__file__).parent / "templates"
    land_registry_templates = Path(land_registry.__file__).parent / "templates"
    loader = ChoiceLoader(
        [
            DictLoader(
                {
                    "catasto/visura_soggetto_archive.html": (
                        "{% block extra_css %}{% endblock %}"
                        "{% block content %}<h1 id='profile-heading'>Profilo</h1>"
                        "{% block before_stats %}{% endblock %}{% endblock %}"
                        "{% block extra_js %}{% endblock %}"
                    )
                }
            ),
            FileSystemLoader(str(project_templates)),
            FileSystemLoader(str(theme_templates)),
            PrefixLoader({"land_registry": FileSystemLoader(str(land_registry_templates))}),
        ]
    )
    template = Environment(loader=loader, autoescape=True).get_template("catasto/entity.html")
    long_note = "Metodo di stima dettagliato. " * 24

    rendered = template.render(
        land_map_parent="catasto/visura_soggetto_archive.html",
        land_map_slot="test",
        doc=None,
        subject_fiscal_code="RSSMRA85T10A562S",
        subject_route_prefix="/persons",
        subject_documents=[],
        subject_archive=None,
        subject_portfolio=None,
        land_registry_map=None,
        land_registry_kpis={
            "asset_value_note": long_note,
            "asset_value": "€ 120.000",
            "owner_assessment": {"preferred_source_used": False},
        },
        manual_valuation_enabled=False,
        subject_favorite_enabled=True,
        subject_is_favorite=True,
        csp_nonce="test-nonce",
    )

    assert rendered.index("id='profile-heading'") < rendered.index("data-subject-favorite-actions")
    assert "Stima OMI indicativa; per metodo, ipotesi e limiti consulta" in rendered
    assert long_note not in rendered
    assert "/valuation#stima-omi" in rendered
