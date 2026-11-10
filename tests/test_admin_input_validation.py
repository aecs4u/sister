from sister.web import _validate_admin_input

HEADER = (
    "# T\n\n"
    '<!-- sister-batch-table {"command": "soggetto", "params": {"tipo_catasto": "E"}} -->\n'
    "| id | codice_fiscale |\n|---|---|\n"
)


def test_reports_bad_check_character_and_duplicate_with_line_numbers():
    result = _validate_admin_input(
        HEADER + "| a | BNCFRC74H65D969L |\n| b | BNCFRC74H65D969X |\n| c | BNCFRC74H65D969L |\n"
    )
    assert result["total"] == 3
    assert result["counts"] == {"ready": 2, "duplicate": 1}
    by_id = {issue["id"]: issue for issue in result["issues"]}
    assert by_id["b"]["line"] == 7 and "controllo" in by_id["b"]["message"]
    assert by_id["c"]["line"] == 8 and "duplicata" in by_id["c"]["message"]


def test_parse_errors_are_short_and_not_pydantic_dumps():
    result = _validate_admin_input(HEADER + "| a | X |\n| a | Y |\n")
    assert result["ok"] is False
    assert result["parse_error"] == "id duplicato: a"


def test_text_without_a_batch_is_a_parse_error():
    result = _validate_admin_input("just some notes")
    assert result["ok"] is False and result["parse_error"]
