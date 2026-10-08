"""Two-step audit of the single-step query forms.

1. portal → spec: every input of the saved SISTER form (tests/fixtures/portal_forms) has a parameter in
   ``sister.query_forms`` (and the spec names no input that the form lacks);
2. spec → CLI → web: every parameter is an option of ``sister query <command>`` and a parameter of the
   ``/web/forms`` page, which is generated from the same spec.
"""

import re

import pytest

from sister.query_forms import FIXTURE_DIR, QUERY_FORMS, normalize_query, param_names, portal_names

_IGNORED_TYPES = {"hidden", "submit", "reset", "button", "image"}


def portal_inputs(fixture: str) -> set[str]:
    """``name`` of every user-facing control of a saved portal form."""
    html = (FIXTURE_DIR / f"{fixture}.html").read_text(encoding="utf-8")
    names = set()
    for match in re.finditer(r"<(input|select|textarea)\b([^>]*)>", html):
        attrs = match.group(2)
        name = re.search(r'name="([^"]*)"', attrs)
        kind = re.search(r'type="([^"]*)"', attrs)
        if name and (kind.group(1) if kind else match.group(1)) not in _IGNORED_TYPES:
            names.add(name.group(1))
    return names


WITH_FORM = sorted(q for q, form in QUERY_FORMS.items() if form.fixture)


@pytest.mark.parametrize("query", WITH_FORM)
def test_every_portal_input_has_a_parameter(query):
    form = QUERY_FORMS[query]
    found = portal_inputs(form.fixture)
    covered = portal_names(query)
    assert found - covered == set(), f"{query}: portal inputs without a parameter: {sorted(found - covered)}"
    assert covered - found == set(), f"{query}: spec names inputs the portal form lacks: {sorted(covered - found)}"


@pytest.mark.parametrize("query", sorted(QUERY_FORMS))
def test_parameters_are_unique_and_snake_case(query):
    params = [fld.param for fld in QUERY_FORMS[query].fields]
    # the same parameter may drive two portal variants of one input (comuneCat | denomComune) but not two inputs
    portal_by_param: dict[str, set[str]] = {}
    for fld in QUERY_FORMS[query].fields:
        portal_by_param.setdefault(fld.param, set()).update(fld.portal_names)
    assert all(len(names) <= 1 for names in portal_by_param.values()), f"{query}: parameter reused for several inputs"
    assert len(params) == len(set(params))
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", p) for p in params)


def test_command_names_are_normalised():
    assert all(normalize_query(q) == q for q in QUERY_FORMS)
    assert param_names("search")[:2] == ["tipo_catasto", "comune"]


# --------------------------------------------------------------------------------------------------
# step 2a: spec -> CLI (every parameter is an option of the command and reaches the client call)
# --------------------------------------------------------------------------------------------------

_NOT_INPUTS = {"output", "wait", "dry_run", "force", "help"}


def sample_value(query: str, param: str) -> str:
    """A value the query accepts for the parameter (a documented choice when it has a fixed list)."""
    for fld in QUERY_FORMS[query].fields:
        if fld.param == param and fld.choices and fld.kind in ("radio", "select"):
            return next(iter(fld.choices))
    return {"tipo_catasto": "F", "codice_fiscale": "RSSMRA85M01H501Z", "identificativo": "02471840997"}.get(
        param, f"v-{param}"
    )


_CLIENT_METHODS = ("search", "intestati", "soggetto", "persona_giuridica", "elenco_immobili", "generic_search")


def _cli_command(query: str):
    import typer

    from sister.cli import app

    return typer.main.get_command(app).commands["query"].commands[query]


def _flatten(kwargs: dict) -> dict:
    flat = {k: v for k, v in kwargs.items() if k != "form_fields"}
    flat.update(kwargs.get("form_fields") or {})
    return flat


@pytest.mark.parametrize("query", sorted(QUERY_FORMS))
def test_every_parameter_is_a_cli_option(query):
    command = _cli_command(query)
    options = {p.name for p in command.params}
    missing = [p for p in param_names(query) if p not in options]
    assert not missing, f"sister query {query}: no option for {missing}"


@pytest.mark.parametrize("query", sorted(QUERY_FORMS))
def test_cli_options_reach_the_client(query, monkeypatch):
    from typer.testing import CliRunner

    from sister.cli import VisuraClient, app

    sent: dict = {}

    def recorder(method):
        async def fake(self, **kwargs):
            sent.update(_flatten(kwargs))
            sent["_method"] = method
            return {"request_id": "x"}

        return fake

    for method in _CLIENT_METHODS:
        monkeypatch.setattr(VisuraClient, method, recorder(method))

    command = _cli_command(query)
    args = ["query", query]
    expected: dict[str, str] = {}
    for param in command.params:
        if param.name in _NOT_INPUTS or param.name not in param_names(query):
            continue
        flag = max(param.opts, key=len)
        if getattr(param, "is_bool_flag", False):
            args.append(flag)
            expected[param.name] = "true"
        else:
            args += [flag, sample_value(query, param.name)]
            expected[param.name] = sample_value(query, param.name)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    not_sent = {k for k in expected if k not in sent}
    assert not not_sent, f"sister query {query}: options not passed on: {sorted(not_sent)}"


# --------------------------------------------------------------------------------------------------
# step 2b: client -> API -> request (every parameter reaches the service request)
# --------------------------------------------------------------------------------------------------

_VALID = {
    "tipo_catasto": "F",
    "codice_fiscale": "RSSMRA85M01H501Z",
    "identificativo": "02471840997",
    "provincia": "Trieste",
}


