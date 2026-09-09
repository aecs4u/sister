#!/usr/bin/env python3
"""Index the files in the SISTER documents directory in SQLite and PostgreSQL.

The source files remain in place.  PDF, P7M, XML and ZIP files are indexed;
generated ``*.plan.json`` sidecars are deliberately excluded.  XML content is
kept in document_metadata and decomposed into document_xml_nodes and
document_xml_attributes so the import is searchable without losing the raw
document content.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from lxml import etree
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from sister.db_models import CadastralLocation, DocumentMetadata, VisuraDocument
from sister.utils import _parse_visura_pdf, _parse_visura_xml
from sister.visura_xml_models import DocumentXmlAttribute, DocumentXmlNode


def resolve_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.exists():
        return candidate.resolve()
    # The production data mount is exposed at /data in the application, while
    # this workstation exposes the same mount through /media/emanuele/storage.
    if str(candidate).startswith("/data/"):
        fallback = Path("/media/emanuele/storage") / str(candidate).lstrip("/")
        if fallback.exists():
            return fallback.resolve()
    raise FileNotFoundError(path)


def file_kind(path: Path) -> str:
    header = path.read_bytes()[:8]
    if header.startswith(b"%PDF"):
        return "PDF"
    if header.startswith(b"PK"):
        return "ZIP"
    if header.lstrip().startswith(b"<"):
        return "XML"
    if path.suffix.lower() == ".p7m":
        return "P7M"
    return "OTHER"


def guess_type(filename: str) -> str:
    name = filename.lower()
    for prefix, document_type in (
        ("vi_att_fab", "visura_fabbricati"),
        ("vi_sto_fab", "visura_fabbricati"),
        ("vi_att_ter", "visura_terreni"),
        ("vi_sto_ter", "visura_terreni"),
        ("vs_att", "visura_soggetto"),
        ("vs_sin", "visura_soggetto"),
        ("vs_sto", "visura_soggetto"),
        ("sogg", "visura_soggetto"),
        ("pnf", "visura_pnf"),
    ):
        if prefix in name:
            return document_type
    return "visura"


def read_xml_content(path: Path, parsed: dict[str, Any] | None) -> str | None:
    if not parsed or not parsed.get("xml_content"):
        return None
    # For plain XML files this preserves the complete document rather than the
    # parser's display-oriented 50k preview.  P7M parsing may have produced an
    # extracted sibling, whose content is already available via parsed data.
    if file_kind(path) == "XML":
        return path.read_text(encoding="utf-8", errors="ignore").replace("\x00", "")
    return str(parsed["xml_content"]).replace("\x00", "")


def parse_xml_tree(content: str) -> etree._Element | None:
    try:
        return etree.fromstring(
            content.encode("utf-8", "replace"),
            etree.XMLParser(recover=True, resolve_entities=False, no_network=True),
        )
    except (etree.XMLSyntaxError, ValueError):
        return None


async def flatten_xml(session: AsyncSession, document_id: int, content: str | None) -> bool:
    if not content:
        return False
    root = parse_xml_tree(content)
    if root is None:
        return False

    existing_root = await session.scalar(
        select(DocumentXmlNode.id).where(DocumentXmlNode.document_id == document_id).limit(1)
    )
    if existing_root is not None:
        return True

    old_nodes = (await session.scalars(select(DocumentXmlNode).where(DocumentXmlNode.document_id == document_id))).all()
    old_ids = [node.id for node in old_nodes]
    if old_ids:
        await session.execute(
            delete(DocumentXmlAttribute).where(DocumentXmlAttribute.node_id.in_(old_ids))
        )
    await session.execute(
        delete(DocumentXmlNode).where(DocumentXmlNode.document_id == document_id)
    )

    def local_name(tag: Any) -> str:
        return etree.QName(tag).localname if isinstance(tag, str) else str(tag)

    async def visit(element: etree._Element, parent_id: int | None, ordinal: int) -> None:
        node = DocumentXmlNode(
            document_id=document_id,
            parent_id=parent_id,
            ordinal=ordinal,
            tag=local_name(element.tag),
            text=(element.text or "").strip() or None,
        )
        session.add(node)
        await session.flush()
        for name, value in element.attrib.items():
            session.add(DocumentXmlAttribute(node_id=node.id, name=local_name(name), value=value))
        for child_ordinal, child in enumerate(element):
            await visit(child, node.id, child_ordinal)

    await visit(root, None, 0)
    return True


async def location_for(session: AsyncSession, parsed: dict[str, Any]) -> int | None:
    fields = {
        "cadastre_type": parsed.get("tipo_catasto") or "",
        "province": parsed.get("provincia") or "",
        "municipality": parsed.get("comune") or "",
        "sheet": parsed.get("foglio") or "",
        "parcel": parsed.get("particella") or "",
        "subunit": parsed.get("subalterno") or "",
        "section": parsed.get("sezione_urbana") or "",
    }
    if not any(fields.values()):
        return None
    row = await session.scalar(
        select(CadastralLocation).where(
            *(getattr(CadastralLocation, key) == value for key, value in fields.items())
        )
    )
    if row is None:
        row = CadastralLocation(**fields)
        session.add(row)
        await session.flush()
    return row.id


async def import_one_database(database_url: str, files: list[Path], paired_xml: dict[str, Path]) -> dict[str, int]:
    engine = create_async_engine(database_url, future=True)
    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    stats = {"indexed": 0, "path_repaired": 0, "metadata": 0, "xml_flattened": 0, "skipped": 0}
    async with session_factory() as session:
        by_filename = {row.filename: row for row in (await session.scalars(select(VisuraDocument))).all()}
        for path in files:
            kind = file_kind(path)
            parsed: dict[str, Any] | None = None
            parse_path = paired_xml.get(path.stem, path)

            if kind == "XML":
                parsed = _parse_visura_xml(str(path))
            elif kind == "P7M":
                if parse_path != path and parse_path.exists():
                    parsed = _parse_visura_xml(str(parse_path))
                else:
                    parsed = _parse_visura_xml(str(path))
            elif kind == "PDF":
                if parse_path != path and parse_path.exists():
                    parsed = _parse_visura_xml(str(parse_path))
                else:
                    parsed = _parse_visura_pdf(str(path))

            parsed = parsed or {}
            xml_content = read_xml_content(parse_path if parse_path != path else path, parsed)
            document_type = parsed.get("tipo") or guess_type(path.name)
            row = by_filename.get(path.name)

            if row is None:
                row = VisuraDocument(
                    document_type=document_type,
                    file_format=kind,
                    filename=path.name,
                    file_path=str(path),
                    file_size=path.stat().st_size,
                )
                session.add(row)
                await session.flush()
                by_filename[path.name] = row
                stats["indexed"] += 1
            else:
                if row.file_path != str(path):
                    row.file_path = str(path)
                    stats["path_repaired"] += 1
                row.file_size = path.stat().st_size
                if not row.file_format:
                    row.file_format = kind
                if not row.document_type:
                    row.document_type = document_type

            if parsed and (xml_content or any(parsed.get(key) for key in ("foglio", "particella"))):
                metadata = await session.get(DocumentMetadata, row.id)
                location_id = await location_for(session, parsed)
                if metadata is None:
                    metadata = DocumentMetadata(id=row.id)
                    session.add(metadata)
                    stats["metadata"] += 1
                metadata.location_id = location_id
                metadata.view_subtype = parsed.get("visura_subtype") or metadata.view_subtype
                metadata.reference_date = parsed.get("situazione_al") or metadata.reference_date
                if xml_content:
                    metadata.content = xml_content
                await session.flush()
                if xml_content and await flatten_xml(session, row.id, xml_content):
                    stats["xml_flattened"] += 1

        await session.commit()
    await engine.dispose()
    return stats


async def async_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--documents", default="/data/aecs4u.it/sister/documents")
    parser.add_argument("--sqlite", default="/data/aecs4u.it/sister/data/sister.sqlite")
    args = parser.parse_args()
    load_dotenv()

    documents_dir = resolve_path(args.documents)
    sqlite_path = resolve_path(args.sqlite)
    files = sorted(
        path
        for path in documents_dir.rglob("*")
        if path.is_file()
        and not path.name.endswith(".plan.json")
        and file_kind(path) in {"PDF", "P7M", "XML", "ZIP"}
    )
    xml_by_stem = {path.stem: path for path in files if file_kind(path) == "XML"}

    sqlite_url = f"sqlite+aiosqlite:///{sqlite_path}"
    pg_dsn = os.environ.get("DATABSE_DSN")
    if not pg_dsn:
        raise RuntimeError("DATABSE_DSN is not set")
    pg_url = pg_dsn

    print(f"files={len(files)} xml_pairs={len(xml_by_stem)}")
    print("sqlite", await import_one_database(sqlite_url, files, xml_by_stem))
    print("postgresql", await import_one_database(pg_url, files, xml_by_stem))


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
