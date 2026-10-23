"""Static guards for the flat/normalised schema migration (``alembic/versions/20261024_flat_normalised.py``).

The migration itself is verified against a copy of the database (see ``docs/database_design.md``); these tests keep the
pieces from drifting apart and keep the DDL honest about the design rules.
"""

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "alembic" / "versions"
SQL_DIR = ROOT / "alembic" / "sql" / "20261024_flat_normalised"


def _load_migration():
    spec = importlib.util.spec_from_file_location("flat_normalised_migration", VERSIONS / "20261024_flat_normalised.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _heads() -> list[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return ScriptDirectory.from_config(config).get_heads()


def _strip_sql_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def test_migration_steps_exist_in_order():
    migration = _load_migration()
    steps = migration.UP_STEPS + migration.DOWN_STEPS
    assert [name[:2] for name in migration.UP_STEPS] == ["01", "02", "03", "04", "05", "06"]
    for name in steps:
        assert (SQL_DIR / name).is_file(), name


def _history() -> list[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return [rev.revision for rev in ScriptDirectory.from_config(config).walk_revisions()]


def test_there_is_a_single_alembic_head_and_it_descends_from_the_flat_migration():
    heads = _heads()
    assert len(heads) == 1, f"several Alembic heads: {heads}"
    assert _load_migration().revision in _history()
    assert "20261026_flat_normalised_fixes" in _history()


def test_forward_fix_is_idempotent_by_construction():
    sql = (ROOT / "alembic" / "sql" / "20261026_flat_normalised_fixes" / "up.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS" in sql and "ADD COLUMN " not in sql.replace("ADD COLUMN IF NOT EXISTS", "")
    assert "CREATE TABLE IF NOT EXISTS sister.sister_request_jobs" in sql
    assert "to_regclass('sister_legacy." in sql  # backfill only while the legacy tables exist


def test_swap_and_downgrade_move_only_named_relations():
    for name in ("06_swap.sql", "07_down.sql"):
        sql = (SQL_DIR / name).read_text(encoding="utf-8")
        assert "= ANY (" in sql, f"{name} must restrict the schema move to an explicit list"


def test_schema_has_no_json_columns_outside_the_archive():
    schema = (SQL_DIR / "02_schema.sql").read_text(encoding="utf-8")
    working, archive = schema.split("-- Archive: raw payloads", 1)
    assert not re.search(r"\bjsonb?\b", _strip_sql_comments(working), re.I)
    assert "jsonb" in _strip_sql_comments(archive)  # the raw extraction payload is the one place JSON is allowed


def test_every_working_table_has_a_primary_key():
    schema = (SQL_DIR / "02_schema.sql").read_text(encoding="utf-8")
    for match in re.finditer(r"CREATE TABLE sister_(?:next|archive)\.(\w+) \((.*?)\n\);", schema, re.S):
        name, body = match.groups()
        if name == "migration_unparsed":  # audit list of values that could not be typed, no identity of its own
            continue
        assert "PRIMARY KEY" in body, name


def test_every_foreign_key_column_is_indexed():
    schema = (SQL_DIR / "02_schema.sql").read_text(encoding="utf-8")
    indexed = set()
    for table, cols in re.findall(r"CREATE (?:UNIQUE )?INDEX \w+ ON sister_\w+\.(\w+)(?: USING \w+)? \((\w+)", schema):
        indexed.add((table, cols))
    for match in re.finditer(r"CREATE TABLE sister_(?:next|archive)\.(\w+) \((.*?)\n\);", schema, re.S):
        table, body = match.groups()
        primary = re.search(r"PRIMARY KEY \(([^)]*)\)", body)
        leading_pk = primary.group(1).split(",")[0].strip() if primary else None
        unique_leading = {m.split(",")[0].strip() for m in re.findall(r"UNIQUE(?: NULLS NOT DISTINCT)? \(([^)]*)\)", body)}
        for line in body.splitlines():
            ref = re.match(r"\s+(\w+) \w+(?:\(\d+\))? (?:NOT NULL )?REFERENCES sister_\w+\.(\w+)", line)
            if not ref:
                continue
            column = ref.group(1)
            covered = (table, column) in indexed or column == leading_pk or column in unique_leading or "PRIMARY KEY" in line
            assert covered, f"{table}.{column} has a foreign key but no index"


def test_swap_never_names_postgis_or_alembic_tables():
    swap = (SQL_DIR / "06_swap.sql").read_text(encoding="utf-8")
    replaced = swap.split("replaced text[] := ARRAY[", 1)[1].split("];", 1)[0]
    names = set(re.findall(r"'([a-z_0-9]+)'", replaced))
    assert {"alembic_version", "spatial_ref_sys", "sister_request_jobs"}.isdisjoint(names)
    assert {"visura_requests", "cadastral_locations", "sister_owner_index"} <= names  # the legacy layout is covered
