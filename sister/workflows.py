"""Workflow orchestrator for multi-step SISTER queries.

Owns preset definitions, step execution, drill-down, aggregation, and
persistence. Both the CLI and the API route delegate to `run_workflow()`.

Workflow state is persisted in `workflow_runs` / `workflow_steps` tables
so that interrupted runs can be resumed.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from .database import save_request, save_response
from .models import STEP_METADATA, WORKFLOW_PRESETS, WorkflowInput, _DEPTH_ORDER
from .services import VisuraService
from .utils import (
    run_elaborato_planimetrico,
    run_elenco_immobili,
    run_export_mappa,
    run_ispezione_ipotecaria,
    run_ispezioni,
    run_ispezioni_cartacee,
    run_originali_impianto,
    run_punti_fiduciali,
    run_ricerca_indirizzo,
    run_ricerca_mappa,
    run_ricerca_nota,
    run_visura,
    run_visura_immobile,
    run_visura_persona_giuridica,
    run_visura_soggetto,
)

logger = logging.getLogger("sister")

MAX_DRILL_DOWN = 20  # Safety limit for fan-out

# ---------------------------------------------------------------------------
# Property normalization (for drill-down)
# ---------------------------------------------------------------------------


def _normalize_property(row: dict) -> Optional[dict]:
    """Extract a normalized property dict from a soggetto/azienda result row.

    Returns None if the row doesn't contain enough location data.
    """
    prov = (row.get("Provincia") or "").strip()
    com = (row.get("Comune") or "").strip()
    fog = (row.get("Foglio") or "").strip()
    par = (row.get("Particella") or "").strip()
    if not (prov and com and fog and par):
        return None
    return {
        "provincia": prov,
        "comune": com,
        "foglio": fog,
        "particella": par,
        "subalterno": (row.get("Sub") or "").strip() or None,
        "tipo_catasto": (row.get("Tipo") or "").strip() or None,
        "sezione": (row.get("Sezione") or "").strip() or None,
    }


def _deduplicate_properties(props: list[dict]) -> list[dict]:
    """Deduplicate properties by (provincia, comune, foglio, particella, subalterno)."""
    seen: set[tuple] = set()
    result: list[dict] = []
    for p in props:
        key = (p["provincia"], p["comune"], p["foglio"], p["particella"], p.get("subalterno"))
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


def _step_key(step_name: str, **ctx) -> str:
    """Build a deterministic step key for persistence."""
    parts = [step_name]
    for k in sorted(ctx):
        v = ctx[k]
        if v:
            parts.append(f"{k}={v}")
    return ":".join(parts)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _build_aggregate(step_results: list[dict]) -> dict:
    """Build a normalized aggregate from step results.

    Returns:
        {
            "properties": [...],
            "owners": [...],
            "links": [{"property_key": ..., "owner_key": ..., "source_step": ...}],
            "addresses": [...],
            "risk_flags": [...],
        }
    """
    properties: list[dict] = []
    owners: list[dict] = []
    links: list[dict] = []
    addresses: list[dict] = []
    risk_flags: list[dict] = []
    prop_keys_seen: set[str] = set()
    owner_keys_seen: set[str] = set()

    def _owner_key(owner: dict) -> str:
        cf = (owner.get("Codice fiscale") or owner.get("Codice Fiscale") or "").strip()
        nome = (owner.get("Nominativo o denominazione") or owner.get("Nominativo") or "").strip()
        return cf or nome

    def _collect_owner(owner: dict, step_name: str, property_key: str = None):
        ok = _owner_key(owner)
        if not ok:
            return
        if ok not in owner_keys_seen:
            owner_keys_seen.add(ok)
            owners.append(owner)
        links.append({"property_key": property_key, "owner_key": ok, "source_step": step_name})
        # Risk: owner without CF
        cf = (owner.get("Codice fiscale") or owner.get("Codice Fiscale") or "").strip()
        if not cf:
            risk_flags.append({"type": "missing_cf", "owner": ok, "source_step": step_name})

    for step in step_results:
        if step["status"] != "completed" or not step.get("data"):
            continue
        data = step["data"]
        step_name = step["step"]

        # Collect immobili
        for imm in data.get("immobili", []):
            pk = f"{imm.get('Foglio', '')}:{imm.get('Particella', '')}:{imm.get('Sub', '')}"
            if pk not in prop_keys_seen:
                prop_keys_seen.add(pk)
                properties.append(imm)

        # Collect intestati
        for owner in data.get("intestati", []):
            _collect_owner(owner, step_name)

        # Drill-down results
        for drill in data.get("drill_results", []):
            prop = drill.get("property", {})
            pk = f"{prop.get('foglio', '')}:{prop.get('particella', '')}:{prop.get('subalterno', '')}"
            if pk not in prop_keys_seen:
                prop_keys_seen.add(pk)
                properties.append(prop)
            for owner in drill.get("intestati", []):
                _collect_owner(owner, f"drill:{pk}", property_key=pk)

        # Cross-property intestati (owner portfolios)
        for portfolio in data.get("owner_portfolios", []):
            cf = portfolio.get("codice_fiscale", "")
            for imm in portfolio.get("immobili", []):
                pk = f"{imm.get('Foglio', '')}:{imm.get('Particella', '')}:{imm.get('Sub', '')}"
                if pk not in prop_keys_seen:
                    prop_keys_seen.add(pk)
                    properties.append(imm)
                links.append({"property_key": pk, "owner_key": cf, "source_step": f"cross_portfolio:{cf}"})

        # Addresses
        for addr in data.get("addresses", []):
            addresses.append(addr)

    # Risk: properties with multiple owners
    owner_count_by_prop: dict[str, int] = {}
    for link in links:
        pk = link.get("property_key")
        if pk:
            owner_count_by_prop[pk] = owner_count_by_prop.get(pk, 0) + 1
    for pk, count in owner_count_by_prop.items():
        if count > 1:
            risk_flags.append({"type": "multiple_owners", "property_key": pk, "count": count})

    # Collect timeline, risk_score, and multi-hop outputs
    timeline: list[dict] = []
    risk_scores: dict = {}
    ranked_properties: list[dict] = []
    for step in step_results:
        if step["status"] != "completed" or not step.get("data"):
            continue
        data = step["data"]
        step_name = step["step"]

        if step_name == "timeline_build":
            timeline = data.get("timeline", [])
        if step_name == "risk_score":
            risk_scores = data
            risk_flags.extend(data.get("risk_flags", []))
        if step_name == "property_rank":
            ranked_properties = data.get("ranked_properties", [])

        # owner_expand discovered properties
        for prop in data.get("discovered_properties", []):
            pk = f"{prop.get('foglio', '')}:{prop.get('particella', '')}:{prop.get('subalterno', '')}"
            if pk not in prop_keys_seen:
                prop_keys_seen.add(pk)
                properties.append(prop)

        # portfolio_drill_intestati results
        for pi in data.get("portfolio_intestati", []):
            prop = pi.get("property", {})
            pk = f"{prop.get('foglio', '')}:{prop.get('particella', '')}:{prop.get('subalterno', '')}"
            if pk not in prop_keys_seen:
                prop_keys_seen.add(pk)
                properties.append(prop)
            for owner in pi.get("intestati", []):
                _collect_owner(owner, f"portfolio_drill:{pk}", property_key=pk)

        # portfolio_history results
        for hr in data.get("history_results", []):
            prop = hr.get("property", {})
            pk = f"{prop.get('foglio', '')}:{prop.get('particella', '')}:{prop.get('subalterno', '')}"
            for sub_step in ("ispezioni", "ispezioni_cart", "originali"):
                sub_data = hr.get(sub_step)
                if isinstance(sub_data, dict) and sub_data.get("risultati"):
                    for row in sub_data["risultati"]:
                        links.append({"property_key": pk, "owner_key": None,
                                      "source_step": f"history:{sub_step}:{pk}"})

        # portfolio_ipotecaria results
        for pr in data.get("paid_results", []):
            if pr.get("status") == "completed" and pr.get("result"):
                prop = pr.get("property", {})
                pk = f"{prop.get('foglio', '')}:{prop.get('particella', '')}:{prop.get('subalterno', '')}"
                res_data = pr["result"]
                if isinstance(res_data, dict) and res_data.get("risultati"):
                    risk_flags.append({
                        "type": "paid_findings", "severity": "high",
                        "property_key": pk, "count": len(res_data["risultati"]),
                        "cost": res_data.get("cost"),
                    })

    return {
        "properties": properties,
        "owners": owners,
        "links": links,
        "addresses": addresses,
        "risk_flags": risk_flags,
        "timeline": timeline,
        "risk_scores": risk_scores,
        "ranked_properties": ranked_properties,
    }


# ---------------------------------------------------------------------------
# DB persistence helpers
# ---------------------------------------------------------------------------


async def _save_workflow_run(workflow_id: str, preset: str, input_data: dict, status: str = "running"):
    """Persist a workflow run to the database."""
    from .database import _get_session_factory

    async with _get_session_factory()() as session:
        from sqlalchemy import text
        await session.execute(text(
            "INSERT OR REPLACE INTO workflow_runs (workflow_id, preset, status, input_json, created_at, updated_at) "
            "VALUES (:wid, :preset, :status, :input_json, :now, :now)"
        ), {"wid": workflow_id, "preset": preset, "status": status,
            "input_json": json.dumps(input_data, default=str), "now": datetime.now().isoformat()})
        await session.commit()


async def _save_workflow_step(workflow_id: str, step_key: str, status: str,
                              result_json: Optional[dict] = None, error: Optional[str] = None):
    """Persist a workflow step result."""
    from .database import _get_session_factory

    now = datetime.now().isoformat()
    async with _get_session_factory()() as session:
        from sqlalchemy import text
        await session.execute(text(
            "INSERT OR REPLACE INTO workflow_steps "
            "(workflow_id, step_key, status, result_json, error, started_at, finished_at) "
            "VALUES (:wid, :sk, :status, :rj, :err, :now, :now)"
        ), {"wid": workflow_id, "sk": step_key, "status": status,
            "rj": json.dumps(result_json, default=str) if result_json else None,
            "err": error, "now": now})
        await session.commit()


async def _load_completed_steps(workflow_id: str) -> dict[str, dict]:
    """Load completed steps for a workflow (for resume)."""
    from .database import _get_session_factory

    async with _get_session_factory()() as session:
        from sqlalchemy import text
        result = await session.execute(text(
            "SELECT step_key, status, result_json FROM workflow_steps "
            "WHERE workflow_id = :wid AND status = 'completed'"
        ), {"wid": workflow_id})
        rows = result.fetchall()

    completed: dict[str, dict] = {}
    for row in rows:
        try:
            data = json.loads(row[2]) if row[2] else None
        except (json.JSONDecodeError, TypeError):
            data = None
        completed[row[0]] = {"status": row[1], "data": data}
    return completed


async def _finish_workflow_run(workflow_id: str, status: str, output_json: Optional[dict] = None):
    """Mark a workflow run as completed/failed."""
    from .database import _get_session_factory

    async with _get_session_factory()() as session:
        from sqlalchemy import text
        await session.execute(text(
            "UPDATE workflow_runs SET status = :status, output_json = :oj, updated_at = :now "
            "WHERE workflow_id = :wid"
        ), {"wid": workflow_id, "status": status,
            "oj": json.dumps(output_json, default=str) if output_json else None,
            "now": datetime.now().isoformat()})
        await session.commit()
        from .database import is_db_writable
        if is_db_writable():
            await session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))



# ---------------------------------------------------------------------------
# Step executors
# ---------------------------------------------------------------------------


async def _persist_step_response(step_name: str, tipo_catasto: str, params: dict, result: dict) -> None:
    """Persist a workflow step's browser result to the response tables and outputs/."""
    request_id = f"wf_{step_name}_{params.get('provincia', '')}_{params.get('foglio', '')}_{params.get('particella', '')}_{uuid4().hex[:8]}"
    try:
        await save_request(
            request_id=request_id,
            request_type=f"workflow_{step_name}",
            tipo_catasto=tipo_catasto,
            provincia=params.get("provincia", ""),
            comune=params.get("comune", ""),
            foglio=params.get("foglio", ""),
            particella=params.get("particella", ""),
            sezione=params.get("sezione"),
            subalterno=params.get("subalterno"),
        )
        await save_response(
            request_id=request_id,
            success=True,
            tipo_catasto=tipo_catasto,
            data=result,
        )
    except Exception as e:
        logger.warning("Failed to persist step response '%s': %s", step_name, e)


