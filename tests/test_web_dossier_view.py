"""The /web/dossiers/view page opens a dossier JSON (it used to crash on an undefined ``_json``)."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

FIXTURE = Path(__file__).parent / "fixtures" / "single_step" / "elenco" / "success.json"


def test_dossier_view_renders_a_stored_response(main_module, monkeypatch, tmp_path):
    monkeypatch.setenv("SISTER_DOSSIERS_BASE", str(tmp_path))
    (tmp_path / "elenco_fixture.json").write_text(
        json.dumps(json.loads(FIXTURE.read_text(encoding="utf-8"))["response"]), encoding="utf-8"
    )
    response = TestClient(main_module.app).get("/web/dossiers/view/elenco_fixture.json")
    assert response.status_code == 200, response.text[:300]
    assert "elenco_fixture.json" in response.text


def test_dossier_view_reports_invalid_json_instead_of_failing(main_module, monkeypatch, tmp_path):
    monkeypatch.setenv("SISTER_DOSSIERS_BASE", str(tmp_path))
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    response = TestClient(main_module.app).get("/web/dossiers/view/broken.json")
    assert response.status_code == 200
    assert "broken.json" in response.text


def test_dossier_view_rejects_path_traversal(main_module, monkeypatch, tmp_path):
    monkeypatch.setenv("SISTER_DOSSIERS_BASE", str(tmp_path))
    assert TestClient(main_module.app).get("/web/dossiers/view/..%2f..%2fetc%2fpasswd").status_code in (403, 404)
