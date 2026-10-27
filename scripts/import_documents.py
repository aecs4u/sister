#!/usr/bin/env python3
"""Index the files in the SISTER documents directory in PostgreSQL.

The source files remain in place. PDF, P7M, XML, ZIP and fiducial DAT files are
indexed; generated ``*.plan.json`` sidecars are deliberately excluded. A document
is stored by file name (the documents directory is configuration); XML content
is kept in the archive (``document_content``) and decomposed into the archived
element tree (``document_xml_node`` / ``document_xml_attribute``) so the import is
searchable without losing the raw document content.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env", override=False)

from lxml import etree
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sister.db_models import Document, DocumentContent
from sister.document_scope import subject_search_scope
from sister.reference import clean, ensure_lookup, get_or_create_cadastral_unit
from sister.result_parsers import parse_it_date
from sister.utils import _document_key, _parse_visura_pdf, _parse_visura_xml
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


def is_extensionless_copy(path: Path) -> bool:
    """True for a bare-name copy of a payload that also exists as ``<name>.xml`` / ``<name>.p7m``.

    ``file_kind`` sniffs the content, so these copies would be indexed as a second XML document.
    """
    if path.suffix:
        return False
    return any(path.with_name(f"{path.name}{ext}").exists() for ext in (".xml", ".p7m"))


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
    if path.suffix.lower() == ".dat":
        return "DAT"
    return "OTHER"


def guess_type(filename: str) -> str:
    name = filename.lower()
    if name.endswith(".dat"):
        return "elenco_immobili"
    if name.startswith("el_sub_"):
        return "elenco_subalterni"
    if name.startswith("punti_fiduciali_"):
        return "fiduciali"
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
    # parser's display-oriented 50k preview.  A P7M is read in memory by the parser,
    # whose content is already available via parsed data.
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
        "cadastre_type": parsed.get("tipo_catasto"),
        "province": parsed.get("provincia"),
        "municipality": parsed.get("comune"),
        "sheet": parsed.get("foglio"),
        "parcel": parsed.get("particella"),
        "subunit": parsed.get("subalterno"),
        "section": parsed.get("sezione_urbana"),
    }
    if not any(clean(value) for value in fields.values()):
        return None
    return await get_or_create_cadastral_unit(session, **fields)


async def import_documents(files: list[Path], paired_xml: dict[str, Path]) -> dict[str, int]:
    from sister.database import _get_engine, _get_session_factory, init_db

    await init_db()
    engine = _get_engine()
    session_factory = _get_session_factory()
    stats = {"indexed": 0, "metadata": 0, "xml_flattened": 0, "skipped": 0}
    async with session_factory() as session:
        by_filename = {row.file_name: row for row in (await session.scalars(select(Document))).all()}
        for path in files:
            kind = file_kind(path)
            parsed: dict[str, Any] | None = None
            parse_path = paired_xml.get(path.stem) or paired_xml.get(_document_key(path.stem), path)

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
            filename_type = guess_type(path.name)
            document_type = parsed.get("tipo") or filename_type
            if filename_type in {"elenco_subalterni", "elenco_immobili"}:
                document_type = filename_type
            row = by_filename.get(path.name)

            if row is None:
                row = Document(
                    document_type=await ensure_lookup(session, "document_type", document_type),
                    file_format=kind,
                    file_name=path.name,
                    file_size=path.stat().st_size,
                )
                session.add(row)
                await session.flush()
                by_filename[path.name] = row
                stats["indexed"] += 1
            else:
                row.file_size = path.stat().st_size
                if not row.file_format:
                    row.file_format = kind
                if not row.document_type or filename_type in {"elenco_subalterni", "elenco_immobili"}:
                    row.document_type = await ensure_lookup(session, "document_type", document_type)

            if document_type == "visura_soggetto":
                scope = subject_search_scope(path.name, request_province=parsed.get("provincia"))
                row.subject_search_scope = (
                    scope["search_scope"] if scope["search_scope"] != "unknown" else None
                )
                row.subject_search_province = scope["search_province"] or None

            if parsed and (xml_content or any(parsed.get(key) for key in ("foglio", "particella"))):
                if row.cadastral_unit_id is None or not row.view_subtype:
                    stats["metadata"] += 1
                row.cadastral_unit_id = await location_for(session, parsed)
                row.view_subtype = clean(parsed.get("visura_subtype")) or row.view_subtype
                row.reference_date = parse_it_date(parsed.get("situazione_al")) or row.reference_date
                if xml_content:
                    await session.merge(DocumentContent(document_id=row.id, content=xml_content))
                await session.flush()
                if xml_content and await flatten_xml(session, row.id, xml_content):
                    stats["xml_flattened"] += 1

        await session.commit()
    await engine.dispose()
    return stats


async def async_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--documents", default=str(PROJECT_ROOT / "documents"))
    args = parser.parse_args()

    documents_dir = resolve_path(args.documents)
    files = sorted(
        path
        for path in documents_dir.rglob("*")
        if path.is_file()
        and not path.name.endswith(".plan.json")
        and not is_extensionless_copy(path)
        and file_kind(path) in {"PDF", "P7M", "XML", "ZIP", "DAT"}
    )
    # PDFs inherit their metadata from the signed document with the same stem (read in memory for a .p7m);
    # the key without the request date also pairs a PDF and a P7M that were requested on different days
    xml_by_stem: dict[str, Path] = {}
    for path in files:
        if file_kind(path) in {"XML", "P7M"}:
            xml_by_stem[path.stem] = path
            xml_by_stem.setdefault(_document_key(path.stem), path)

    print(f"files={len(files)} xml_pairs={len(xml_by_stem)}")
    print("postgresql", await import_documents(files, xml_by_stem))


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