async def _exec_search(page, params: dict, step_results: list[dict]) -> dict:
    """Run property search (one or both catasto types)."""
    tipos = [params["tipo_catasto"]] if params.get("tipo_catasto") and params["tipo_catasto"] != "E" else ["T", "F"]
    all_immobili = []
    for tc in tipos:
        result = await run_visura(
            page, params["provincia"], params["comune"], params.get("sezione"),
            params["foglio"], params["particella"], tc,
            extract_intestati=False, subalterno=params.get("subalterno"),
            sezione_urbana=params.get("sezione_urbana"),
        )
        if isinstance(result, dict):
            for imm in result.get("immobili", []):
                imm["_tipo_catasto"] = tc
            all_immobili.extend(result.get("immobili", []))
            await _persist_step_response("search", tc, params, result)
    return {"immobili": all_immobili, "total": len(all_immobili)}


async def _exec_intestati(page, params: dict, step_results: list[dict]) -> dict:
    """Run intestati lookup, using sub-units from previous search if available."""
    prev_search = next((s for s in step_results if s["step"] == "search" and s["status"] == "completed"), None)
    all_intestati = []

    if prev_search and prev_search.get("data"):
        immobili = prev_search["data"].get("immobili", [])
        targets = []
        t_found = any(i.get("_tipo_catasto") == "T" for i in immobili)
        f_subs = {i.get("Sub", "").strip() for i in immobili if i.get("_tipo_catasto") == "F" and i.get("Sub", "").strip()}

        if t_found:
            targets.append(("T", None))
        if params.get("subalterno"):
            targets.append(("F", params["subalterno"]))
        elif f_subs:
            targets.extend(("F", sub) for sub in sorted(f_subs))

        for tc, sub in targets:
            try:
                if tc == "F" and sub:
                    res = await run_visura_immobile(
                        page, params["provincia"], params["comune"], params.get("sezione"),
                        params["foglio"], params["particella"], sub,
                    )
                else:
                    res = await run_visura(
                        page, params["provincia"], params["comune"], params.get("sezione"),
                        params["foglio"], params["particella"], tc,
                        extract_intestati=True, sezione_urbana=params.get("sezione_urbana"),
                    )
                if isinstance(res, dict):
                    await _persist_step_response("intestati", tc, params, res)
                intestati = res.get("intestati", []) if isinstance(res, dict) else []
                for intest in intestati:
                    intest["_tipo_catasto"] = tc
                    intest["_subalterno"] = sub
                all_intestati.extend(intestati)
            except Exception as e:
                logger.warning("Intestati %s/%s failed: %s", tc, sub, e)
    else:
        tc = params.get("tipo_catasto") or "T"
        res = await run_visura(
            page, params["provincia"], params["comune"], params.get("sezione"),
            params["foglio"], params["particella"], tc,
            extract_intestati=True, sezione_urbana=params.get("sezione_urbana"),
        )
        if isinstance(res, dict):
            await _persist_step_response("intestati", tc, params, res)
        all_intestati = res.get("intestati", []) if isinstance(res, dict) else []

    return {"intestati": all_intestati, "total": len(all_intestati)}


async def _exec_soggetto(page, params: dict, step_results: list[dict]) -> dict:
    """Run national subject search."""
    tc = params.get("tipo_catasto", "E")
    result = await run_visura_soggetto(
        page, codice_fiscale=params["codice_fiscale"],
        tipo_catasto=tc, provincia=params.get("provincia"),
    )
    if isinstance(result, dict):
        await _persist_step_response("soggetto", tc, params, result)
    return result


async def _exec_azienda(page, params: dict, step_results: list[dict]) -> dict:
    """Run company search."""
    tc = params.get("tipo_catasto", "E")
    result = await run_visura_persona_giuridica(
        page, identificativo=params["identificativo"],
        tipo_catasto=tc, provincia=params.get("provincia"),
    )
    if isinstance(result, dict):
        await _persist_step_response("azienda", tc, params, result)
    return result


