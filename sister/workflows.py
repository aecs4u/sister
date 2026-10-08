"""Local execution for multi-step cadastral workflows."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from uuid import uuid4

from aecs4u_workflow.executors import (
    STEP_EXECUTORS,
    _build_aggregate,
    _deduplicate_properties,
    _exec_azienda,
    _exec_cross_property_intestati,
    _exec_drill_intestati,
    _exec_elaborato_planimetrico,
    _exec_elenco,
    _exec_export_mappa,
    _exec_fiduciali,
    _exec_indirizzo_reverse,
    _exec_indirizzo_search,
    _exec_intestati,
    _exec_ispezione_ipotecaria,
    _exec_ispezioni,
    _exec_ispezioni_cart,
    _exec_mappa,
    _exec_nota,
    _exec_originali,
    _exec_owner_expand,
    _exec_portfolio_drill_intestati,
    _exec_portfolio_history,
    _exec_portfolio_ipotecaria,
    _exec_property_rank,
    _exec_risk_score,
    _exec_search,
    _exec_soggetto,
    _exec_timeline_build,
    _normalize_property,
    _step_key,
)
from aecs4u_workflow.models import (
    STEP_METADATA,
    WORKFLOW_PRESETS,
    WorkflowInput,
    _DEPTH_ORDER,
)
from pydantic import ValidationError

from .client import VisuraClient

logger = logging.getLogger("sister")


@dataclass
class WorkflowPlan:
    """Validated workflow request plus its selected step sequence."""

    workflow: WorkflowInput
    family: str
    description: str
    steps: list[str]
    extra_params: dict


def _is_true(value) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "si", "sì", "on"}


def prepare_workflow(payload: dict) -> WorkflowPlan:
    """Normalize compact form families to the existing workflow step presets."""
    data = dict(payload)
    family = str(data.get("preset") or "").strip().lower()
    depth = str(data.get("depth") or "standard").strip().lower()
    extra_params = {}
    custom_steps = None

    if family == "due-diligence":
        include_history = _is_true(data.pop("include_history", False))
        numero_nota = data.pop("numero_nota", None)
        if numero_nota:
            extra_params["numero_nota"] = str(numero_nota).strip()

        if depth == "full":
            data["preset"] = "full-due-diligence"
            custom_steps = [
                "search",
                "intestati",
                *( ["nota"] if include_history else [] ),
                "ispezioni",
                *( ["ispezioni_cart", "originali"] if include_history else [] ),
                "elaborato_planimetrico",
                "drill_intestati",
                "owner_expand",
                "property_rank",
                "portfolio_drill_intestati",
                "portfolio_history",
                "timeline_build",
                "ispezione_ipotecaria",
                "portfolio_ipotecaria",
                "risk_score",
            ]
        elif include_history:
            data["preset"] = "storico"
        else:
            data["preset"] = "due-diligence"

    elif family == "portfolio":
        codice_fiscale = str(data.get("codice_fiscale") or "").strip()
        identificativo = str(data.get("identificativo") or "").strip()
        if bool(codice_fiscale) == bool(identificativo):
            raise ValueError("Enter either a codice fiscale or a company identifier.")
        if depth == "full":
            data["preset"] = "full-patrimonio" if codice_fiscale else "full-aziendale"
        else:
            data["preset"] = "patrimonio" if codice_fiscale else "aziendale"

    workflow = WorkflowInput.model_validate(data)
    definition = WORKFLOW_PRESETS[workflow.preset]
    step_names = custom_steps or definition["steps"]
    max_depth = _DEPTH_ORDER[workflow.depth]
    steps = [
        name
        for name in step_names
        if _DEPTH_ORDER.get(STEP_METADATA.get(name, {}).get("depth", "standard"), 1) <= max_depth
        and (
            not STEP_METADATA.get(name, {}).get("paid")
            or (workflow.include_paid_steps and workflow.auto_confirm)
        )
    ]

    description = definition["description"]
    if family == "due-diligence":
        description = "Parcel due diligence with optional history and depth-based enrichment."
    elif family == "portfolio":
        description = "Person or company asset investigation with depth-based portfolio expansion."

    return WorkflowPlan(
        workflow=workflow,
        family=family or workflow.preset,
        description=description,
        steps=steps,
        extra_params=extra_params,
    )


def _build_params(workflow: WorkflowInput, extra: dict) -> dict:
    return {
        "provincia": workflow.provincia,
        "comune": workflow.comune,
        "foglio": workflow.foglio,
        "particella": workflow.particella,
        "tipo_catasto": workflow.tipo_catasto or "T",
        "sezione": workflow.sezione,
        "sezione_urbana": workflow.sezione_urbana,
        "subalterno": workflow.subalterno,
        "codice_fiscale": workflow.codice_fiscale,
        "identificativo": workflow.identificativo,
        "indirizzo": workflow.indirizzo,
        "auto_confirm": workflow.auto_confirm,
        "max_fanout": workflow.max_fanout,
        "max_owners": workflow.max_owners,
        "max_properties_per_owner": workflow.max_properties_per_owner,
        "max_historical_properties": workflow.max_historical_properties,
        "max_paid_steps": workflow.max_paid_steps,
        "max_total_steps": workflow.max_total_steps,
        "_paid_step_count": 0,
        "_total_step_count": 0,
        **extra,
    }


def _missing_required_fields(workflow: WorkflowInput) -> list[str]:
    definition = WORKFLOW_PRESETS[workflow.preset]
    values = {
        "provincia": workflow.provincia,
        "comune": workflow.comune,
        "foglio": workflow.foglio,
        "particella": workflow.particella,
        "codice_fiscale": workflow.codice_fiscale,
        "identificativo": workflow.identificativo,
        "indirizzo": workflow.indirizzo,
    }
    return [name for name in definition["requires"] if not values.get(name)]


async def run_workflow_stream(plan: WorkflowPlan, *, base_url: str | None = None, api_key: str | None = None):
    """Execute a workflow locally and yield JSON event payloads for SSE."""
    workflow = plan.workflow
    missing = _missing_required_fields(workflow)
    if missing:
        yield json.dumps({"event": "error", "error": f"Missing required fields: {', '.join(missing)}"})
        return

    workflow_id = f"wf_{plan.family}_{uuid4().hex[:12]}"
    params = _build_params(workflow, plan.extra_params)
    yield json.dumps(
        {
            "event": "start",
            "workflow_id": workflow_id,
            "preset": plan.family,
            "description": plan.description,
            "planned_steps": plan.steps,
        },
        default=str,
    )

    client = VisuraClient(
        base_url=base_url,
        api_key=api_key or os.getenv("VISURA_API_KEY") or os.getenv("API_KEY"),
    )
    async with client:
        try:
            health = await client.health()
            if health.get("status") != "healthy":
                raise RuntimeError(f"Sister service is not healthy (status={health.get('status')!r})")
        except Exception as exc:
            yield json.dumps({"event": "error", "error": f"Sister service unavailable: {exc}"})
            return

        step_results = []
        for step_name in plan.steps:
            if params["_total_step_count"] >= params["max_total_steps"]:
                result = {"step": step_name, "status": "skipped", "data": None, "reason": "max_total_steps reached"}
                step_results.append(result)
                yield json.dumps({"event": "step", **result}, default=str)
                continue

            metadata = STEP_METADATA.get(step_name, {})
            when = metadata.get("when")
            if when and not when(step_results, params):
                result = {"step": step_name, "status": "skipped", "data": None, "reason": "precondition not met"}
                step_results.append(result)
                yield json.dumps({"event": "step", **result}, default=str)
                continue

            executor = STEP_EXECUTORS.get(step_name)
            if executor is None:
                result = {"step": step_name, "status": "skipped", "data": None, "error": f"Unknown step: {step_name}"}
                step_results.append(result)
                yield json.dumps({"event": "step", **result}, default=str)
                continue

            yield json.dumps({"event": "step", "step": step_name, "status": "running"})
            try:
                data = await executor(
                    client,
                    params,
                    step_results,
                    poll_timeout=metadata.get("poll_timeout"),
                )
                result = {"step": step_name, "status": "completed", "data": data}
                params["_total_step_count"] += 1
            except Exception as exc:
                logger.exception("Workflow step %s failed", step_name)
                result = {"step": step_name, "status": "error", "error": str(exc)}
            step_results.append(result)
            yield json.dumps({"event": "step", **result}, default=str)
            if result["status"] == "error" and metadata.get("critical"):
                break

    aggregate = _build_aggregate(step_results)
    completed = sum(result["status"] == "completed" for result in step_results)
    failed = sum(result["status"] == "error" for result in step_results)
    skipped = sum(result["status"] == "skipped" for result in step_results)
    output = {
        "workflow_id": workflow_id,
        "preset": plan.family,
        "description": plan.description,
        "steps": step_results,
        "aggregate": aggregate,
        "summary": {
            "total_steps": len(step_results),
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "properties": len(aggregate["properties"]),
            "owners": len(aggregate["owners"]),
            "addresses": len(aggregate.get("addresses", [])),
            "risk_flags": len(aggregate.get("risk_flags", [])),
            "links": len(aggregate.get("links", [])),
            "timeline_events": len(aggregate.get("timeline", [])),
        },
    }
    yield json.dumps({"event": "done", **output}, default=str)


async def run_workflow(payload: dict) -> dict:
    """Execute a workflow and return its final event as a regular response."""
    plan = prepare_workflow(payload)
    result = None
    async for event in run_workflow_stream(plan):
        parsed = json.loads(event)
        if parsed.get("event") in {"done", "error"}:
            result = parsed
    return result or {"error": "Workflow produced no result", "steps": []}
