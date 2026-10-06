"""Tests for the optional TypeSafe Jev cadastral-choice adapter."""

import json

import httpx
import pytest

from sister.jev import (
    AmbiguousOptionError,
    JevAbstentionError,
    _build_batch_payload,
    _build_payload,
    _parse_choice,
    choose_cadastral_option,
    choose_cadastral_options,
)
from sister.utils import find_best_option_match, find_best_option_matches, record_ambiguous_options


def _answer(choice="option_1", confidence=0.99, probabilities=None):
    return {
        "answers": {
            "best_option": {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": probabilities or {"option_0": 0.01, "option_1": 0.99},
            }
        }
    }


def test_build_payload_is_a_short_page_description_without_values_or_query_string():
    payload, option_map = _build_payload(
        selector="select[name='denomComune']",
        requested="PALERMO",
        page_url="https://sister3.example/Visure/SceltaComune.do?session=secret",
        candidates=[("082053", "PALERMO NORD"), ("082054", "PALERMO SUD")],
    )

    description = payload["state"]["page_description"]
    assert description["portal"] == "Agenzia delle Entrate SISTER"
    assert description["control"] == "cadastral municipality"
    assert description["page_path"].endswith("SceltaComune.do")
    assert description["available_choices"] == ["PALERMO NORD", "PALERMO SUD"]
    assert "cadastral municipality" in description["decision_required"]
    instructions = payload["questions"]["best_option"]["instructions"]
    assert "complete requested label" in instructions
    assert "low confidence" in instructions
    assert "session=secret" not in json.dumps(payload)
    assert "082053" not in json.dumps(payload)
    assert option_map == {"option_0": "082053", "option_1": "082054"}




def test_build_batch_payload_has_separate_questions_for_page_decisions():
    payload, option_maps = _build_batch_payload(
        page_url="https://sister3.example/Visure/SceltaComune.do?session=secret",
        decisions=[
            {
                "id": "office",
                "selector": "select[name='listacom']",
                "requested": "PA",
                "candidates": [("082", "PALERMO"), ("083", "RAGUSA")],
            },
            {
                "id": "comune",
                "selector": "select[name='denomComune']",
                "requested": "PALERMO",
                "candidates": [("1", "PALERMO NORD"), ("2", "PALERMO SUD")],
            },
        ],
    )

    assert set(payload["questions"]) == {"office", "comune"}
    decisions = payload["state"]["page_description"]["decision_points"]
    assert len(decisions) == 2
    assert "province/office" in decisions[0]["decision_required"]
    assert "municipality" in decisions[1]["decision_required"]
    assert "session=secret" not in json.dumps(payload)
    assert "082" not in json.dumps(payload)
    assert option_maps == {
        "office": {"option_0": "082", "option_1": "083"},
        "comune": {"option_0": "1", "option_1": "2"},
    }


@pytest.mark.asyncio
async def test_batch_choice_uses_one_request_for_multiple_page_decisions(monkeypatch):
    monkeypatch.setenv("SISTER_JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    captured = {}

    def handler(request):
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "answers": {
                    "office": {
                        "type": "choice",
                        "choice": "option_0",
                        "confidence": 0.99,
                        "probabilities": {"option_0": 0.99, "option_1": 0.01},
                    },
                    "comune": {
                        "type": "choice",
                        "choice": "option_1",
                        "confidence": 0.99,
                        "probabilities": {"option_0": 0.01, "option_1": 0.99},
                    },
                }
            },
        )

    choices = await choose_cadastral_options(
        page_url="https://sister3.example/Visure/SceltaComune.do",
        decisions=[
            {
                "id": "office",
                "selector": "select[name='listacom']",
                "requested": "PA",
                "candidates": [("082", "PALERMO"), ("083", "RAGUSA")],
            },
            {
                "id": "comune",
                "selector": "select[name='denomComune']",
                "requested": "PALERMO",
                "candidates": [("1", "PALERMO NORD"), ("2", "PALERMO SUD")],
            },
        ],
        _transport=httpx.MockTransport(handler),
    )

    assert choices["office"].option_value == "082"
    assert choices["comune"].option_value == "2"
    assert set(captured["payload"]["questions"]) == {"office", "comune"}