async def _exec_drill_intestati(page, params: dict, step_results: list[dict]) -> dict:
    """Auto drill-down: extract properties from soggetto/azienda, then get intestati for each."""
    # Collect discovered properties from previous steps
    discovered: list[dict] = []
    for s in step_results:
        if s["status"] != "completed" or not s.get("data"):
            continue
        if s["step"] in ("soggetto", "azienda"):
            for imm in s["data"].get("immobili", []):
                prop = _normalize_property(imm)
                if prop:
                    discovered.append(prop)

    discovered = _deduplicate_properties(discovered)
    if not discovered:
        return {"drill_results": [], "total_properties": 0, "truncated": False}

    all_drill = []
    limit = params.get("max_fanout", MAX_DRILL_DOWN)
    for prop in discovered[:limit]:
        tc = prop.get("tipo_catasto") or ("F" if prop.get("subalterno") else "T")
        try:
            if tc == "F" and prop.get("subalterno"):
                res = await run_visura_immobile(
                    page, prop["provincia"], prop["comune"], prop.get("sezione"),
                    prop["foglio"], prop["particella"], prop["subalterno"],
                )
            else:
                res = await run_visura(
                    page, prop["provincia"], prop["comune"], prop.get("sezione"),
                    prop["foglio"], prop["particella"], tc, extract_intestati=True,
                )
            if isinstance(res, dict):
                await _persist_step_response("drill_intestati", tc, prop, res)
            intestati = res.get("intestati", []) if isinstance(res, dict) else []
            all_drill.append({"property": prop, "intestati": intestati, "status": "completed"})
        except Exception as e:
            logger.warning("Drill-down failed for %s: %s", prop, e)
            all_drill.append({"property": prop, "intestati": [], "status": "error", "error": str(e)})

    return {
        "drill_results": all_drill,
        "total_properties": len(discovered),
        "truncated": len(discovered) > limit,
    }


async def _exec_elenco(page, params: dict, step_results: list[dict]) -> dict:
    tc = params.get("tipo_catasto", "T")
    result = await run_elenco_immobili(
        page, params["provincia"], params["comune"],
        tipo_catasto=tc, foglio=params.get("foglio"), sezione=params.get("sezione"),
    )
    if isinstance(result, dict):
        await _persist_step_response("elenco", tc, params, result)
    return result


async def _exec_mappa(page, params: dict, step_results: list[dict]) -> dict:
    return await run_ricerca_mappa(
        page, params["provincia"], comune=params["comune"],
        tipo_catasto=params.get("tipo_catasto", "T"), foglio=params.get("foglio"),
    )


async def _exec_fiduciali(page, params: dict, step_results: list[dict]) -> dict:
    return await run_punti_fiduciali(
        page, params["provincia"], comune=params["comune"],
        tipo_catasto=params.get("tipo_catasto", "T"), foglio=params.get("foglio"),
    )


async def _exec_originali(page, params: dict, step_results: list[dict]) -> dict:
    return await run_originali_impianto(
        page, params["provincia"], comune=params["comune"],
        tipo_catasto=params.get("tipo_catasto", "T"), foglio=params.get("foglio"),
    )


async def _exec_ispezioni(page, params: dict, step_results: list[dict]) -> dict:
    return await run_ispezioni(
        page, params["provincia"], comune=params["comune"],
        tipo_catasto=params.get("tipo_catasto", "T"),
        foglio=params.get("foglio"), particella=params.get("particella"),
    )


async def _exec_ispezioni_cart(page, params: dict, step_results: list[dict]) -> dict:
    return await run_ispezioni_cartacee(
        page, params["provincia"], comune=params["comune"],
        tipo_catasto=params.get("tipo_catasto", "T"),
        foglio=params.get("foglio"), particella=params.get("particella"),
    )


async def _exec_indirizzo_search(page, params: dict, step_results: list[dict]) -> dict:
    tc = params.get("tipo_catasto", "T")
    result = await run_ricerca_indirizzo(
        page, params["provincia"], comune=params["comune"],
        tipo_catasto=tc, indirizzo=params["indirizzo"],
    )
    if isinstance(result, dict):
        await _persist_step_response("indirizzo", tc, params, result)
    # Extract foglio/particella from results for follow-up steps
    if isinstance(result, dict) and not params.get("foglio"):
        immobili = result.get("risultati", result.get("immobili", []))
        if immobili and isinstance(immobili, list) and len(immobili) > 0:
            first = immobili[0]
            fog = first.get("Foglio", "").strip()
            par = first.get("Particella", "").strip()
            if fog:
                params["foglio"] = fog
            if par:
                params["particella"] = par
    return result


async def _exec_elaborato_planimetrico(page, params: dict, step_results: list[dict]) -> dict:
    """Retrieve planimetric floor plan documents (ELPL)."""
    return await run_elaborato_planimetrico(
        page, params["provincia"], params["comune"],
        tipo_catasto=params.get("tipo_catasto", "F"),
        foglio=params.get("foglio"),
    )


async def _exec_export_mappa(page, params: dict, step_results: list[dict]) -> dict:
    """Export full cadastral map data (EXPM)."""
    return await run_export_mappa(
        page, params["provincia"], params["comune"],
        foglio=params.get("foglio", ""),
        tipo_catasto=params.get("tipo_catasto", "T"),
        sezione=params.get("sezione"),
    )


async def _exec_nota(page, params: dict, step_results: list[dict]) -> dict:
    """Search by annotation/note reference."""
    numero_nota = params.get("numero_nota")
    if not numero_nota:
        return {"risultati": [], "total_results": 0, "skipped": "no numero_nota provided"}
    return await run_ricerca_nota(
        page, params["provincia"],
        numero_nota=numero_nota,
        anno_nota=params.get("anno_nota"),
        tipo_catasto=params.get("tipo_catasto", "T"),
    )


async def _exec_cross_property_intestati(page, params: dict, step_results: list[dict]) -> dict:
    """For each unique owner CF found in intestati, run soggetto to discover their full portfolio.

    This turns a single-property workflow into a portfolio discovery.
    Fan-out limited by MAX_DRILL_DOWN.
    """
    # Collect unique codice_fiscale values from all intestati steps
    owner_cfs: set[str] = set()
    for s in step_results:
        if s["status"] != "completed" or not s.get("data"):
            continue
        data = s["data"]
        for owner in data.get("intestati", []):
            cf = (owner.get("Codice fiscale") or owner.get("Codice Fiscale") or "").strip()
            if cf and len(cf) >= 11:
                owner_cfs.add(cf.upper())
        # Also from drill-down results
        for drill in data.get("drill_results", []):
            for owner in drill.get("intestati", []):
                cf = (owner.get("Codice fiscale") or owner.get("Codice Fiscale") or "").strip()
                if cf and len(cf) >= 11:
                    owner_cfs.add(cf.upper())

    if not owner_cfs:
        return {"owner_portfolios": [], "total_owners": 0, "skipped": "no owner CFs found"}

    limit = params.get("max_fanout", MAX_DRILL_DOWN)
    portfolios = []
    for cf in sorted(owner_cfs)[:limit]:
        try:
            # Determine if CF is person (16 chars) or company (11 digits)
            if len(cf) == 11 and cf.isdigit():
                result = await run_visura_persona_giuridica(
                    page, identificativo=cf,
                    tipo_catasto=params.get("tipo_catasto", "E"),
                    provincia=params.get("provincia"),
                )
            else:
                result = await run_visura_soggetto(
                    page, codice_fiscale=cf,
                    tipo_catasto=params.get("tipo_catasto", "E"),
                    provincia=params.get("provincia"),
                )
            immobili = result.get("immobili", []) if isinstance(result, dict) else []
            portfolios.append({
                "codice_fiscale": cf, "status": "completed",
                "immobili": immobili, "total_properties": len(immobili),
            })
        except Exception as e:
            logger.warning("Cross-property lookup failed for CF=%s: %s", cf, e)
            portfolios.append({"codice_fiscale": cf, "status": "error", "error": str(e), "immobili": []})

    return {
        "owner_portfolios": portfolios,
        "total_owners": len(owner_cfs),
        "truncated": len(owner_cfs) > limit,
    }


