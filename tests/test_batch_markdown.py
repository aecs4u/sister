"""Markdown batch files (docs/batch_markdown_format.md): tables, legacy codici lists, conversion, pruning."""

from __future__ import annotations

import pytest

from sister.batch import load_batch
from sister.batch_markdown import legacy_codes_to_markdown, load_markdown, parse_markdown

V1 = """# PVP sales parcels — AG

<!-- sister-batch-table:v1 -->
| id | command | provincia | comune | foglio | particella | subalterno | tipo_catasto | scope_note |
|---|---|---|---|---|---|---|---|---|
| a-1 | visura-storica | AG | AGRIGENTO | 100 | 146 | 18 | F | note &#124; one |
| a-2 | visura-storica | AG | AGRIGENTO | 100 | 59 |  | T |  |
"""

V2 = """# Mixed

First paragraph.

Second paragraph.

<!-- sister-batch {"options": {"retries": 2}, "defaults": {"params": {"tipo_catasto": "E"}}} -->

## Persone

<!-- sister-batch-table {"command": "soggetto", "params": {"tipo_catasto": "E"}, "metadata": {"tags": "a;b"}} -->
| id | codice_fiscale | meta_nome | enabled |
|---|---|---|---|
| pf-1 | `RSSMRA80A01H501U` | Mario Rossi |  |
| pf-2 | `RSSMRA80A01H501U` | — | false |

## Parcels

<!-- sister-batch-table -->
| id | command | provincia | comune | foglio | particella | tipo_catasto |
|---|---|---|---|---|---|---|
| p-1 | visura-storica | RM | Roma | 1 | 2 | F |
"""

LEGACY = """# Codici

## Persone fisiche (codice fiscale, 16 caratteri) (2)

| # | Codice | Nome | Fonte |
|---:|---|---|---|
| 1 | `RSSMRA80A01H501U` | MARIO ROSSI | `opendata` |
| 2 | `NOTACODE12345678` | X | y |

## Persone giuridiche (partita IVA, 11 cifre) (1)

| # | Codice | Nome | Fonte |
|---:|---|---|---|
| 1 | `09339391006` | — | codici_fiscali |
"""


def test_v1_table_keeps_params_and_metadata():
    parsed = parse_markdown(V1)
    assert parsed.batch.name == "PVP sales parcels — AG"
    first, second = parsed.batch.queries
    assert first.id == "a-1" and first.command == "visura-storica"
    assert first.params["particella"] == "146" and first.params["subalterno"] == "18"
    assert first.metadata == {"scope_note": "note | one"}
    assert "subalterno" not in second.params  # empty cell = not given
    assert parsed.lines == [6, 7]


def test_table_defaults_document_options_and_description():
    batch = parse_markdown(V2).batch
    assert batch.description == "First paragraph."
    assert batch.options.retries == 2
    pf1, pf2, parcel = batch.queries
    assert pf1.command == "soggetto" and pf1.params["codice_fiscale"] == "RSSMRA80A01H501U"
    assert pf1.params["tipo_catasto"] == "E"
    assert pf1.metadata == {"nome": "Mario Rossi", "tags": ["A", "B"]}
    assert pf2.enabled is False and "nome" not in pf2.metadata
    assert parcel.command == "visura-storica" and parcel.params["tipo_catasto"] == "F"


def test_errors_name_the_line():
    with pytest.raises(ValueError, match="riga 7: comando"):
        parse_markdown(V1.replace("| a-2 | visura-storica", "| a-2 | nope"))
    with pytest.raises(ValueError, match="riga 6: 8 celle, attese 9"):
        parse_markdown(V1.replace("| a-1 | visura-storica |", "| a-1 |"))
    with pytest.raises(ValueError, match="nessuna riga"):
        parse_markdown("<!-- sister-batch-table -->\n")


def test_duplicate_ids_are_rejected():
    with pytest.raises(ValueError, match="id duplicato"):
        parse_markdown(V1.replace("a-2", "a-1"))


def test_legacy_code_list():
    parsed = load_markdown(LEGACY)
    assert [(q.id, q.command) for q in parsed.batch.queries] == [
        ("pf-RSSMRA80A01H501U", "soggetto"),
        ("pi-09339391006", "azienda"),
    ]
    assert parsed.skipped == ["NOTACODE12345678"]
    assert parsed.batch.queries[0].params["tipo_catasto"] == "E"


def test_convert_legacy_list_round_trips():
    new = legacy_codes_to_markdown(LEGACY.replace("`NOTACODE12345678`", "`XXXXXXXXXXXXXXXX`"))
    batch = parse_markdown(new).batch
    ids = [q.id for q in batch.queries]
    assert ids == ["pf-RSSMRA80A01H501U", "pf-XXXXXXXXXXXXXXXX", "pi-09339391006"]
    assert batch.queries[0].metadata == {"intestatario": "MARIO ROSSI", "fonte": "opendata"}
    assert "nome" not in batch.queries[0].params
    # converting again is a no-op for the loader: same queries
    assert [q.model_dump() for q in parse_markdown(new).batch.queries] == [q.model_dump() for q in batch.queries]


def test_load_batch_reads_markdown(tmp_path):
    path = tmp_path / "list.md"
    path.write_text(V1, encoding="utf-8")
    assert [q.id for q in load_batch(path).queries] == ["a-1", "a-2"]


def test_prune_drops_available_rows_and_alternatives():
    from sister.command_tools.prune_code_list import available_lines

    text = """# t

<!-- sister-batch-table -->
| id | command | provincia | comune | foglio | particella | subalterno | tipo_catasto |
|---|---|---|---|---|---|---|---|
| x-1-F | visura-storica | RM | Velletri | 103 | 113 | 501 | F |
| x-1-T | visura-storica | RM | Velletri | 103 | 113 | 501 | T |
| x-2-F | visura-storica | RM | Roma | 1 | 2 |  | F |
| x-3 | visura-storica | RM | Roma | 5 | 6 |  | T |

<!-- sister-batch-table {"command": "soggetto"} -->
| id | codice_fiscale |
|---|---|
| s-1 | RSSMRA80A01H501U |
| s-2 | VRDLGU80A01H501V |
"""
    stored = {("velletri", "RM", "103", "113", "501", "F"), ("roma", "RM", "5", "6", "", "F")}
    drop, counts = available_lines(text, {"RSSMRA80A01H501U"}, stored)
    # x-1 pair (one alternative stored), s-1 answered; x-3 is Terreni but only Fabbricati is stored: stays
    assert drop == [6, 7, 14]
    assert counts == {"subject": 2, "parcel": 4, "other": 0}
