#!/usr/bin/env python3
"""Import exported query JSON files into the configured PostgreSQL database."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env", override=False)

from sqlalchemy import func, select  # noqa: E402

from sister.database import (  # noqa: E402
    _get_session_factory,
    compute_cache_key,
    count_responses,
    init_db,
    save_request,
    save_response,
)
from sister.db_models import PageVisit, VisuraOwner, VisuraProperty  # noqa: E402

REQUEST_TYPE_BY_PREFIX = {
    "req": "visura",
    "intestati": "intestati",
    "soggetto": "soggetto",
    "pnf": "persona_giuridica",
    "eimm": "elenco_immobili",
    "richieste": "richieste",
}

PROVINCE_BY_COMUNE = {
    "AGRIGENTO": "Agrigento",
    "PALERMO": "Palermo",
    "RAVENNA": "Ravenna",
    "ROMA": "Roma",
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_response_payload(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("request_id"), str)
        and "data" in value
        and ("success" in value or "status" in value)
    )


def _iter_response_payloads(source: Path) -> Iterable[tuple[Path, dict[str, Any]]]:
    for path in sorted(source.glob("*.json")):
        payload = _load_json(path)
        if _is_response_payload(payload):
            yield path, payload
        elif isinstance(payload, dict):
            for value in payload.values():
                if _is_response_payload(value):
                    yield path, value


def _request_type(request_id: str) -> str:
    parts = request_id.split("_")
    if len(parts) >= 2 and parts[0] == "wf":
        return f"workflow_{parts[1]}"
    return REQUEST_TYPE_BY_PREFIX.get(parts[0], parts[0] or "query")


def _success(payload: dict[str, Any]) -> bool:
    if "success" in payload:
        return bool(payload["success"])
    return payload.get("status") == "completed"


def _form_elements(data: dict[str, Any]) -> Iterable[dict[str, Any]]:
    visits = data.get("page_visits", [])
    if not isinstance(visits, list):
        return
    for visit in visits:
        if not isinstance(visit, dict):
            continue
        elements = visit.get("form_elements", [])
        if isinstance(elements, list):
            yield from (element for element in elements if isinstance(element, dict))


def _last_form_value(data: dict[str, Any], *names: str, skip_province_wide: bool = False) -> str:
    value = ""
    for element in _form_elements(data):
        if element.get("name") not in names:
            continue
        raw = str(element.get("value") or "").strip()
        if not raw or (skip_province_wide and raw.upper().startswith("TUTTA LA PROVINCIA")):
            continue
        value = raw
    return value


def _clean_comune(value: str) -> str:
    if not value or value.upper().startswith("TUTTA LA PROVINCIA"):
        return ""
    return value.split("(", 1)[0].strip()


def _split_foglio(raw: Any) -> tuple[str, str | None]:
    value = str(raw or "").strip()
    if "/" not in value:
        return value, None
    prefix, foglio = value.split("/", 1)
    return foglio.strip(), prefix.strip() or None


def _first_mapping(*items: Any) -> dict[str, Any]:
    return next((item for item in items if isinstance(item, dict)), {})


def _first_immobile(data: dict[str, Any]) -> dict[str, Any]:
    immobili = data.get("immobili")
    if isinstance(immobili, list):
        for item in immobili:
            if isinstance(item, dict) and item:
                return item
    return _first_mapping(data.get("immobile"))


def _infer_from_request_id(request_id: str) -> dict[str, str]:
    parts = request_id.split("_")
    if len(parts) >= 6 and parts[0] == "wf":
        return {
            "provincia": parts[2],
            "comune": parts[2].upper(),
            "foglio": parts[3],
            "particella": parts[4],
        }
    return {}


def _infer_request_fields(payload: dict[str, Any]) -> dict[str, Any]:
    request_id = payload["request_id"]
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    inferred = _infer_from_request_id(request_id)
    first = _first_immobile(data)
    foglio, section_from_sheet = _split_foglio(first.get("Foglio"))
    comune = _clean_comune(_last_form_value(data, "denomComune", "comuneCat", skip_province_wide=True))

    fields = {
        "request_id": request_id,
        "request_type": _request_type(request_id),
        "tipo_catasto": payload.get("tipo_catasto") or "",
        "provincia": data.get("provincia") or inferred.get("provincia") or "",
        "comune": data.get("comune") or comune or inferred.get("comune") or "",
        "foglio": data.get("foglio") or _last_form_value(data, "foglio") or inferred.get("foglio") or foglio,
        "particella": (
            data.get("particella")
            or _last_form_value(data, "particella1")
            or inferred.get("particella")
            or first.get("Particella")
            or data.get("soggetto")
            or ""
        ),
        "sezione": _last_form_value(data, "sezUrb", "sezione") or section_from_sheet,
        "subalterno": _last_form_value(data, "subalterno1") or first.get("Sub"),
    }
    if not fields["provincia"] and fields["comune"]:
        fields["provincia"] = PROVINCE_BY_COMUNE.get(str(fields["comune"]).upper(), "")

    cache_params = {
        "tipo_catasto": fields["tipo_catasto"],
        "provincia": fields["provincia"],
        "comune": fields["comune"],
        "foglio": fields["foglio"],
        "particella": fields["particella"],
        "sezione": fields["sezione"],
        "subalterno": fields["subalterno"],
    }
    if fields["request_type"] in {"soggetto", "persona_giuridica"} and data.get("soggetto"):
        key = "codice_fiscale" if fields["request_type"] == "soggetto" else "identificativo"
        cache_params[key] = data["soggetto"]
    fields["cache_key"] = compute_cache_key(fields["request_type"], **cache_params)
    return fields


def _load_manifest(path: Path | None) -> dict[str, dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_manifest(path: Path, manifest: dict[str, dict]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _manifest_key(path: Path) -> str:
    return str(path.resolve())


def _is_known(path: Path, manifest: dict) -> bool:
    entry = manifest.get(_manifest_key(path))
    if not entry:
        return False
    stat = path.stat()
    return entry.get("mtime") == stat.st_mtime and entry.get("size") == stat.st_size


async def populate(source: Path, dry_run: bool, manifest_path: Path | None = None) -> dict[str, int]:
    manifest = _load_manifest(manifest_path)
    payloads = list(_iter_response_payloads(source))
    new_payloads = [(path, payload) for path, payload in payloads if not _is_known(path, manifest)]
    if dry_run:
        return {
            "files_total": len({path for path, _ in payloads}),
            "files_new": len({path for path, _ in new_payloads}),
            "responses_new": len(new_payloads),
        }

    await init_db()
    processed: set[Path] = set()
    for path, payload in new_payloads:
        fields = _infer_request_fields(payload)
        await save_request(
            request_id=fields["request_id"],
            request_type=fields["request_type"],
            tipo_catasto=fields["tipo_catasto"],
            provincia=fields["provincia"],
            comune=fields["comune"],
            foglio=fields["foglio"],
            particella=fields["particella"],
            sezione=fields["sezione"],
            subalterno=fields["subalterno"],
            cache_key=fields["cache_key"],
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else None
        await save_response(
            request_id=fields["request_id"],
            success=_success(payload),
            tipo_catasto=fields["tipo_catasto"],
            data=data,
            error=payload.get("error"),
            export=False,
        )
        processed.add(path)
        if manifest_path:
            stat = path.stat()
            manifest[_manifest_key(path)] = {
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }

    if manifest_path and processed:
        _save_manifest(manifest_path, manifest)

    counts = await count_responses()
    async with _get_session_factory()() as session:
        properties = await session.scalar(select(func.count()).select_from(VisuraProperty)) or 0
        owners = await session.scalar(select(func.count()).select_from(VisuraOwner)) or 0
        page_visits = await session.scalar(select(func.count()).select_from(PageVisit)) or 0
    return {
        "files_new": len(processed),
        "files_skipped": len({path for path, _ in payloads}) - len(processed),
        "visura_requests": counts["total_requests"],
        "visura_responses": counts["total_responses"],
        "visura_properties": properties,
        "visura_owners": owners,
        "page_visits": page_visits,
    }


async def async_main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=PROJECT_ROOT / "outputs")
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    for key, value in (await populate(args.source, args.dry_run, args.manifest)).items():
        print(f"{key}: {value}")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