async def _exec_indirizzo_reverse(page, params: dict, step_results: list[dict]) -> dict:
    """For discovered properties, look up their street addresses.

    Useful after patrimonio/aziendale to get human-readable locations.
    """
    # Collect properties from previous steps
    properties: list[dict] = []
    for s in step_results:
        if s["status"] != "completed" or not s.get("data"):
            continue
        data = s["data"]
        for imm in data.get("immobili", []):
            prop = _normalize_property(imm)
            if prop:
                properties.append(prop)
        for drill in data.get("drill_results", []):
            prop = drill.get("property")
            if prop and prop.get("provincia"):
                properties.append(prop)
        for portfolio in data.get("owner_portfolios", []):
            for imm in portfolio.get("immobili", []):
                prop = _normalize_property(imm)
                if prop:
                    properties.append(prop)

    properties = _deduplicate_properties(properties)
    if not properties:
        return {"addresses": [], "total": 0, "skipped": "no properties to look up"}

    # Group by (provincia, comune) to minimize navigation
    limit = params.get("max_fanout", MAX_DRILL_DOWN)
    addresses = []
    for prop in properties[:limit]:
        if not prop.get("provincia") or not prop.get("comune"):
            continue
        try:
            result = await run_ricerca_indirizzo(
                page, prop["provincia"], comune=prop["comune"],
                tipo_catasto=prop.get("tipo_catasto", "T"),
                indirizzo="",  # empty = list all for the parcel
            )
            # Try to match our parcel in results
            found_addresses = []
            risultati = result.get("risultati", []) if isinstance(result, dict) else []
            for r in risultati:
                if r.get("Foglio", "").strip() == prop.get("foglio") and r.get("Particella", "").strip() == prop.get("particella"):
                    found_addresses.append(r.get("Indirizzo", "").strip())
            addresses.append({
                "property": prop,
                "addresses": found_addresses or ["(not found)"],
            })
        except Exception as e:
            logger.warning("Address reverse lookup failed for %s: %s", prop, e)
            addresses.append({"property": prop, "addresses": [], "error": str(e)})

    return {"addresses": addresses, "total": len(addresses), "truncated": len(properties) > limit}


async def _exec_ispezione_ipotecaria(page, params: dict, step_results: list[dict]) -> dict:
    """Run paid Ispezione Ipotecaria (requires explicit opt-in via include_paid_steps + auto_confirm)."""
    return await run_ispezione_ipotecaria(
        page, provincia=params["provincia"], comune=params.get("comune"),
        tipo_ricerca="immobile",
        foglio=params.get("foglio"), particella=params.get("particella"),
        tipo_catasto=params.get("tipo_catasto", "T"),
        auto_confirm=params.get("auto_confirm", False),
    )


# ---------------------------------------------------------------------------
# Analytical steps (post-processing, no browser needed)
# ---------------------------------------------------------------------------


async def _exec_owner_expand(page, params: dict, step_results: list[dict]) -> dict:
    """For each unique owner found in intestati/drill results, run soggetto or azienda
    to discover their full property portfolio. Merges into normalized owner entities.

    Unlike cross_property_intestati (which expands from one direction), this step:
    - Collects ALL unique owner CFs from all completed intestati-producing steps
    - Classifies each CF as person (16 chars) vs company (11 digits)
    - Runs the appropriate search
    - Deduplicates the merged property set
    - Produces owner_entities with portfolio counts
    """
    owner_cfs: dict[str, str] = {}  # cf → "person" or "company"

    for s in step_results:
        if s["status"] != "completed" or not s.get("data"):
            continue
        data = s["data"]
        for owner in data.get("intestati", []):
            cf = (owner.get("Codice fiscale") or owner.get("Codice Fiscale") or "").strip().upper()
            if cf and len(cf) >= 11 and cf not in owner_cfs:
                owner_cfs[cf] = "company" if (len(cf) == 11 and cf.isdigit()) else "person"
        for drill in data.get("drill_results", []):
            for owner in drill.get("intestati", []):
                cf = (owner.get("Codice fiscale") or owner.get("Codice Fiscale") or "").strip().upper()
                if cf and len(cf) >= 11 and cf not in owner_cfs:
                    owner_cfs[cf] = "company" if (len(cf) == 11 and cf.isdigit()) else "person"

    if not owner_cfs:
        return {"owner_entities": [], "total_owners": 0, "skipped": "no owner CFs found"}

    limit = params.get("max_fanout", MAX_DRILL_DOWN)
    entities = []
    all_discovered_properties: list[dict] = []

    for cf in sorted(owner_cfs)[:limit]:
        entity_type = owner_cfs[cf]
        try:
            if entity_type == "company":
                result = await run_visura_persona_giuridica(
                    page, identificativo=cf,
                    tipo_catasto=params.get("tipo_catasto", "E"),
                    provincia=params.get("provincia"),
                )
            else:
                result = await run_visura_soggetto(
                    page, codice_fiscale=cf,
                    tipo_catasto=params.get("tipo_catasto", "E"),
                    provincia=params.get("provincia"),
                )
            immobili = result.get("immobili", []) if isinstance(result, dict) else []
            props = [_normalize_property(imm) for imm in immobili]
            props = [p for p in props if p is not None]
            all_discovered_properties.extend(props)
            entities.append({
                "codice_fiscale": cf, "type": entity_type, "status": "completed",
                "properties_count": len(props),
            })
        except Exception as e:
            logger.warning("Owner expand failed for CF=%s: %s", cf, e)
            entities.append({"codice_fiscale": cf, "type": entity_type, "status": "error", "error": str(e)})

    all_discovered_properties = _deduplicate_properties(all_discovered_properties)

    return {
        "owner_entities": entities,
        "total_owners": len(owner_cfs),
        "discovered_properties": all_discovered_properties,
        "total_discovered": len(all_discovered_properties),
        "truncated": len(owner_cfs) > limit,
    }


async def _exec_timeline_build(_page, params: dict, step_results: list[dict]) -> dict:
    """Build a chronological timeline from nota, ispezioni, originali, and ispezioni_cart results.

    This is a post-processing step that does not use the browser. It extracts
    date-like fields from prior step results and orders them into an event timeline.
    """
    import re

    events: list[dict] = []
    _date_pattern = re.compile(r'(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})')

    for s in step_results:
        if s["status"] != "completed" or not s.get("data"):
            continue
        data = s["data"]
        step_name = s["step"]

        # Extract from risultati (generic table rows)
        for row in data.get("risultati", []):
            event = {"source_step": step_name, "type": step_name}
            # Try to extract a date from any field
            date_found = None
            for key, value in row.items():
                if not isinstance(value, str):
                    continue
                match = _date_pattern.search(value)
                if match and not date_found:
                    date_found = match.group(1)
                    event["date"] = date_found
                    event["date_field"] = key
            event["details"] = {k: v for k, v in row.items() if isinstance(v, str) and v.strip()}
            events.append(event)

        # Extract from intestati (ownership records with dates)
        for owner in data.get("intestati", []):
            event = {"source_step": step_name, "type": "ownership"}
            for key, value in owner.items():
                if not isinstance(value, str):
                    continue
                match = _date_pattern.search(value)
                if match:
                    event["date"] = match.group(1)
                    event["date_field"] = key
                    break
            event["details"] = {k: v for k, v in owner.items() if isinstance(v, str) and v.strip()}
            if event.get("date") or event.get("details"):
                events.append(event)

    # Sort by date (best effort — dates may be in various formats)
    def _parse_date_key(e):
        d = e.get("date", "")
        parts = re.split(r'[/\-\.]', d)
        if len(parts) == 3:
            try:
                day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
                if year < 100:
                    year += 2000 if year < 50 else 1900
                return (year, month, day)
            except (ValueError, IndexError):
                pass
        return (9999, 0, 0)  # undated events sort last

    events.sort(key=_parse_date_key)

    # Detect gaps: if consecutive ownership events span > 1 year without an intermediate event
    gaps = []
    dated_events = [e for e in events if e.get("date")]
    for i in range(1, len(dated_events)):
        prev_key = _parse_date_key(dated_events[i - 1])
        curr_key = _parse_date_key(dated_events[i])
        if prev_key[0] < 9999 and curr_key[0] < 9999:
            year_gap = curr_key[0] - prev_key[0]
            if year_gap > 1:
                gaps.append({
                    "from_date": dated_events[i - 1].get("date"),
                    "to_date": dated_events[i].get("date"),
                    "gap_years": year_gap,
                    "from_step": dated_events[i - 1].get("source_step"),
                    "to_step": dated_events[i].get("source_step"),
                })

    return {
        "timeline": events,
        "total_events": len(events),
        "dated_events": len(dated_events),
        "undated_events": len(events) - len(dated_events),
        "gaps": gaps,
        "total_gaps": len(gaps),
    }


