"""Visura XML → typed document tables (shapes taken from real SISTER documents, with invented data)."""

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from sister import database, xml_ingest
from sister.db_models import DocumentMetadata, VisuraDocument

XML = Path(__file__).parent / "fixtures" / "visura_xml"


def _read(name: str) -> str:
    return (XML / f"{name}.xml").read_text(encoding="utf-8")


# --- pure parsing -----------------------------------------------------------------------------------------


def test_parse_fabbricati_storica_separates_current_and_historical_owners():
    parsed = xml_ingest.parse_visura_xml(_read("fabbricati_storica"))

    assert parsed["kind"] == "fabbricati_storica"
    (unit,) = parsed["units"]
    assert unit["identifier"]["sheet"] == "103" and unit["identifier"]["section"] == "RA"
    assert unit["classification"]["Consistenza"] == {"Unita": "vani", "Valore": "6.0"}
    assert unit["address"] == "VIA EL ALAMEIN n. 2 Piano T-1"
    current, history = parsed["mutations"]
    assert current["index"] == "current" and [o["CF"] for o in current["owners"]] == [
        "RSSMRA70A01H501Z",
        "RSSNNA70A41H501Z",
    ]
    assert history["index"] == "1" and history["reference"]["status"] == "A"
    assert [a["address"] for a in parsed["history_addresses"]] == ["VIA EL ALAMEIN n. 2"]


def test_parse_terreni_attuale_keeps_every_classification_and_the_owners_on_the_parcel():
    parsed = xml_ingest.parse_visura_xml(_read("terreni_attuale"))

    assert parsed["kind"] == "terreni_attuale"
    (parcel,) = parsed["units"]
    assert parcel["identifier"]["partita"] == "1234" and parcel["identifier"]["section"] == "SAVIO"
    assert [c["Qualita"] for c in parcel["classifications"]] == ["SEMINATIVO", "VIGNETO"]
    assert [o["Nominativo"] for m in parcel["mutations"] for o in m["owners"]] == ["ROSSI S.R.L."]


def test_parse_soggetto_groups_buildings_and_land():
    parsed = xml_ingest.parse_visura_xml(_read("soggetto_attuale"))

    assert parsed["subject"]["type"] == "person" and parsed["subject"]["CodiceFiscale"] == "RSSMRA70A01H501Z"
    buildings, land = parsed["groups"]
    assert [u["kind"] for u in buildings["units"]] == ["building", "building"]
    assert [u["kind"] for u in land["units"]] == ["land"]
    assert len(buildings["mutations"]) == 1 and land["mutations"] == []


@pytest.mark.parametrize("content", ["", "<not xml", "<Visura><Other/></Visura>"])
def test_parse_rejects_what_is_not_a_visura(content):
    assert xml_ingest.parse_visura_xml(content) is None


# --- database ---------------------------------------------------------------------------------------------


async def _document(session, xml: str) -> int:
    row = VisuraDocument(document_type="visura", file_format="XML", filename="x.xml")
    session.add(row)
    await session.flush()
    session.add(DocumentMetadata(id=row.id, content=xml))
    await session.flush()
    return row.id


async def _scalar(session, sql: str, **params):
    return (await session.execute(text(sql), params)).scalar_one()


@pytest.mark.usefixtures("fresh_db")
async def test_ingest_fabbricati_storica_fills_unit_owners_and_header():
    async with database._get_session_factory()() as session:
        doc = await _document(session, _read("fabbricati_storica"))
        counts = await xml_ingest.ingest_visura_xml(session, doc, _read("fabbricati_storica"))
        await session.commit()

        assert counts == {"units": 1, "parcels": 0, "mutations": 2, "owners": 3}
        classification = (
            await session.execute(
                text(
                    "SELECT category, cadastral_class, cadastral_income, consistency_value, consistency_unit, census_zone "
                    "FROM building_classifications WHERE current_state_id = :d"
                ),
                {"d": doc},
            )
        ).one()
        assert tuple(classification) == ("A/4", "04", Decimal("557.77"), Decimal("6.0"), "vani", "1")
        assert await _scalar(session, "SELECT total_area FROM building_surfaces WHERE current_state_id = :d", d=doc) == 120
        assert await _scalar(session, "SELECT address_text FROM building_current_states WHERE id = :d", d=doc) == (
            "VIA EL ALAMEIN n. 2 Piano T-1"
        )
        assert await _scalar(session, "SELECT count(*) FROM building_addresses WHERE document_id = :d", d=doc) == 1
        assert await _scalar(session, "SELECT count(*) FROM related_parcels WHERE current_state_id = :d", d=doc) == 1
        # the one-half owner with an end date keeps start/end of the right
        right = (
            await session.execute(
                text(
                    "SELECT r.ownership_share, r.start_date, r.end_date, s.fiscal_code, s.gender "
                    "FROM property_owners o JOIN ownership_rights r ON r.id = o.right_id "
                    "JOIN cadastral_subjects s ON s.id = o.subject_id "
                    "JOIN ownership_mutations m ON m.id = o.mutation_id "
                    "WHERE m.document_id = :d AND m.mutation_index = 'current' AND o.owner_index = '1'"
                ),
                {"d": doc},
            )
        ).one()
        assert tuple(right) == ("1/2", "", "31/12/2023", "RSSMRA70A01H501Z", "M")
        meta = (
            await session.execute(
                text(
                    "SELECT title, registry_view_type, municipality_code, requester, liquidation_units "
                    "FROM document_metadata WHERE id = :d"
                ),
                {"d": doc},
            )
        ).one()
        assert tuple(meta) == ("VISURA STORICA PER IMMOBILE", "STORICA", "H199", "STUDIO ROSSI", 1)


