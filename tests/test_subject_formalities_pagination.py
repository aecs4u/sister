"""One page of formalities per inspection on the subject page, with the counts of the whole list kept."""

from pathlib import Path

import aecs4u_theme
import pytest
from jinja2 import ChoiceLoader, Environment, FileSystemLoader

from sister.web import FORMALITIES_PAGE_SIZE, _paginate_formalities

TEMPLATES = Path(__file__).resolve().parents[1] / "sister" / "templates"


def _inspection(count: int, number: str = "T 1") -> dict:
    return {
        "request_ref": f"r-{number}",
        "inspection": {"number": number, "date": "2026-11-12", "office": "RAVENNA"},
        "note_request": {},
        "formalities": [
            {"numero": n, "tipo": "TRASCRIZIONE", "verso": "CONTRO", "registro_particolare": str(n), "registro_generale": str(n + 100)}
            for n in range(1, count + 1)
        ],
    }


def _numbers(inspection: dict) -> list[int]:
    return [row["numero"] for row in inspection["formalities"]]


def test_default_page_holds_the_first_rows_and_describes_the_whole_list():
    inspections = [_inspection(60)]
    _paginate_formalities(inspections, [])
    pg = inspections[0]["pagination"]
    assert _numbers(inspections[0]) == list(range(1, FORMALITIES_PAGE_SIZE + 1))
    assert (pg["page"], pg["pages"], pg["total"], pg["first"], pg["last"], pg["all"]) == (1, 3, 60, 1, 25, False)


def test_requested_page_and_out_of_range_values():
    for query, expected in ((("fp1", "3"), range(51, 61)), (("fp1", "99"), range(51, 61)), (("fp1", "0"), range(1, 26)), (("fp1", "x"), range(1, 26))):
        inspections = [_inspection(60)]
        _paginate_formalities(inspections, [query])
        assert _numbers(inspections[0]) == list(expected), query


def test_all_keeps_every_row_and_size_is_clamped():
    inspections = [_inspection(60)]
    _paginate_formalities(inspections, [("fp1", "all")])
    assert len(inspections[0]["formalities"]) == 60 and inspections[0]["pagination"]["all"] is True

    for raw, size in (("2", 5), ("500", 100), ("abc", FORMALITIES_PAGE_SIZE), ("10", 10)):
        inspections = [_inspection(60)]
        _paginate_formalities(inspections, [("fsize", raw)])
        assert inspections[0]["pagination"]["size"] == size, raw


def test_each_inspection_has_its_own_page_and_links_keep_the_other_parameters():
    inspections = [_inspection(60, "T 1"), _inspection(60, "T 2")]
    _paginate_formalities(inspections, [("map", "1,2,3"), ("fp1", "2"), ("fp2", "3"), ("fsize", "10")])
    assert _numbers(inspections[0]) == list(range(11, 21))
    assert inspections[1]["formalities"][0]["numero"] == 21
    query = inspections[0]["pagination"]["query"]
    assert "fp1" not in query and "fp2=3" in query and "map=1%2C2%2C3" in query and "fsize=10" in query


def test_empty_and_short_lists_need_no_pager():
    inspections = [_inspection(0, "T 0"), _inspection(7, "T 7")]
    _paginate_formalities(inspections, [])
    assert inspections[0]["pagination"]["pages"] == 1 and inspections[0]["pagination"]["first"] == 0
    assert inspections[1]["pagination"]["pages"] == 1 and len(inspections[1]["formalities"]) == 7


def _render(inspections) -> str:
    theme_root = Path(aecs4u_theme.__file__).parent / "templates"
    env = Environment(loader=ChoiceLoader([FileSystemLoader(str(TEMPLATES)), FileSystemLoader(str(theme_root))]), autoescape=True)
    env.globals["csrf_input"] = lambda: ""
    return env.from_string(
        '{% from "components/subject_formalities.html" import subject_formalities_panel %}{{ subject_formalities_panel(f) }}'
    ).render(f=inspections)


def test_panel_renders_one_page_with_a_pager_that_keeps_the_query():
    inspections = [_inspection(60)]
    _paginate_formalities(inspections, [("map", "1,2,3"), ("fp1", "2")])
    html = _render(inspections)
    assert html.count("data-formality-number=") == 25
    assert "60 formalità" in html and "Formalità 26–50 di 60" in html
    assert 'data-formalities-pager="fp1"' in html
    assert 'href="?map=1%2C2%2C3&amp;fp1=3#formalities-inspection-1"' in html
    assert 'aria-current="page"' in html and "Mostra tutte" in html


def test_panel_all_view_offers_the_way_back_and_short_lists_show_no_pager():
    inspections = [_inspection(60)]
    _paginate_formalities(inspections, [("fp1", "all")])
    html = _render(inspections)
    assert html.count("data-formality-number=") == 60 and "Mostra a pagine" in html and "Tutte le 60 formalità" in html

    short = [_inspection(7)]
    _paginate_formalities(short, [])
    html = _render(short)
    assert html.count("data-formality-number=") == 7 and "data-formalities-pager" not in html and "7 formalità" in html