async def _exec_risk_score(_page, params: dict, step_results: list[dict]) -> dict:
    """Score and flag risks from all prior step results.

    This is a post-processing step that does not use the browser. It examines
    the aggregate of all step data and produces risk flags with severity levels.

    Risk categories:
    - missing_cf: owner without codice fiscale
    - multiple_owners: property with >1 owner
    - ownership_mismatch: owner found in intestati but not in soggetto/azienda
    - unresolved_address: indirizzo_search returned ambiguous results
    - timeline_gap: gaps in ownership/act history
    - high_fanout: unusually many properties or owners (concentration risk)
    - missing_subalterno: fabbricati without sub-unit specified
    - paid_findings: ispezione ipotecaria returned results
    """
    flags: list[dict] = []

    # Collect all owners and properties from all steps
    all_owners: dict[str, dict] = {}  # cf → owner data
    all_properties: list[dict] = []
    owner_to_props: dict[str, list[str]] = {}  # cf → [prop_keys]
    prop_to_owners: dict[str, list[str]] = {}  # prop_key → [cfs]
    soggetto_cfs: set[str] = set()
    azienda_ids: set[str] = set()

    for s in step_results:
        if s["status"] != "completed" or not s.get("data"):
            continue
        data = s["data"]
        step_name = s["step"]

        # Track soggetto/azienda source CFs
        if step_name == "soggetto" and params.get("codice_fiscale"):
            soggetto_cfs.add(params["codice_fiscale"].upper())
        if step_name == "azienda" and params.get("identificativo"):
            azienda_ids.add(params["identificativo"].upper())

        for owner in data.get("intestati", []):
            cf = (owner.get("Codice fiscale") or owner.get("Codice Fiscale") or "").strip().upper()
            nome = (owner.get("Nominativo o denominazione") or owner.get("Nominativo") or "").strip()
            key = cf or nome
            if key and key not in all_owners:
                all_owners[key] = owner

            if not cf and nome:
                flags.append({"type": "missing_cf", "severity": "medium", "owner": nome, "source_step": step_name})

        for imm in data.get("immobili", []):
            prop = _normalize_property(imm)
            if prop:
                all_properties.append(prop)

        # Drill-down
        for drill in data.get("drill_results", []):
            prop = drill.get("property", {})
            pk = f"{prop.get('foglio', '')}:{prop.get('particella', '')}:{prop.get('subalterno', '')}"
            for owner in drill.get("intestati", []):
                cf = (owner.get("Codice fiscale") or owner.get("Codice Fiscale") or "").strip().upper()
                if cf:
                    owner_to_props.setdefault(cf, []).append(pk)
                    prop_to_owners.setdefault(pk, []).append(cf)

        # Owner expand
        for entity in data.get("owner_entities", []):
            if entity.get("status") == "error":
                flags.append({"type": "owner_expand_failed", "severity": "low",
                              "codice_fiscale": entity.get("codice_fiscale"), "error": entity.get("error")})

        # Timeline gaps
        for gap in data.get("gaps", []):
            flags.append({"type": "timeline_gap", "severity": "medium",
                          "from_date": gap.get("from_date"), "to_date": gap.get("to_date"),
                          "gap_years": gap.get("gap_years")})

        # Paid findings
        if step_name == "ispezione_ipotecaria" and data.get("risultati"):
            flags.append({"type": "paid_findings", "severity": "high",
                          "count": len(data["risultati"]),
                          "cost": data.get("cost")})

    # Multiple owners per property
    for pk, owners in prop_to_owners.items():
        unique = set(owners)
        if len(unique) > 1:
            flags.append({"type": "multiple_owners", "severity": "low",
                          "property_key": pk, "owner_count": len(unique)})

    # Ownership mismatch: owners in intestati not found in soggetto/azienda source
    if soggetto_cfs or azienda_ids:
        source_ids = soggetto_cfs | azienda_ids
        for key, owner in all_owners.items():
            if key and key not in source_ids and len(key) >= 11:
                flags.append({"type": "ownership_mismatch", "severity": "medium",
                              "owner_key": key, "note": "Found in intestati but not in soggetto/azienda source"})

    # Missing subalterno for fabbricati
    all_properties = _deduplicate_properties(all_properties)
    for prop in all_properties:
        tc = prop.get("tipo_catasto", "")
        if tc == "F" and not prop.get("subalterno"):
            flags.append({"type": "missing_subalterno", "severity": "low",
                          "property": f"{prop.get('comune', '')} F.{prop.get('foglio', '')} P.{prop.get('particella', '')}"})

    # High fanout / concentration
    if len(all_properties) > 50:
        flags.append({"type": "high_property_count", "severity": "info", "count": len(all_properties)})
    if len(all_owners) > 20:
        flags.append({"type": "high_owner_count", "severity": "info", "count": len(all_owners)})

    # Geographic clustering
    by_comune: dict[str, int] = {}
    for prop in all_properties:
        loc = f"{prop.get('provincia', '')}/{prop.get('comune', '')}"
        by_comune[loc] = by_comune.get(loc, 0) + 1
    concentration = sorted(by_comune.items(), key=lambda x: -x[1])

    # Severity summary
    severity_counts = {"high": 0, "medium": 0, "low": 0, "info": 0}
    for f in flags:
        sev = f.get("severity", "info")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    return {
        "risk_flags": flags,
        "total_flags": len(flags),
        "severity_counts": severity_counts,
        "total_properties": len(all_properties),
        "total_owners": len(all_owners),
        "geographic_concentration": concentration[:10],
    }


# ---------------------------------------------------------------------------
# Multi-hop steps (full depth — bounded graph expansion)
# ---------------------------------------------------------------------------


def _collect_all_properties(step_results: list[dict]) -> list[dict]:
    """Gather all discovered properties from all completed steps."""
    props: list[dict] = []
    for s in step_results:
        if s["status"] != "completed" or not s.get("data"):
            continue
        data = s["data"]
        for imm in data.get("immobili", []):
            p = _normalize_property(imm)
            if p:
                props.append(p)
        for drill in data.get("drill_results", []):
            p = drill.get("property")
            if p and p.get("provincia"):
                props.append(p)
        for dp in data.get("discovered_properties", []):
            if dp.get("provincia"):
                props.append(dp)
        for entity in data.get("owner_entities", []):
            pass  # entities don't carry properties inline
        for portfolio in data.get("owner_portfolios", []):
            for imm in portfolio.get("immobili", []):
                p = _normalize_property(imm)
                if p:
                    props.append(p)
    return _deduplicate_properties(props)


def _collect_all_owner_cfs(step_results: list[dict]) -> set[str]:
    """Gather all unique owner CFs from intestati/drill results."""
    cfs: set[str] = set()
    for s in step_results:
        if s["status"] != "completed" or not s.get("data"):
            continue
        data = s["data"]
        for owner in data.get("intestati", []):
            cf = (owner.get("Codice fiscale") or owner.get("Codice Fiscale") or "").strip().upper()
            if cf and len(cf) >= 11:
                cfs.add(cf)
        for drill in data.get("drill_results", []):
            for owner in drill.get("intestati", []):
                cf = (owner.get("Codice fiscale") or owner.get("Codice Fiscale") or "").strip().upper()
                if cf and len(cf) >= 11:
                    cfs.add(cf)
        for pi in data.get("portfolio_intestati", []):
            for owner in pi.get("intestati", []):
                cf = (owner.get("Codice fiscale") or owner.get("Codice Fiscale") or "").strip().upper()
                if cf and len(cf) >= 11:
                    cfs.add(cf)
    return cfs


