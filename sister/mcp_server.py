"""Local MCP tools for the SISTER HTTP API.

The MCP process does not start a browser or log in to SISTER. It talks to an already-running
SISTER service through :class:`VisuraClient`.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv

from .client import VisuraClient
from .query_forms import QUERY_FORMS, get_query_form, validate_params

load_dotenv()
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

PREVIEW_TTL_SECONDS = 15 * 60
MAX_PREVIEWS = 100
_SIDE_EFFECT_WARNING = (
    "This sends a request to the SISTER portal. Depending on the operation, it may incur fees and may require a human "
    "to solve a CAPTCHA."
)


@dataclass(frozen=True)
class PreparedQuery:
    command: str
    params: dict[str, str]
    created_at: float


_prepared_queries: dict[str, PreparedQuery] = {}


def _client() -> VisuraClient:
    return VisuraClient(
        base_url=os.getenv("VISURA_API_URL"),
        api_key=os.getenv("VISURA_API_KEY") or os.getenv("API_KEY"),
    )


def _normalise_params(params: dict[str, Any]) -> dict[str, str]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object mapping query parameter names to values")

    normalized: dict[str, str] = {}
    for name, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            normalized[str(name)] = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            normalized[str(name)] = str(value)
        else:
            raise ValueError(f"parameter {name!r} must be a string, number, or boolean")
    return normalized


def _query_description(spec) -> dict[str, Any]:
    fields = []
    seen: set[str] = set()
    for field in spec.fields:
        if field.param in seen:
            continue
        seen.add(field.param)
        fields.append(
            {
                "name": field.param,
                "type": field.kind,
                "description": field.help,
                "choices": field.choices,
                "required": field.param in spec.required,
                "requires": list(field.requires) if field.requires else None,
            }
        )
    return {
        "command": spec.command,
        "description": spec.summary,
        "api_path": spec.path,
        "parameters": fields,
        "required_any": [list(group) for group in spec.required_any],
    }


def _prune_previews(now: float) -> None:
    expired = [
        key
        for key, value in _prepared_queries.items()
        if now - value.created_at > PREVIEW_TTL_SECONDS
    ]
    for key in expired:
        _prepared_queries.pop(key, None)

    while len(_prepared_queries) > MAX_PREVIEWS:
        oldest = min(_prepared_queries, key=lambda key: _prepared_queries[key].created_at)
        _prepared_queries.pop(oldest, None)


def create_server():
    """Build the MCP server, importing the optional SDK only when this command is run."""
    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - depends on the optional install
        raise SystemExit("Install the MCP extra first: uv sync --extra mcp") from exc

    server = MCPServer("SISTER")
    read_local = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    read_remote = ToolAnnotations(read_only_hint=True, open_world_hint=True)
    submit_action = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    )

    @server.tool(title="List SISTER query types", annotations=read_local)
    def list_queries() -> list[dict[str, Any]]:
        """List supported SISTER query commands, their parameters, choices, and requirements."""
        return [_query_description(QUERY_FORMS[name]) for name in sorted(QUERY_FORMS)]

    @server.tool(title="Preview a SISTER query", annotations=read_local)
    def preview_query(command: str, params: dict[str, Any]) -> dict[str, Any]:
        """Validate a query without contacting SISTER and prepare a short-lived submission preview.

        Use parameter names from list_queries. Values may be strings, numbers, or booleans; booleans are
        converted to the lowercase strings expected by the SISTER API. The returned preview_id is required
        by submit_query and expires after 15 minutes.
        """
        spec = get_query_form(command)
        if spec is None:
            return {
                "valid": False,
                "error": f"Unknown query command: {command}",
                "available_commands": sorted(QUERY_FORMS),
            }

        try:
            normalized = _normalise_params(params)
        except ValueError as exc:
            return {"valid": False, "command": spec.command, "error": str(exc)}

        allowed = {field.param for field in spec.fields}
        unknown = sorted(set(normalized) - allowed)
        if unknown:
            return {
                "valid": False,
                "command": spec.command,
                "error": f"Unknown parameters for '{spec.command}': {', '.join(unknown)}",
            }

        try:
            validate_params(spec.command, normalized)
        except ValueError as exc:
            return {
                "valid": False,
                "command": spec.command,
                "description": spec.summary,
                "api_path": spec.path,
                "error": str(exc),
                "requirements": {
                    "required": list(spec.required),
                    "required_any": [list(group) for group in spec.required_any],
                },
            }

        now = time.monotonic()
        _prune_previews(now)
        preview_id = uuid4().hex
        _prepared_queries[preview_id] = PreparedQuery(spec.command, normalized, now)
        _prune_previews(now)
        return {
            "valid": True,
            "preview_id": preview_id,
            "expires_in_seconds": PREVIEW_TTL_SECONDS,
            "command": spec.command,
            "description": spec.summary,
            "api_path": spec.path,
            "parameters": normalized,
            "side_effects": _SIDE_EFFECT_WARNING,
            "submit_requires_explicit_confirmation": True,
        }

    @server.tool(title="Submit a SISTER query", annotations=submit_action)
    async def submit_query(
        preview_id: str,
        confirm_submission: bool,
        force: bool = False,
    ) -> dict[str, Any]:
        """Submit a previously previewed query after the user confirms that exact request.

        Set confirm_submission=true only after the user explicitly asks to send this query. It is required
        for every submission. force=true bypasses SISTER's cache and can repeat a potentially billable request.
        Without force, an identical old answer is returned as stale instead of being resubmitted.
        """
        now = time.monotonic()
        _prune_previews(now)
        prepared = _prepared_queries.get(preview_id)
        if prepared is None:
            return {
                "submitted": False,
                "error": "Preview not found or expired. Call preview_query again before submitting.",
            }
        if not confirm_submission:
            return {
                "submitted": False,
                "preview_id": preview_id,
                "error": "Submission refused. Ask the user to confirm this exact query first.",
            }

        # Consume the preview before network I/O so the same token cannot submit twice.
        _prepared_queries.pop(preview_id, None)
        async with _client() as client:
            result = await client.submit(
                prepared.command,
                prepared.params,
                force=force,
                reuse_previous=not force,
            )

        ids = VisuraClient.request_ids(result)
        cached_results = result.get("cached_results") or []
        stale = bool(result.get("stale")) or any(item.get("stale") for item in cached_results)
        used_previous_result = (
            result.get("status") == "cached"
            or bool(cached_results)
            or stale
            or (result.get("timestamp") is not None and "data" in result)
        )
        new_request_sent = result.get("status") in {"queued", "processing"} or (
            bool(ids) and not used_previous_result
        )
        return {
            "submitted": new_request_sent,
            "used_previous_result": used_previous_result,
            "stale": stale,
            "status": result.get("status", "unknown"),
            "request_ids": ids,
            "message": (
                "New request(s) accepted; some results were reused."
                if new_request_sent and used_previous_result
                else "A previous result was returned; no new request was sent."
                if used_previous_result
                else result.get("message", "SISTER accepted the query.")
            ),
            "force": force,
        }

    @server.tool(title="Get a SISTER request result", annotations=read_remote)
    async def get_request(request_id: str) -> dict[str, Any]:
        """Get the current status and result data for a SISTER request ID.

        Results may contain personal and property information.
        """
        async with _client() as client:
            return await client.get_result(request_id)

    @server.tool(title="Search SISTER history", annotations=read_remote)
    async def search_history(
        provincia: str | None = None,
        comune: str | None = None,
        foglio: str | None = None,
        particella: str | None = None,
        tipo_catasto: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search stored SISTER response history; results can contain personal and property information."""
        if not 1 <= limit <= 50:
            return {"error": "limit must be between 1 and 50"}
        async with _client() as client:
            return await client.history(
                provincia=provincia,
                comune=comune,
                foglio=foglio,
                particella=particella,
                tipo_catasto=tipo_catasto,
                limit=limit,
            )

    @server.tool(title="Check SISTER service health", annotations=read_remote)
    async def sister_health() -> dict[str, Any]:
        """Check whether the configured SISTER API is reachable and healthy."""
        async with _client() as client:
            return await client.health()

    return server


def run() -> None:
    """Run the local MCP server over stdio."""
    create_server().run(transport="stdio")


if __name__ == "__main__":
    run()