@pytest.mark.parametrize(
    ("selector", "requested", "candidates", "expected_task"),
    [
        ("select[name='listacom']", "NAZIONALE", [("N", "NAZIONALE"), ("PA", "PALERMO")], "nationwide option"),
        ("select[name='denomComune']", "PALERMO", [("1", "PALERMO NORD"), ("2", "PALERMO SUD")], "municipality"),
        ("select[name='comuneCat']", "PALERMO", [("1", "PALERMO NORD"), ("2", "PALERMO SUD")], "municipality"),
        ("select[name='sezione']", "A", [("1", "A"), ("2", "B")], "section"),
    ],
)
def test_jev_request_states_the_specific_decision_for_each_cadastral_control(
    selector, requested, candidates, expected_task
):
    payload, _ = _build_payload(
        selector=selector,
        requested=requested,
        page_url="https://sister3.example/Visure/SceltaComune.do",
        candidates=candidates,
    )
    task = payload["state"]["page_description"]["decision_required"].lower()
    assert expected_task in task
    assert "one supplied candidate only" in payload["questions"]["best_option"]["instructions"]


def test_build_payload_rejects_personal_identifiers():
    with pytest.raises(ValueError, match="must not contain"):
        _build_payload(
            selector="select[name='denomComune']",
            requested="LVRSML93H08G273J",
            page_url="https://sister3.example/Visure/SceltaComune.do",
            candidates=[("01", "PALERMO NORD"), ("02", "PALERMO EAST")],
        )


def test_build_payload_rejects_non_location_controls():
    with pytest.raises(ValueError):
        _build_payload(
            selector="input[name='omonimoSelezionato']",
            requested="PERSON",
            page_url="https://sister3.example/Visure/SceltaSoggetto.do",
            candidates=[("a", "one"), ("b", "two")],
        )


def test_parse_choice_enforces_confidence_and_margin(monkeypatch):
    monkeypatch.setenv("SISTER_JEV_MIN_CONFIDENCE", "0.95")
    option_map = {"option_0": "p1", "option_1": "p2"}

    chosen = _parse_choice(_answer(), option_map)
    assert chosen.option_value == "p2"
    assert not chosen.abstained

    unsure = _parse_choice(
        _answer(confidence=0.82, probabilities={"option_0": 0.18, "option_1": 0.82}),
        option_map,
    )
    assert unsure.abstained
    assert unsure.option_value is None

    tied = _parse_choice(
        _answer(confidence=0.98, probabilities={"option_0": 0.48, "option_1": 0.52}),
        option_map,
    )
    assert tied.abstained