async def _exec_property_rank(_page, params: dict, step_results: list[dict]) -> dict:
    """Score and rank discovered properties for selective enrichment.

    Scoring criteria:
    - same provincia/comune as seed: +20
    - multiple owners found: +15
    - missing subalterno on fabbricati: +10
    - discovered via multiple paths: +10
    - owner with missing CF: +5
    - otherwise: base score 10
    """
    all_props = _collect_all_properties(step_results)
    seed_prov = (params.get("provincia") or "").upper()
    seed_com = (params.get("comune") or "").upper()

    # Count how many times each property key appears (multi-path discovery)
    discovery_counts: dict[str, int] = {}
    for s in step_results:
        if s["status"] != "completed" or not s.get("data"):
            continue
        data = s["data"]
        for imm in data.get("immobili", []):
            p = _normalize_property(imm)
            if p:
                k = f"{p['provincia']}:{p['comune']}:{p['foglio']}:{p['particella']}:{p.get('subalterno', '')}"
                discovery_counts[k] = discovery_counts.get(k, 0) + 1
        for drill in data.get("drill_results", []):
            prop = drill.get("property", {})
            k = f"{prop.get('provincia', '')}:{prop.get('comune', '')}:{prop.get('foglio', '')}:{prop.get('particella', '')}:{prop.get('subalterno', '')}"
            discovery_counts[k] = discovery_counts.get(k, 0) + 1
        for dp in data.get("discovered_properties", []):
            k = f"{dp.get('provincia', '')}:{dp.get('comune', '')}:{dp.get('foglio', '')}:{dp.get('particella', '')}:{dp.get('subalterno', '')}"
            discovery_counts[k] = discovery_counts.get(k, 0) + 1

    # Collect owner counts per property from drill results
    prop_owner_counts: dict[str, int] = {}
    for s in step_results:
        if s["status"] != "completed" or not s.get("data"):
            continue
        for drill in s["data"].get("drill_results", []):
            prop = drill.get("property", {})
            k = f"{prop.get('provincia', '')}:{prop.get('comune', '')}:{prop.get('foglio', '')}:{prop.get('particella', '')}:{prop.get('subalterno', '')}"
            prop_owner_counts[k] = len(drill.get("intestati", []))

    # Owners with missing CF
    missing_cf_owners: set[str] = set()
    for s in step_results:
        if s["status"] != "completed" or not s.get("data"):
            continue
        for owner in s["data"].get("intestati", []):
            cf = (owner.get("Codice fiscale") or owner.get("Codice Fiscale") or "").strip()
            nome = (owner.get("Nominativo o denominazione") or owner.get("Nominativo") or "").strip()
            if not cf and nome:
                missing_cf_owners.add(nome)

    ranked = []
    for prop in all_props:
        k = f"{prop.get('provincia', '')}:{prop.get('comune', '')}:{prop.get('foglio', '')}:{prop.get('particella', '')}:{prop.get('subalterno', '')}"
        score = 10  # base

        if prop.get("provincia", "").upper() == seed_prov and prop.get("comune", "").upper() == seed_com:
            score += 20
        if prop_owner_counts.get(k, 0) > 1:
            score += 15
        tc = prop.get("tipo_catasto", "")
        if tc == "F" and not prop.get("subalterno"):
            score += 10
        if discovery_counts.get(k, 0) > 1:
            score += 10

        ranked.append({**prop, "_score": score, "_key": k})

    ranked.sort(key=lambda p: -p["_score"])
    min_score = params.get("min_property_score", 30)

    return {
        "ranked_properties": ranked,
        "total": len(ranked),
        "above_threshold": sum(1 for p in ranked if p["_score"] >= min_score),
        "min_score": min_score,
    }


async def _exec_portfolio_drill_intestati(page, params: dict, step_results: list[dict]) -> dict:
    """Run intestati on top-ranked unseen properties from owner expansion.

    Uses property_rank output to select which properties to enrich.
    Tracks per-property persistence keys for resume.
    """
    # Get ranked properties from prior step
    rank_step = next((s for s in step_results if s["step"] == "property_rank" and s["status"] == "completed"), None)
    if not rank_step or not rank_step.get("data"):
        return {"portfolio_intestati": [], "total": 0, "skipped": "no ranked properties"}

    ranked = rank_step["data"].get("ranked_properties", [])
    min_score = params.get("min_property_score", 30)
    limit = params.get("max_properties_per_owner", 20)

    # Filter: above threshold, not the seed property itself
    seed_key = f"{params.get('provincia', '')}:{params.get('comune', '')}:{params.get('foglio', '')}:{params.get('particella', '')}:{params.get('subalterno', '')}"
    candidates = [p for p in ranked if p.get("_score", 0) >= min_score and p.get("_key") != seed_key][:limit]

    if not candidates:
        return {"portfolio_intestati": [], "total": 0, "skipped": "no candidates above threshold"}

    results = []
    for prop in candidates:
        tc = prop.get("tipo_catasto") or ("F" if prop.get("subalterno") else "T")
        try:
            if tc == "F" and prop.get("subalterno"):
                res = await run_visura_immobile(
                    page, prop["provincia"], prop["comune"], prop.get("sezione"),
                    prop["foglio"], prop["particella"], prop["subalterno"],
                )
            else:
                res = await run_visura(
                    page, prop["provincia"], prop["comune"], prop.get("sezione"),
                    prop["foglio"], prop["particella"], tc, extract_intestati=True,
                )
            intestati = res.get("intestati", []) if isinstance(res, dict) else []
            results.append({"property": prop, "intestati": intestati, "status": "completed"})
        except Exception as e:
            logger.warning("Portfolio drill intestati failed for %s: %s", prop.get("_key"), e)
            results.append({"property": prop, "intestati": [], "status": "error", "error": str(e)})

    return {"portfolio_intestati": results, "total": len(results)}


async def _exec_portfolio_history(page, params: dict, step_results: list[dict]) -> dict:
    """Run ispezioni, ispezioni_cart, originali, and nota on top-ranked properties.

    Only processes the top N properties (max_historical_properties).
    Selectively runs each sub-step only when required inputs are present.
    """
    rank_step = next((s for s in step_results if s["step"] == "property_rank" and s["status"] == "completed"), None)
    if not rank_step or not rank_step.get("data"):
        return {"history_results": [], "total": 0, "skipped": "no ranked properties"}

    ranked = rank_step["data"].get("ranked_properties", [])
    min_score = params.get("min_property_score", 30)
    limit = params.get("max_historical_properties", 5)
    candidates = [p for p in ranked if p.get("_score", 0) >= min_score][:limit]

    if not candidates:
        return {"history_results": [], "total": 0, "skipped": "no candidates above threshold"}

    results = []
    for prop in candidates:
        history: dict = {"property": prop, "ispezioni": None, "ispezioni_cart": None, "originali": None, "nota": None}
        tc = prop.get("tipo_catasto") or "T"
        prov, com = prop.get("provincia", ""), prop.get("comune", "")
        fog, par = prop.get("foglio"), prop.get("particella")

        if prov and com:
            try:
                history["ispezioni"] = await run_ispezioni(page, prov, comune=com, tipo_catasto=tc, foglio=fog, particella=par)
            except Exception as e:
                history["ispezioni"] = {"error": str(e)}

            try:
                history["ispezioni_cart"] = await run_ispezioni_cartacee(page, prov, comune=com, tipo_catasto=tc, foglio=fog, particella=par)
            except Exception as e:
                history["ispezioni_cart"] = {"error": str(e)}

            try:
                history["originali"] = await run_originali_impianto(page, prov, comune=com, tipo_catasto=tc, foglio=fog)
            except Exception as e:
                history["originali"] = {"error": str(e)}

        history["status"] = "completed"
        results.append(history)

    return {"history_results": results, "total": len(results)}


async def _exec_portfolio_ipotecaria(page, params: dict, step_results: list[dict]) -> dict:
    """Run paid ispezione ipotecaria on top-risk properties.

    Uses property_rank scores and respects max_paid_steps budget.
    """
    if not params.get("auto_confirm"):
        return {"paid_results": [], "total": 0, "skipped": "auto_confirm not set"}

    rank_step = next((s for s in step_results if s["step"] == "property_rank" and s["status"] == "completed"), None)
    if not rank_step or not rank_step.get("data"):
        return {"paid_results": [], "total": 0, "skipped": "no ranked properties"}

    ranked = rank_step["data"].get("ranked_properties", [])
    max_paid = params.get("max_paid_steps", 3)
    min_score = params.get("min_property_score", 30)
    # Only top-risk: above threshold, sorted by score desc (already sorted)
    candidates = [p for p in ranked if p.get("_score", 0) >= min_score][:max_paid]

    if not candidates:
        return {"paid_results": [], "total": 0, "skipped": "no candidates above threshold"}

    paid_count = params.get("_paid_step_count", 0)
    results = []
    for prop in candidates:
        if paid_count >= max_paid:
            break
        try:
            res = await run_ispezione_ipotecaria(
                page, provincia=prop.get("provincia", ""), comune=prop.get("comune"),
                tipo_ricerca="immobile",
                foglio=prop.get("foglio"), particella=prop.get("particella"),
                tipo_catasto=prop.get("tipo_catasto", "T"),
                auto_confirm=True,
            )
            results.append({"property": prop, "result": res, "status": "completed"})
            paid_count += 1
        except Exception as e:
            logger.warning("Portfolio ipotecaria failed for %s: %s", prop.get("_key"), e)
            results.append({"property": prop, "result": None, "status": "error", "error": str(e)})

    params["_paid_step_count"] = paid_count
    return {"paid_results": results, "total": len(results), "paid_invocations": paid_count}


