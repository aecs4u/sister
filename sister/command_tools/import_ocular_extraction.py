"""Import an Ocular structured JSON result into Sister's flat and normalized tables.

The inspection, its title and lien are one ``mortgage_inspection`` row; every party becomes a ``subject`` (found by fiscal
code, else by name and birth date) linked by ``mortgage_inspection_party``; every property is a ``cadastral_unit`` linked
by ``mortgage_inspection_property``. The JSON as extracted is kept in ``sister_archive.document_extraction_payload``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete  # noqa: E402
from sqlmodel import select  # noqa: E402

from sister.database import _get_session_factory, document_file_path  # noqa: E402
from sister.db_models import (  # noqa: E402
    Document,
    DocumentExtraction,
    DocumentExtractionPayload,
    MortgageInspection,
    MortgageInspectionParty,
    MortgageInspectionProperty,
)
from sister.mortgage_note_projection import project_note_data  # noqa: E402
from sister.result_parsers import parse_it_date  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value




def _string(value: Any) -> str | None:
    return None if value is None else str(value)



def _date(value: Any):
    return parse_it_date(value)



def _datetime(value: Any):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _completed_at(run: dict[str, Any]) -> datetime | None:
    value = run.get("completed_at") or run.get("updated_at")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _file_hash_prefix(path: Path, length: int = 8) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:length]


async def import_extraction(document_id: int, structured_path: Path) -> int:
    structured_path = structured_path.resolve()
    data = _read_json(structured_path)
    base = structured_path.parent
    provenance_path = base / "provenance.json"
    provenance = _read_json(provenance_path) if provenance_path.is_file() else {}
    run_id = str(provenance.get("run_id") or "")
    if not run_id:
        raise ValueError("Ocular provenance.json does not contain run_id")

    run_path = structured_path.parents[1] / "runs" / run_id / "run.json"
    run = _read_json(run_path) if run_path.is_file() else {}
    schema_name = str(provenance.get("schema_name") or "ocular_structured")
    source_file = Path(str(data.get("source_file") or "")).name
    is_mortgage_inspection = schema_name == "ispezione_ipotecaria_schema" or (
        "ispezione" in data and "nota" in data
    )
    schema_metadata = provenance.get("schema_metadata") or {}
    schema_version = data.get("$schema_version") or schema_metadata.get("version")
    workflow_id = run.get("workflow_id") or run.get("mode")

    session_factory = _get_session_factory()
    async with session_factory() as session:
        document = (await session.execute(
            select(Document).where(Document.id == document_id)
        )).scalar_one_or_none()
        if document is None:
            raise ValueError(f"Sister document {document_id} does not exist")
        if source_file and Path(document.file_name or "").name != source_file:
            output_root_hash = structured_path.parents[1].name
            stored_path = document_file_path(document.file_name)
            content_hash_matches = (
                stored_path
                and Path(stored_path).is_file()
                and output_root_hash == _file_hash_prefix(Path(stored_path))
            )
            if not content_hash_matches:
                raise ValueError(
                    f"Ocular source file {source_file!r} does not match Sister document filename "
                    f"{document.file_name!r} or its content hash"
                )

        extraction = (await session.execute(
            select(DocumentExtraction).where(
                DocumentExtraction.document_id == document_id,
                DocumentExtraction.schema_name == schema_name,
                DocumentExtraction.run_id == run_id,
            )
        )).scalar_one_or_none()
        if extraction is None:
            extraction = DocumentExtraction(document_id=document_id, schema_name=schema_name, run_id=run_id)
            session.add(extraction)
            await session.flush()
        else:
            await session.execute(delete(MortgageInspectionProperty).where(MortgageInspectionProperty.inspection_id == extraction.id))
            await session.execute(delete(MortgageInspectionParty).where(MortgageInspectionParty.inspection_id == extraction.id))
            await session.execute(delete(MortgageInspection).where(MortgageInspection.extraction_id == extraction.id))

        extraction.provider = provenance.get("provider")
        extraction.model = _string(provenance.get("model") or run.get("structure_models"))
        extraction.schema_version = _string(schema_version)
        extraction.workflow_id = _string(workflow_id)
        extraction.output_path = str(structured_path)
        extraction.extracted_at = _completed_at(run)
        session.add(extraction)
        await session.flush()
        extraction_id = extraction.id
        await session.execute(delete(DocumentExtractionPayload).where(DocumentExtractionPayload.extraction_id == extraction_id))
        session.add(DocumentExtractionPayload(extraction_id=extraction_id, structured_data=data))

        await project_note_data(session, extraction_id, data, is_mortgage_inspection=is_mortgage_inspection)

        await session.commit()
    return extraction_id


def run(document_id: int, structured_json: Path) -> int:
    """Import one Ocular structured extraction into the document archive."""
    extraction_id = asyncio.run(import_extraction(document_id, structured_json))
    print(f"Imported structured extraction {extraction_id} for document {document_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document_id", type=int)
    parser.add_argument("structured_json", type=Path)
    args = parser.parse_args(argv)
    return run(args.document_id, args.structured_json)


if __name__ == "__main__":
    raise SystemExit(main())