@pytest.fixture()
def api(main_module, monkeypatch):
    import httpx

    from sister.client import VisuraClient
    from tests.test_client_contract import FakeService

    service = FakeService()
    monkeypatch.setattr(main_module, "visura_service", service)
    monkeypatch.setattr(main_module, "api_key", None, raising=False)
    client = VisuraClient(base_url="http://sister.test", timeout=5)
    client._get_client = lambda: httpx.AsyncClient(
        base_url=client.base_url, headers=client._headers(), transport=httpx.ASGITransport(app=main_module.app)
    )
    return client, service


@pytest.mark.parametrize("query", sorted(QUERY_FORMS))
async def test_every_parameter_reaches_the_service_request(query, api):
    client, service = api
    params = {name: sample_value(query, name) for name in param_names(query)}
    # the checkbox / choice parameters keep their documented values
    params = {k: ("true" if k in _bool_params(query) else v) for k, v in params.items()}

    await client.submit(query, params)

    ((_, submitted),) = service.submissions
    request = submitted[0] if isinstance(submitted, list) else submitted
    if hasattr(request, "params"):  # generic request: every input travels in params
        received = set(request.params) | {"provincia", "comune", "tipo_catasto"}
    else:
        names = getattr(request, "names", {})
        received = set(request.form_fields) | {
            p for p in params if getattr(request, names.get(p, p), None) not in (None, "")
        }
    assert set(params) - received == set(), f"{query}: lost on the way to the service: {sorted(set(params) - received)}"


def _bool_params(query: str) -> set[str]:
    return {fld.param for fld in QUERY_FORMS[query].fields if fld.kind == "checkbox"}


# --------------------------------------------------------------------------------------------------
# step 2c: CLI -> /web/forms (the page is generated from the CLI commands)
# --------------------------------------------------------------------------------------------------


def _single_step_endpoints():
    from sister.form_config import get_single_step_groups
    from sister.query_forms import command_for_path

    for group in get_single_step_groups():
        for endpoint in group.endpoints:
            command = command_for_path(endpoint.path)
            if command in QUERY_FORMS:
                yield group, endpoint, command


def test_web_forms_take_exactly_the_cli_options():
    seen = set()
    for group, endpoint, command in _single_step_endpoints():
        cli = {p.name for p in _cli_command(command).params} - _NOT_INPUTS
        web = {p.name for p in group.params if endpoint.id in p.endpoints}
        assert web == cli, f"/web/forms {group.id}/{endpoint.id} vs sister query {command}: {sorted(web ^ cli)}"
        assert set(param_names(command)) <= web
        seen.add(command)
    assert seen >= set(WITH_FORM)  # every query with a portal form has a web endpoint


def test_web_forms_page_renders_an_input_for_every_parameter(main_module):
    from fastapi.testclient import TestClient

    response = TestClient(main_module.app).get("/web/forms")
    assert response.status_code == 200
    html = response.text
    for group, endpoint, command in _single_step_endpoints():
        for param in group.params:
            if endpoint.id in param.endpoints:
                assert f'name="{param.name}"' in html, f"{group.id}: {param.name} not rendered"


def test_web_submit_goes_through_the_cli_client(main_module, monkeypatch):
    """/web/api/<endpoint> submits with the same client code as the CLI (command + parameters)."""
    from fastapi.testclient import TestClient

    from sister.client import VisuraClient

    calls = []

    async def fake_submit(self, command, params, *, force=False):
        calls.append((command, dict(params), force))
        return {"request_id": "x", "status": "queued"}

    monkeypatch.setattr(VisuraClient, "submit", fake_submit)
    web = TestClient(main_module.app)
    body = {"provincia": "Ravenna", "comune": "RAVENNA", "foglio": "103", "categoria": "A%", "force": "true"}
    assert web.post("/web/api/elenco-immobili", json=body).json()["request_id"] == "x"
    assert web.post("/web/api/mappa", json={"provincia": "Ravenna", "intero_foglio": "true"}).status_code == 200
    assert calls[0] == (
        "elenco",
        {"provincia": "Ravenna", "comune": "RAVENNA", "foglio": "103", "categoria": "A%"},
        True,
    )
    assert calls[1][0] == "mappa" and calls[1][1]["intero_foglio"] == "true"


@pytest.mark.parametrize("query", sorted(QUERY_FORMS))
def test_mode_dependent_inputs_name_an_existing_mode(query):
    """``requires=(mode parameter, value)`` must point at a radio of the same form and one of its values."""
    by_param = {fld.param: fld for fld in QUERY_FORMS[query].fields}
    for fld in QUERY_FORMS[query].fields:
        if fld.requires:
            mode_param, mode_value = fld.requires
            assert mode_param in by_param, f"{query}.{fld.param}: mode parameter {mode_param} not in the form"
            assert by_param[mode_param].kind == "radio"
            assert mode_value in by_param[mode_param].choices, f"{query}.{fld.param}: {mode_value} not a choice"


def test_web_proxies_send_the_api_key(main_module, monkeypatch):
    """The page's poll/proxy calls to the service's own API carry X-API-Key (otherwise they get 401)."""
    import httpx
    from fastapi.testclient import TestClient

    seen = []

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"status": "completed"}

    async def fake_get(self, url, **kwargs):
        seen.append(("GET", url, kwargs.get("headers")))
        return _Resp()

    async def fake_post(self, url, **kwargs):
        seen.append(("POST", url, kwargs.get("headers")))
        return _Resp()

    monkeypatch.setenv("API_KEY", "secret-key")
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    web = TestClient(main_module.app)
    assert web.get("/web/api/visura/req_F_x").status_code == 200
    assert web.post("/web/api/ipotecaria-stato-not-a-query", json={}).status_code == 200
    assert all(headers == {"X-API-Key": "secret-key"} for _, _, headers in seen), seen
    assert len(seen) == 2