_STEP_EXECUTORS = {
    "search": _exec_search,
    "intestati": _exec_intestati,
    "soggetto": _exec_soggetto,
    "azienda": _exec_azienda,
    "drill_intestati": _exec_drill_intestati,
    "elenco": _exec_elenco,
    "mappa": _exec_mappa,
    "fiduciali": _exec_fiduciali,
    "originali": _exec_originali,
    "ispezioni": _exec_ispezioni,
    "ispezioni_cart": _exec_ispezioni_cart,
    "indirizzo_search": _exec_indirizzo_search,
    "elaborato_planimetrico": _exec_elaborato_planimetrico,
    "export_mappa": _exec_export_mappa,
    "nota": _exec_nota,
    "cross_property_intestati": _exec_cross_property_intestati,
    "indirizzo_reverse": _exec_indirizzo_reverse,
    "ispezione_ipotecaria": _exec_ispezione_ipotecaria,
    "owner_expand": _exec_owner_expand,
    "timeline_build": _exec_timeline_build,
    "risk_score": _exec_risk_score,
    "property_rank": _exec_property_rank,
    "portfolio_drill_intestati": _exec_portfolio_drill_intestati,
    "portfolio_history": _exec_portfolio_history,
    "portfolio_ipotecaria": _exec_portfolio_ipotecaria,
}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_workflow(
    service: VisuraService,
    wf: WorkflowInput,
    workflow_id: Optional[str] = None,
    resume: bool = False,
) -> dict:
    """Execute a multi-step workflow.

    This is the single shared entry point used by both the API endpoint
    and the CLI.

    Args:
        service: The VisuraService with an authenticated browser session.
        wf: Workflow input parameters.
        workflow_id: Optional ID for persistence/resume. Generated if not given.
        resume: If True, skip steps already completed for this workflow_id.

    Returns:
        {
            "workflow_id": str,
            "preset": str,
            "description": str,
            "steps": [...],
            "aggregate": {"properties": [...], "owners": [...], "links": [...]},
            "summary": {...},
        }
    """
    preset_def = WORKFLOW_PRESETS[wf.preset]
    max_depth = _DEPTH_ORDER.get(wf.depth, 1)

    # Filter steps by depth tier and paid flag
    steps_to_run = []
    for step_name in preset_def["steps"]:
        meta = STEP_METADATA.get(step_name, {"depth": "standard", "paid": False})
        step_depth = _DEPTH_ORDER.get(meta["depth"], 1)
        if step_depth > max_depth:
            continue
        if meta["paid"] and not (wf.include_paid_steps and wf.auto_confirm):
            continue
        steps_to_run.append(step_name)

    # Validate required fields
    field_map = {
        "provincia": wf.provincia, "comune": wf.comune, "foglio": wf.foglio,
        "particella": wf.particella, "codice_fiscale": wf.codice_fiscale,
        "identificativo": wf.identificativo, "indirizzo": wf.indirizzo,
    }
    missing = [f for f in preset_def["requires"] if not field_map.get(f)]
    if missing:
        return {"preset": wf.preset, "error": f"Missing required fields: {', '.join(missing)}", "steps": []}

    if not workflow_id:
        workflow_id = f"wf_{wf.preset}_{uuid4().hex[:12]}"

    # Build mutable params dict that steps can update (e.g. indirizzo_search sets foglio)
    params = {
        "provincia": wf.provincia, "comune": wf.comune,
        "foglio": wf.foglio, "particella": wf.particella,
        "tipo_catasto": wf.tipo_catasto or "T",
        "sezione": wf.sezione, "sezione_urbana": wf.sezione_urbana, "subalterno": wf.subalterno,
        "codice_fiscale": wf.codice_fiscale,
        "identificativo": wf.identificativo,
        "indirizzo": wf.indirizzo,
        "auto_confirm": wf.auto_confirm,
        "max_fanout": wf.max_fanout,
        "max_owners": wf.max_owners,
        "max_properties_per_owner": wf.max_properties_per_owner,
        "max_historical_properties": wf.max_historical_properties,
        "max_paid_steps": wf.max_paid_steps,
        "max_total_steps": wf.max_total_steps,
        # Runtime counters (mutated during execution)
        "_paid_step_count": 0,
        "_total_step_count": 0,
    }

    # Persist workflow run
    try:
        await _save_workflow_run(workflow_id, wf.preset, params)
    except Exception as e:
        logger.warning("Failed to persist workflow run: %s", e)

    # Load completed steps if resuming
    completed_steps: dict[str, dict] = {}
    if resume:
        try:
            completed_steps = await _load_completed_steps(workflow_id)
        except Exception as e:
            logger.warning("Failed to load completed steps for resume: %s", e)

    step_results: list[dict] = []

    async with service.browser_manager._page_lock:
        page = await service.browser_manager._get_authenticated_page()

        for step_name in steps_to_run:
            # Circuit breaker: max total steps
            if params.get("_total_step_count", 0) >= params.get("max_total_steps", 100):
                step_results.append({"step": step_name, "status": "skipped", "data": None, "reason": "max_total_steps reached"})
                logger.warning("Circuit breaker: max_total_steps (%d) reached, skipping '%s'", params["max_total_steps"], step_name)
                continue

            sk = _step_key(step_name)
            executor = _STEP_EXECUTORS.get(step_name)

            if not executor:
                step_results.append({"step": step_name, "status": "skipped", "data": None, "error": f"Unknown step: {step_name}"})
                continue

            # Evaluate 'when' clause — skip if precondition not met
            meta = STEP_METADATA.get(step_name, {})
            when_fn = meta.get("when")
            if when_fn and not when_fn(step_results, params):
                step_results.append({"step": step_name, "status": "skipped", "data": None, "reason": "precondition not met"})
                logger.debug("Skipping step '%s': when-clause returned False", step_name)
                continue

            # Resume: skip completed steps
            if sk in completed_steps:
                cached = completed_steps[sk]
                step_results.append({"step": step_name, "status": "completed", "data": cached.get("data"), "resumed": True})
                logger.info("Resumed step '%s' (workflow %s)", step_name, workflow_id)
                continue

            for attempt in range(2):
                try:
                    data = await executor(page, params, step_results)
                    step_result = {"step": step_name, "status": "completed", "data": data}
                    step_results.append(step_result)
                    params["_total_step_count"] = params.get("_total_step_count", 0) + 1

                    # Persist step
                    try:
                        await _save_workflow_step(workflow_id, sk, "completed", result_json=data)
                    except Exception as e:
                        logger.warning("Failed to persist step '%s': %s", step_name, e)

                    break  # success — no retry needed

                except Exception as e:
                    is_session_expired = "Sessione scaduta" in str(e)
                    if is_session_expired and attempt == 0:
                        logger.warning("Session expired during step '%s', re-authenticating...", step_name)
                        try:
                            page = await service.browser_manager._get_authenticated_page()
                            continue  # retry the step
                        except Exception as reauth_err:
                            logger.error("Re-authentication failed: %s", reauth_err)

                    logger.error("Workflow step '%s' failed: %s", step_name, e)
                    step_result = {"step": step_name, "status": "error", "error": str(e)}
                    step_results.append(step_result)

                    try:
                        await _save_workflow_step(workflow_id, sk, "error", error=str(e))
                    except Exception:
                        pass

    # Build aggregate
    aggregate = _build_aggregate(step_results)

    # Summary
    completed_count = sum(1 for s in step_results if s["status"] == "completed")
    failed_count = sum(1 for s in step_results if s["status"] == "error")
    skipped_count = sum(1 for s in step_results if s["status"] == "skipped")

    output = {
        "workflow_id": workflow_id,
        "preset": wf.preset,
        "description": preset_def["description"],
        "steps": step_results,
        "aggregate": aggregate,
        "summary": {
            "total_steps": len(step_results),
            "completed": completed_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "properties": len(aggregate["properties"]),
            "owners": len(aggregate["owners"]),
            "addresses": len(aggregate.get("addresses", [])),
            "risk_flags": len(aggregate.get("risk_flags", [])),
            "links": len(aggregate.get("links", [])),
            "timeline_events": len(aggregate.get("timeline", [])),
        },
    }

    # Finalize workflow run
    final_status = "completed" if failed_count == 0 else "partial"
    try:
        await _finish_workflow_run(workflow_id, final_status, output)
    except Exception as e:
        logger.warning("Failed to finalize workflow run: %s", e)

    # Export to outputs/ directory
    try:
        from .database import OUTPUTS_DIR
        outputs_dir = Path(OUTPUTS_DIR)
        outputs_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = outputs_dir / f"{wf.preset}_{ts}.json"
        output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str))
        logger.info("Workflow output saved to %s", output_path)
    except Exception as e:
        logger.warning("Failed to export workflow output: %s", e)

    return output