@pytest.mark.asyncio
async def test_call_uses_typed_choice_and_returns_local_option_value(monkeypatch):
    monkeypatch.setenv("SISTER_JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    captured = {}

    def handler(request):
        captured["payload"] = json.loads(request.content)
        captured["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json=_answer())

    result = await choose_cadastral_option(
        selector="select[name='denomComune']",
        requested="PALERMO",
        page_url="https://sister3.example/Visure/SceltaComune.do",
        candidates=[("082053", "PALERMO NORD"), ("082054", "PALERMO SUD")],
        _transport=httpx.MockTransport(handler),
    )

    assert result.option_value == "082054"
    assert captured["authorization"] == "Bearer test-key"
    assert captured["payload"]["questions"]["best_option"]["type"] == "choice"
    assert captured["payload"]["state"]["page_description"]["available_choices"] == [
        "PALERMO NORD",
        "PALERMO SUD",
    ]


class _Option:
    def __init__(self, value, label):
        self.value = value
        self.label = label

    async def get_attribute(self, name):
        return self.value if name == "value" else None

    async def inner_text(self):
        return self.label


class _OptionsLocator:
    def __init__(self, options):
        self.options = options

    async def all(self):
        return self.options


class _Page:
    url = "https://sister3.example/Visure/SceltaComune.do?session=secret"

    def __init__(self, options, html=""):
        self.options = options
        self.html = html

    def locator(self, selector):
        if isinstance(self.options, dict):
            normalized = selector.removesuffix(" option")
            return _OptionsLocator(self.options.get(normalized, []))
        return _OptionsLocator(self.options)

    async def content(self):
        return self.html


@pytest.mark.asyncio
async def test_fuzzy_tie_uses_jev_choice(monkeypatch):
    from sister import jev

    monkeypatch.setenv("SISTER_JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    seen = {}

    async def fake_choice(**kwargs):
        seen.update(kwargs)
        return jev.JevChoice(option_value="02", confidence=0.99)

    monkeypatch.setattr(jev, "choose_cadastral_option", fake_choice)
    page = _Page([_Option("01", "PALERMO NORD"), _Option("02", "PALERMO EAST")])

    result = await find_best_option_match(page, "select[name='denomComune']", "PALERMO")

    assert result == "02"
    assert seen["requested"] == "PALERMO"
    assert len(seen["candidates"]) == 2


@pytest.mark.asyncio
async def test_page_matcher_batches_only_ambiguous_dynamic_controls(monkeypatch):
    from sister import jev

    monkeypatch.setenv("SISTER_JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    seen = {}

    async def fake_batch(**kwargs):
        seen.update(kwargs)
        return {
            "province": jev.JevChoice(option_value="082", confidence=0.99),
            "municipality": jev.JevChoice(option_value="001", confidence=0.99),
        }

    monkeypatch.setattr(jev, "choose_cadastral_options", fake_batch)
    page = _Page(
        {
            "select[name='listacom']": [_Option("082", "PA NORD"), _Option("083", "PA EAST")],
            "select[name='denomComune']": [
                _Option("001", "PALERMO NORD"),
                _Option("002", "PALERMO EAST"),
            ],
            "select[name='sezione']": [_Option("A", "A"), _Option("B", "B")],
        }
    )

    result = await find_best_option_matches(
        page,
        [
            {"id": "province", "selector": "select[name='listacom']", "requested": "PA"},
            {"id": "municipality", "selector": "select[name='denomComune']", "requested": "PALERMO"},
            {"id": "section", "selector": "select[name='sezione']", "requested": "A"},
        ],
    )

    assert result == {"province": "082", "municipality": "001", "section": "A"}
    assert [item["id"] for item in seen["decisions"]] == ["province", "municipality"]


@pytest.mark.asyncio
async def test_exact_match_skips_jev(monkeypatch):
    from sister import jev

    async def should_not_call(**_kwargs):
        raise AssertionError("Jev should not be called for an exact match")

    monkeypatch.setenv("SISTER_JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(jev, "choose_cadastral_option", should_not_call)

    result = await find_best_option_match(
        _Page([_Option("01", "PALERMO"), _Option("02", "PALERMO SUD")]),
        "select[name='denomComune']",
        "PALERMO",
    )
    assert result == "01"


@pytest.mark.asyncio
async def test_low_confidence_tie_stops_selection(monkeypatch):
    from sister import jev

    async def abstain(**_kwargs):
        return jev.JevChoice(option_value=None, confidence=0.70, abstained=True)

    monkeypatch.setenv("SISTER_JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(jev, "choose_cadastral_option", abstain)

    with pytest.raises(JevAbstentionError, match="operator selection is required"):
        await find_best_option_match(
            _Page([_Option("01", "PALERMO NORD"), _Option("02", "PALERMO EAST")]),
            "select[name='denomComune']",
            "PALERMO",
        )

@pytest.mark.asyncio
async def test_live_dom_context_is_sent_but_raw_html_and_values_are_not(monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("SISTER_JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    fixture = Path(__file__).parent / "fixtures" / "jev" / "sister_comune.html"
    page = _Page([], fixture.read_text())
    captured = {}

    def handler(request):
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_answer())

    result = await choose_cadastral_option(
        selector="select[name='denomComune']",
        requested="PALERMO",
        page_url=page.url,
        candidates=[("082053", "PALERMO NORD"), ("082054", "PALERMO SUD")],
        page=page,
        _transport=httpx.MockTransport(handler),
    )

    description = captured["payload"]["state"]["page_description"]
    assert description["dom_context"] == {
        "role": "combobox",
        "control_label": "Comune",
        "section_label": "Ricerca per località",
    }
    serialized = json.dumps(captured["payload"])
    assert "LVRSML93H08G273J" not in serialized
    assert "secret-session-token" not in serialized
    assert "082053" not in serialized
    assert "<html" not in serialized
    assert result.option_value == "082054"


@pytest.mark.asyncio
async def test_soppresso_option_is_never_selected(monkeypatch):
    from sister import jev

    async def should_not_call(**_kwargs):
        raise AssertionError("Jev must not receive a suppressed cadastral option")

    monkeypatch.setenv("SISTER_JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(jev, "choose_cadastral_option", should_not_call)
    result = await find_best_option_match(
        _Page([_Option("99", "PALERMO SOPPRESSA")]),
        "select[name='denomComune']",
        "PALERMO SOPPRESSA",
    )
    assert result is None


def _tied_page():
    return _Page([_Option("01", "PALERMO NORD"), _Option("02", "PALERMO EAST")])


@pytest.mark.asyncio
async def test_local_tie_break_is_recorded_when_jev_disabled(monkeypatch):
    monkeypatch.delenv("SISTER_JEV_ENABLED", raising=False)
    monkeypatch.delenv("SISTER_AMBIGUOUS_OPTION_POLICY", raising=False)

    with record_ambiguous_options() as decisions:
        result = await find_best_option_match(_tied_page(), "select[name='denomComune']", "PALERMO")

    assert result == "01"
    assert decisions == [{
        "control": "denomComune",
        "requested": "PALERMO",
        "candidates": ["PALERMO NORD", "PALERMO EAST"],
        "selected": "PALERMO NORD",
        "resolution": "local_tie_break",
    }]


@pytest.mark.asyncio
async def test_operator_policy_stops_unresolved_tie(monkeypatch):
    monkeypatch.delenv("SISTER_JEV_ENABLED", raising=False)
    monkeypatch.setenv("SISTER_AMBIGUOUS_OPTION_POLICY", "operator")

    with record_ambiguous_options() as decisions:
        with pytest.raises(AmbiguousOptionError, match="operator selection is required"):
            await find_best_option_match(_tied_page(), "select[name='denomComune']", "PALERMO")
    assert decisions == []


@pytest.mark.asyncio
async def test_operator_policy_stops_when_jev_is_unavailable(monkeypatch):
    from sister import jev

    async def unavailable(**_kwargs):
        return None

    monkeypatch.setenv("SISTER_JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("SISTER_AMBIGUOUS_OPTION_POLICY", "operator")
    monkeypatch.setattr(jev, "choose_cadastral_option", unavailable)

    with pytest.raises(AmbiguousOptionError):
        await find_best_option_match(_tied_page(), "select[name='denomComune']", "PALERMO")


@pytest.mark.asyncio
async def test_operator_policy_keeps_accepted_jev_answer_and_exact_matches(monkeypatch):
    from sister import jev

    async def accepted(**_kwargs):
        return jev.JevChoice(option_value="02", confidence=0.991)

    monkeypatch.setenv("SISTER_JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("SISTER_AMBIGUOUS_OPTION_POLICY", "operator")
    monkeypatch.setattr(jev, "choose_cadastral_option", accepted)

    with record_ambiguous_options() as decisions:
        assert await find_best_option_match(_tied_page(), "select[name='denomComune']", "PALERMO") == "02"
        assert await find_best_option_match(_tied_page(), "select[name='denomComune']", "PALERMO NORD") == "01"

    assert len(decisions) == 1
    assert decisions[0]["selected"] == "PALERMO EAST"
    assert decisions[0]["resolution"] == "jev"
    assert decisions[0]["confidence"] == 0.991


@pytest.mark.asyncio
async def test_batch_operator_policy_stops_without_partial_results(monkeypatch):
    monkeypatch.delenv("SISTER_JEV_ENABLED", raising=False)
    monkeypatch.setenv("SISTER_AMBIGUOUS_OPTION_POLICY", "operator")
    page = _Page({
        "select[name='denomComune']": [_Option("001", "PALERMO NORD"), _Option("002", "PALERMO EAST")],
        "select[name='sezione']": [_Option("A", "A"), _Option("B", "B")],
    })

    with pytest.raises(AmbiguousOptionError, match="PALERMO"):
        await find_best_option_matches(page, [
            {"id": "municipality", "selector": "select[name='denomComune']", "requested": "PALERMO"},
            {"id": "section", "selector": "select[name='sezione']", "requested": "A"},
        ])


@pytest.mark.asyncio
async def test_batch_local_fallback_is_recorded_when_jev_omits_an_answer(monkeypatch):
    from sister import jev

    async def partial(**_kwargs):
        return {"province": jev.JevChoice(option_value="082", confidence=0.99)}

    monkeypatch.setenv("SISTER_JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.delenv("SISTER_AMBIGUOUS_OPTION_POLICY", raising=False)
    monkeypatch.setattr(jev, "choose_cadastral_options", partial)
    page = _Page({
        "select[name='listacom']": [_Option("082", "PA NORD"), _Option("083", "PA EAST")],
        "select[name='denomComune']": [_Option("001", "PALERMO NORD"), _Option("002", "PALERMO EAST")],
    })

    with record_ambiguous_options() as decisions:
        result = await find_best_option_matches(page, [
            {"id": "province", "selector": "select[name='listacom']", "requested": "PA"},
            {"id": "municipality", "selector": "select[name='denomComune']", "requested": "PALERMO"},
        ])

    assert result == {"province": "082", "municipality": "001"}
    assert [item["resolution"] for item in decisions] == ["local_tie_break", "local_tie_break"]


def test_jev_abstention_is_an_ambiguous_option_error():
    assert issubclass(JevAbstentionError, AmbiguousOptionError)


@pytest.mark.parametrize("value, expected", [("operator", "operator"), (" OPERATOR ", "operator"),
                                             ("local", "local"), ("stop", "local"), ("", "local")])
def test_ambiguous_option_policy_values(monkeypatch, value, expected):
    from sister.jev import ambiguous_option_policy

    monkeypatch.setenv("SISTER_AMBIGUOUS_OPTION_POLICY", value)
    assert ambiguous_option_policy() == expected
