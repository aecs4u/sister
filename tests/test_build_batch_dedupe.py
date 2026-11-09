from sister.command_tools.build_batch_from_list import drop_duplicate_queries


def q(id, **kw):
    return {"id": id, "command": "soggetto", "params": {"codice_fiscale": "AAA"}, **kw}


def test_duplicates_dropped_first_kept():
    kept, dropped = drop_duplicate_queries([q("a"), q("b", params={"codice_fiscale": " aaa "}), q("c", params={"codice_fiscale": "BBB"})])
    assert [k["id"] for k in kept] == ["a", "c"]
    assert dropped == [("b", "a")]


def test_force_disabled_and_different_params_stay():
    rows = [q("a"), q("b", reuse="force"), q("c", enabled=False), q("d", params={"codice_fiscale": "AAA", "tipo_catasto": "T"})]
    kept, dropped = drop_duplicate_queries(rows)
    assert [k["id"] for k in kept] == ["a", "b", "c", "d"] and not dropped


def test_defaults_count():
    rows = [{"id": "a", "params": {"identificativo": "1"}}, {"id": "b", "params": {"identificativo": "1"}}]
    kept, dropped = drop_duplicate_queries(rows, {"command": "azienda", "params": {"tipo_catasto": "E"}})
    assert len(kept) == 1 and dropped == [("b", "a")]