async def run_workflow_stream(
    service: VisuraService,
    wf: WorkflowInput,
    workflow_id: Optional[str] = None,
    resume: bool = False,
):
    """Execute a workflow yielding SSE events for each step as it completes.

    Yields JSON-serialized dicts with event types:
      - {"event": "step", "step": ..., "status": ..., "data": ...}
      - {"event": "done", "workflow_id": ..., "summary": ..., "aggregate": ...}
    """
    preset_def = WORKFLOW_PRESETS[wf.preset]
    max_depth = _DEPTH_ORDER.get(wf.depth, 1)

    steps_to_run = []
    for step_name in preset_def["steps"]:
        meta = STEP_METADATA.get(step_name, {"depth": "standard", "paid": False})
        step_depth = _DEPTH_ORDER.get(meta["depth"], 1)
        if step_depth > max_depth:
            continue
        if meta["paid"] and not (wf.include_paid_steps and wf.auto_confirm):
            continue
        steps_to_run.append(step_name)

    field_map = {
        "provincia": wf.provincia, "comune": wf.comune, "foglio": wf.foglio,
        "particella": wf.particella, "codice_fiscale": wf.codice_fiscale,
        "identificativo": wf.identificativo, "indirizzo": wf.indirizzo,
    }
    missing = [f for f in preset_def["requires"] if not field_map.get(f)]
    if missing:
        yield json.dumps({"event": "error", "error": f"Missing required fields: {', '.join(missing)}"}, default=str)
        return

    if not workflow_id:
        workflow_id = f"wf_{wf.preset}_{uuid4().hex[:12]}"

    params = {
        "provincia": wf.provincia, "comune": wf.comune,
        "foglio": wf.foglio, "particella": wf.particella,
        "tipo_catasto": wf.tipo_catasto or "T",
        "sezione": wf.sezione, "sezione_urbana": wf.sezione_urbana, "subalterno": wf.subalterno,
        "codice_fiscale": wf.codice_fiscale,
        "identificativo": wf.identificativo,
        "indirizzo": wf.indirizzo,
        "auto_confirm": wf.auto_confirm,
        "max_fanout": wf.max_fanout,
        "max_owners": wf.max_owners,
        "max_properties_per_owner": wf.max_properties_per_owner,
        "max_historical_properties": wf.max_historical_properties,
        "max_paid_steps": wf.max_paid_steps,
        "max_total_steps": wf.max_total_steps,
        "_paid_step_count": 0,
        "_total_step_count": 0,
    }

    try:
        await _save_workflow_run(workflow_id, wf.preset, params)
    except Exception as e:
        logger.warning("Failed to persist workflow run: %s", e)

    # Yield initial event with plan
    yield json.dumps({
        "event": "start",
        "workflow_id": workflow_id,
        "preset": wf.preset,
        "description": preset_def["description"],
        "planned_steps": steps_to_run,
    }, default=str)

    completed_steps: dict[str, dict] = {}
    if resume:
        try:
            completed_steps = await _load_completed_steps(workflow_id)
        except Exception as e:
            logger.warning("Failed to load completed steps for resume: %s", e)

    step_results: list[dict] = []

    async with service.browser_manager._page_lock:
        page = await service.browser_manager._get_authenticated_page()

        for step_name in steps_to_run:
            if params.get("_total_step_count", 0) >= params.get("max_total_steps", 100):
                sr = {"step": step_name, "status": "skipped", "data": None, "reason": "max_total_steps reached"}
                step_results.append(sr)
                yield json.dumps({"event": "step", **sr}, default=str)
                continue

            sk = _step_key(step_name)
            executor = _STEP_EXECUTORS.get(step_name)

            if not executor:
                sr = {"step": step_name, "status": "skipped", "data": None, "error": f"Unknown step: {step_name}"}
                step_results.append(sr)
                yield json.dumps({"event": "step", **sr}, default=str)
                continue

            meta = STEP_METADATA.get(step_name, {})
            when_fn = meta.get("when")
            if when_fn and not when_fn(step_results, params):
                sr = {"step": step_name, "status": "skipped", "data": None, "reason": "precondition not met"}
                step_results.append(sr)
                yield json.dumps({"event": "step", **sr}, default=str)
                continue

            if sk in completed_steps:
                cached = completed_steps[sk]
                sr = {"step": step_name, "status": "completed", "data": cached.get("data"), "resumed": True}
                step_results.append(sr)
                yield json.dumps({"event": "step", **sr}, default=str)
                continue

            # Yield "running" event before execution
            yield json.dumps({"event": "step", "step": step_name, "status": "running"}, default=str)

            for attempt in range(2):
                try:
                    data = await executor(page, params, step_results)
                    sr = {"step": step_name, "status": "completed", "data": data}
                    step_results.append(sr)
                    params["_total_step_count"] = params.get("_total_step_count", 0) + 1

                    try:
                        await _save_workflow_step(workflow_id, sk, "completed", result_json=data)
                    except Exception as e:
                        logger.warning("Failed to persist step '%s': %s", step_name, e)

                    yield json.dumps({"event": "step", **sr}, default=str)
                    break

                except Exception as e:
                    is_session_expired = "Sessione scaduta" in str(e)
                    if is_session_expired and attempt == 0:
                        logger.warning("Session expired during step '%s', re-authenticating...", step_name)
                        try:
                            page = await service.browser_manager._get_authenticated_page()
                            continue
                        except Exception as reauth_err:
                            logger.error("Re-authentication failed: %s", reauth_err)

                    logger.error("Workflow step '%s' failed: %s", step_name, e)
                    sr = {"step": step_name, "status": "error", "error": str(e)}
                    step_results.append(sr)

                    try:
                        await _save_workflow_step(workflow_id, sk, "error", error=str(e))
                    except Exception:
                        pass

                    yield json.dumps({"event": "step", **sr}, default=str)

    # Final aggregate
    aggregate = _build_aggregate(step_results)
    completed_count = sum(1 for s in step_results if s["status"] == "completed")
    failed_count = sum(1 for s in step_results if s["status"] == "error")
    skipped_count = sum(1 for s in step_results if s["status"] == "skipped")

    output = {
        "workflow_id": workflow_id,
        "preset": wf.preset,
        "description": preset_def["description"],
        "steps": step_results,
        "aggregate": aggregate,
        "summary": {
            "total_steps": len(step_results),
            "completed": completed_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "properties": len(aggregate["properties"]),
            "owners": len(aggregate["owners"]),
            "addresses": len(aggregate.get("addresses", [])),
            "risk_flags": len(aggregate.get("risk_flags", [])),
            "links": len(aggregate.get("links", [])),
            "timeline_events": len(aggregate.get("timeline", [])),
        },
    }

    final_status = "completed" if failed_count == 0 else "partial"
    try:
        await _finish_workflow_run(workflow_id, final_status, output)
    except Exception as e:
        logger.warning("Failed to finalize workflow run: %s", e)

    try:
        from .database import OUTPUTS_DIR
        outputs_dir = Path(OUTPUTS_DIR)
        outputs_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = outputs_dir / f"{wf.preset}_{ts}.json"
        output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str))
    except Exception:
        pass

    yield json.dumps({"event": "done", **output}, default=str)
