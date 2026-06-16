"""Backward-compatible re-exports from aecs4u_workflow.

The workflow engine was moved to opendata; helper functions and models are now
in the shared aecs4u-workflow package. This shim keeps existing imports working.
"""

from aecs4u_workflow.executors import (  # noqa: F401
    STEP_EXECUTORS as _STEP_EXECUTORS,
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
from aecs4u_workflow.models import (  # noqa: F401
    STEP_METADATA,
    WORKFLOW_PRESETS,
    WorkflowInput,
    _DEPTH_ORDER,
)
