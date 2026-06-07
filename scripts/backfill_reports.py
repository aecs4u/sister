"""Backfill visura_documents from /data/aecs4u.it/sister/reports/ using rename_map.json.

For each file in the rename_map old_to_new:
  - Parses the file (P7M/XML via _parse_visura_xml, PDF via filename convention)
  - Derives structured fields from the new meaningful filename
  - Inserts a row into visura_documents (skips duplicates by file_path)

Usage:
    uv run python scripts/backfill_reports.py [--dry-run]
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

REPORTS_DIR = Path("/data/aecs4u.it/sister/reports")
RENAME_MAP_PATH = REPORTS_DIR / "rename_map.json"

# Map new-name prefix → (document_type, tipo_catasto)
_TYPE_MAP = {
    "vs_att":           ("visura_soggetto",        "E"),
    "vs_sto":           ("visura_soggetto",        "E"),
    "vs_sin":           ("visura_soggetto",        "E"),
    "vi_att_fab":       ("visura_fabbricati",      "F"),
    "vi_att_ter":       ("visura_terreni",         "T"),
    "vi_sto":           ("visura_storica",         "F"),
    "elenco_fab":       ("elenco_immobili",        "F"),
    "planimetria_elab": ("elaborato_planimetrico", "F"),
    "planimetria":      ("planimetria",            "F"),
    "epa":              ("epa",                    "F"),
}

# Immobile pattern: <tipo>_<PROV>[_SEZ]_FG<n>_PT<n>[_SUB...].<ext>
# e.g. vi_att_fab_RA_FG104_PT2154_SUB1_4.pdf
#      vi_att_fab_RA_SAVIO_FG002_PT246.pdf  (SAVIO = sezione)
_RE_IMMOBILE = re.compile(
    r"^(?P<tipo>[a-z_]+)"
    r"_(?P<prov>[A-Z]{2})"
    r"(?:_(?P<sez>[A-Z]+(?:[A-Z0-9]*)))?"   # optional sezione (alpha only, before FG)
    r"_FG(?P<foglio>\d+)"
    r"_PT(?P<particella>\d+)"
    r"(?P<sub>(?:_SUB[\d_]+)?)?"
    r"\.\w+$"
)

# Soggetto pattern: <tipo>_<COGNOME>_<Nome>_<CF>[_PROV].<ext>
# e.g. vs_att_FARISELLI_Elisa_FRSLSE77B54E730C.pdf
#      vs_sin_FARISELLI_Elisa_FRSLSE77B54E730C_RA.pdf
_RE_SOGGETTO = re.compile(
    r"^(?P<tipo>[a-z_]+)"
    r"_(?P<cognome>[A-Z][A-Z0-9]*(?:_[A-Z][A-Z0-9]*)*)"
    r"_(?P<nome>[A-Z][a-z]+(?:[A-Z][a-z]+)*)"
    r"_(?P<cf>[A-Z0-9]{16})"
    r"(?:_(?P<prov>[A-Z]{2}))?"
    r"\.\w+$"
)


def _parse_new_name(new_name: str) -> dict:
    """Extract structured fields from the new meaningful filename."""
    stem = Path(new_name).stem
    ext = Path(new_name).suffix.lower()
    result: dict = {"file_format": ext.lstrip(".").upper()}

    # Identify tipo prefix
    tipo_key = next((k for k in _TYPE_MAP if stem.startswith(k + "_")), None)
    if tipo_key:
        doc_type, tipo_catasto = _TYPE_MAP[tipo_key]
        result["document_type"] = doc_type
        result["tipo_catasto"] = tipo_catasto
        result["oggetto"] = new_name
    else:
        result["document_type"] = "visura"
        result["tipo_catasto"] = ""
        result["oggetto"] = new_name

    # Try immobile pattern
    m = _RE_IMMOBILE.match(new_name)
    if m:
        result["provincia"] = m.group("prov") or ""
        sez = m.group("sez") or ""
        # Distinguish sezione from a comune abbreviation: sezione is 1-2 alpha chars
        # SAVIO is a comune (locality), single/double alpha sezione e.g. "A", "RA"
        if sez and len(sez) <= 2 and sez.isalpha():
            result["sezione_urbana"] = sez
        elif sez:
            result["comune"] = sez.title()  # e.g. SAVIO → Savio
        result["foglio"] = m.group("foglio").lstrip("0") or m.group("foglio")
        result["particella"] = m.group("particella").lstrip("0") or m.group("particella")
        sub_raw = (m.group("sub") or "").lstrip("_SUB").replace("_", "/")
        result["subalterno"] = sub_raw
        return result

    # Try soggetto pattern
    m = _re_soggetto_match(stem)
    if m:
        result["oggetto"] = f"{m['cognome']} {m['nome']} ({m['cf']})"
        result["provincia"] = m.get("prov") or ""
        return result

    return result


def _re_soggetto_match(stem: str) -> dict | None:
    """Extract soggetto fields from a stem like vs_att_COGNOME_Nome_CF or vs_att_ALTRAVIA_SERVIZI_SRL_12485671007."""
    # Try the standard pattern first
    m = _RE_SOGGETTO.match(stem + ".pdf")
    if m:
        return m.groupdict()
    # Fallback: anything after the tipo_ prefix is the soggetto identifier
    for tipo_key in _TYPE_MAP:
        if stem.startswith(tipo_key + "_"):
            rest = stem[len(tipo_key) + 1:]
            return {"cognome": rest, "nome": "", "cf": "", "prov": ""}
    return None


async def run(dry_run: bool = False):
    from sister.database import init_db, _get_session_factory
    from sister.utils import _parse_visura_xml
    from sister.db_models import VisuraDocumentDB
    from sqlalchemy import select

    os.environ.setdefault("SISTER_DB_PATH", "/data/aecs4u.it/sister/data/sister.sqlite")

    await init_db()
    session_factory = _get_session_factory()

    rename_map = json.loads(RENAME_MAP_PATH.read_text())
    old_to_new: dict[str, str] = rename_map["old_to_new"]

    # Pre-fetch existing file_paths to avoid duplicates
    async with session_factory() as session:
        existing = set(
            (await session.execute(select(VisuraDocumentDB.file_path))).scalars().all()
        )
        existing.discard(None)

    inserted = 0
    skipped = 0

    for old_name, new_name in old_to_new.items():
        file_path = REPORTS_DIR / old_name
        if not file_path.exists():
            print(f"  MISSING  {old_name}")
            continue

        abs_path = str(file_path.resolve())
        if abs_path in existing:
            print(f"  EXISTS   {old_name}")
            skipped += 1
            continue

        # Parse structured fields from new filename
        fields = _parse_new_name(new_name)
        fields["filename"] = old_name
        fields["file_path"] = abs_path
        fields["file_size"] = file_path.stat().st_size
        fields["richiesta_del"] = datetime.fromtimestamp(file_path.stat().st_mtime).strftime("%Y-%m-%d")

        # For P7M/XML: parse XML content
        xml_data: dict | None = None
        ext = file_path.suffix.lower()
        if ext in (".p7m", ".xml"):
            xml_data = _parse_visura_xml(abs_path)
            if xml_data:
                # Override with parsed values (more accurate than filename heuristics)
                for key in ("provincia", "comune", "foglio", "particella", "subalterno",
                            "sezione_urbana", "tipo_catasto", "document_type"):
                    if xml_data.get(key):
                        fields[key] = xml_data[key]
                fields["xml_content"] = xml_data.get("xml_content", "")

        intestati_json = None
        dati_immobile_json = None
        if xml_data:
            intestati = xml_data.get("intestati", [])
            if intestati:
                intestati_json = json.dumps(intestati, ensure_ascii=False)
            immobile = xml_data.get("immobile") or {}
            classamento = xml_data.get("classamento") or []
            indirizzo = xml_data.get("indirizzo") or ""
            if immobile or classamento:
                dati_immobile_json = json.dumps(
                    {"immobile": immobile, "classamento": classamento, "indirizzo": indirizzo},
                    ensure_ascii=False
                )

        row = VisuraDocumentDB(
            document_type=fields.get("document_type", "visura"),
            file_format=fields.get("file_format", ext.lstrip(".").upper()),
            filename=fields["filename"],
            file_path=fields["file_path"],
            file_size=fields.get("file_size"),
            oggetto=fields.get("oggetto") or new_name,
            richiesta_del=fields.get("richiesta_del"),
            provincia=fields.get("provincia") or "",
            comune=fields.get("comune") or "",
            foglio=fields.get("foglio") or "",
            particella=fields.get("particella") or "",
            subalterno=fields.get("subalterno") or "",
            sezione_urbana=fields.get("sezione_urbana") or "",
            tipo_catasto=fields.get("tipo_catasto") or "",
            intestati_json=intestati_json,
            dati_immobile_json=dati_immobile_json,
            xml_content=fields.get("xml_content") or None,
        )

        action = "DRY-RUN" if dry_run else "INSERT "
        print(f"  {action}  {old_name:45s} → {new_name}")
        print(f"           type={row.document_type} prov={row.provincia} "
              f"FG={row.foglio} PT={row.particella} sub={row.subalterno}")

        if not dry_run:
            async with session_factory() as session:
                session.add(row)
                await session.commit()
            inserted += 1

    print(f"\nDone: {inserted} inserted, {skipped} skipped (already in DB)")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    asyncio.run(run(dry_run=dry_run))