@pytest.mark.usefixtures("fresh_db")
async def test_ingest_soggetto_fills_subject_groups_units_and_land():
    async with database._get_session_factory()() as session:
        doc = await _document(session, _read("soggetto_attuale"))
        counts = await xml_ingest.ingest_visura_xml(session, doc, _read("soggetto_attuale"))
        await session.commit()

        assert counts == {"units": 2, "parcels": 1, "mutations": 1, "owners": 1}
        subject = (
            await session.execute(
                text(
                    "SELECT s.fiscal_code, s.last_name, s.first_name, s.date_of_birth, g.municipality, "
                    "s.birth_municipality_code FROM document_subjects d JOIN cadastral_subjects s ON s.id = d.subject_id "
                    "LEFT JOIN geographic_places g ON g.id = s.birth_place_id WHERE d.id = :d"
                ),
                {"d": doc},
            )
        ).one()
        assert tuple(subject) == ("RSSMRA70A01H501Z", "ROSSI", "MARIO", "01/01/1970", "ROMA", "H501")
        assert await _scalar(session, "SELECT count(*) FROM property_groups WHERE document_id = :d", d=doc) == 2
        categories = (
            await session.execute(
                text(
                    "SELECT c.category FROM building_classifications c JOIN building_units u ON u.id = c.building_unit_id "
                    "JOIN property_groups g ON g.id = u.group_id WHERE g.document_id = :d ORDER BY u.unit_index"
                ),
                {"d": doc},
            )
        ).scalars().all()
        assert categories == ["A/4", "C/6"]
        assert await _scalar(session, "SELECT quality FROM land_classifications") == "ULIVETO"
        assert await _scalar(session, "SELECT planimetria FROM building_surfaces WHERE total_area = 120") == "PRESENTE"


@pytest.mark.usefixtures("fresh_db")
async def test_ingest_terreni_attuale_and_company_owner():
    async with database._get_session_factory()() as session:
        doc = await _document(session, _read("terreni_attuale"))
        await xml_ingest.ingest_visura_xml(session, doc, _read("terreni_attuale"))
        await session.commit()

        assert await _scalar(session, "SELECT partita FROM land_parcels WHERE document_id = :d", d=doc) == "1234"
        assert await _scalar(session, "SELECT count(*) FROM land_classifications") == 2
        owner = (
            await session.execute(
                text(
                    "SELECT s.subject_type, s.gender FROM property_owners o JOIN cadastral_subjects s ON s.id = o.subject_id"
                )
            )
        ).one()
        assert tuple(owner) == ("legal_entity", None)


@pytest.mark.usefixtures("fresh_db")
async def test_reingesting_a_document_replaces_its_rows():
    async with database._get_session_factory()() as session:
        doc = await _document(session, _read("soggetto_attuale"))
        first = await xml_ingest.ingest_visura_xml(session, doc, _read("soggetto_attuale"))
        second = await xml_ingest.ingest_visura_xml(session, doc, _read("soggetto_attuale"))
        await session.commit()

        assert first == second
        assert await _scalar(session, "SELECT count(*) FROM building_units") == 2
        assert await _scalar(session, "SELECT count(*) FROM property_owners") == 1
        assert await _scalar(session, "SELECT count(*) FROM cadastral_subjects WHERE fiscal_code = 'RSSMRA70A01H501Z'") == 1


@pytest.mark.usefixtures("fresh_db")
async def test_ingest_company_subject_document():
    async with database._get_session_factory()() as session:
        doc = await _document(session, _read("soggetto_azienda"))
        await xml_ingest.ingest_visura_xml(session, doc, _read("soggetto_azienda"))
        await session.commit()

        row = (
            await session.execute(
                text(
                    "SELECT s.fiscal_code, s.display_name, s.subject_type FROM document_subjects d "
                    "JOIN cadastral_subjects s ON s.id = d.subject_id"
                )
            )
        ).one()
        assert tuple(row) == ("00000000001", "ROSSI S.R.L.", "legal_entity")


@pytest.mark.usefixtures("fresh_db")
async def test_non_visura_xml_leaves_the_tables_alone():
    async with database._get_session_factory()() as session:
        doc = await _document(session, "<Other/>")
        assert await xml_ingest.ingest_visura_xml(session, doc, "<Other/>") == {}


@pytest.mark.usefixtures("fresh_db")
async def test_backfill_fills_documents_without_typed_rows_and_skips_the_done_ones():
    async with database._get_session_factory()() as session:
        todo = await _document(session, _read("terreni_attuale"))
        await _document(session, "<Other/>")
        await session.commit()

    first = await xml_ingest.backfill_documents()
    again = await xml_ingest.backfill_documents()

    assert first["documents"] == 1 and first["not_visura"] == 1 and first["failed"] == 0 and first["parcels"] == 1
    assert again["documents"] == 0  # the filled document is skipped; the non-visura one is looked at again, harmlessly
    forced = await xml_ingest.backfill_documents(force=True)
    assert forced["documents"] == 1
    async with database._get_session_factory()() as session:
        assert await _scalar(session, "SELECT count(*) FROM land_parcels WHERE document_id = :d", d=todo) == 1


def test_complete_content_rereads_a_legacy_cut_copy(tmp_path):
    full = "<Visura>" + "x" * 60_000 + "</Visura>"
    path = tmp_path / "doc.xml"
    path.write_text(full)

    assert xml_ingest._complete_content(full[:50_000], str(path)) == full
    assert xml_ingest._complete_content("<short/>", str(path)) == "<short/>"
    assert xml_ingest._complete_content(full[:50_000], None) == full[:50_000]
