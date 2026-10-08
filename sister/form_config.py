"""Form group definitions for the sister web UI.

Defines the query forms rendered on the /web/forms page. Each FormGroup
maps to one or more sister API endpoints.

Categories:
  - "single": Single-step queries (search, soggetto, azienda, etc.)
  - "workflow": Multi-step workflow presets (due-diligence, patrimonio, etc.)

The "batch" mode is a toggle on any form group, not a separate group.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import WORKFLOW_PRESETS


@dataclass
class EndpointParam:
    name: str
    label: str
    placeholder: str
    required: bool = True
    input_type: str = "text"
    help_text: Optional[str] = None
    example: Optional[str] = None
    options: Optional[list[tuple[str, str]]] = None
    # ids of the endpoints that take this parameter (the form shows it only for those)
    endpoints: tuple[str, ...] = ()


@dataclass
class EndpointOption:
    id: str
    name: str
    path: str
    description: str
    method: str = "POST"


@dataclass
class FormGroup:
    id: str
    name: str
    description: str
    icon: str
    color: str
    params: list[EndpointParam]
    endpoints: list[EndpointOption]
    default_endpoint_id: str = ""
    category: str = "single"  # "single" or "workflow"
    available: bool = True
    # For workflows: the SVG flowchart name (without .svg)
    flowchart: Optional[str] = None


# ---------------------------------------------------------------------------
# Shared parameter definitions
# ---------------------------------------------------------------------------

_TIPO_CATASTO = EndpointParam(
    name="tipo_catasto",
    label="Cadastre Type",
    placeholder="Select type",
    input_type="select",
    required=False,
    options=[("", "Both (T+F)"), ("T", "Terreni (T)"), ("F", "Fabbricati (F)")],
    help_text="T = Land, F = Buildings. Leave blank for both.",
)

_TIPO_CATASTO_TF = EndpointParam(
    name="tipo_catasto",
    label="Cadastre Type",
    placeholder="Select type",
    input_type="select",
    required=False,
    options=[("T", "Terreni (T)"), ("F", "Fabbricati (F)")],
)

_TIPO_CATASTO_TFE = EndpointParam(
    name="tipo_catasto",
    label="Cadastre Type",
    placeholder="Select type",
    input_type="select",
    required=False,
    options=[("", "Both (E)"), ("T", "Terreni (T)"), ("F", "Fabbricati (F)")],
)

_PROVINCIA = EndpointParam(
    name="provincia",
    label="Province",
    placeholder="e.g. Roma",
    help_text="Province name",
    example="Roma",
)

_COMUNE = EndpointParam(
    name="comune",
    label="Municipality",
    placeholder="e.g. ROMA",
    help_text="Municipality name (uppercase)",
    example="ROMA",
)

_FOGLIO = EndpointParam(
    name="foglio",
    label="Sheet (Foglio)",
    placeholder="e.g. 100",
    example="100",
)

_PARTICELLA = EndpointParam(
    name="particella",
    label="Parcel (Particella)",
    placeholder="e.g. 50",
    example="50",
)

_SEZIONE = EndpointParam(
    name="sezione",
    label="Section (Sezione)",
    placeholder="e.g. A, B, C",
    required=False,
    help_text="Census section code for multi-section comuni (e.g. A = RAVENNA, C = SAVIO). Leave blank if not applicable.",
)

_SEZIONE_URBANA = EndpointParam(
    name="sezione_urbana",
    label="Urban Section (Sezione Urbana)",
    placeholder="e.g. RA",
    required=False,
    help_text="2-3 char urban section code for Fabbricati (e.g. RA, PA)",
)

_SUBALTERNO = EndpointParam(
    name="subalterno",
    label="Sub-unit (Subalterno)",
    placeholder="e.g. 3",
    required=False,
    help_text="Required for Fabbricati intestati",
)

_PROVINCIA_OPT = EndpointParam(
    name="provincia",
    label="Province",
    placeholder="Leave blank for national search",
    required=False,
    help_text="Omit for nationwide search",
)

_FOGLIO_OPT = EndpointParam(
    name="foglio",
    label="Sheet",
    placeholder="Required for property presets",
    required=False,
)

_PARTICELLA_OPT = EndpointParam(
    name="particella",
    label="Parcel",
    placeholder="Required for property presets",
    required=False,
)


# ---------------------------------------------------------------------------
# Single-step form groups
# ---------------------------------------------------------------------------

SINGLE_STEP_GROUPS: list[FormGroup] = [
    FormGroup(
        id="property-search",
        name="Property Search",
        description="Search for properties by cadastral coordinates (sheet + parcel).",
        icon="fa-search",
        color="primary",
        category="single",
        params=[_TIPO_CATASTO, _PROVINCIA, _COMUNE, _SEZIONE, _SEZIONE_URBANA, _FOGLIO, _PARTICELLA, _SUBALTERNO],
        endpoints=[
            EndpointOption(
                id="visura",
                name="Property Data",
                path="/visura",
                method="POST",
                description="Find all properties on a parcel (Fase 1)",
            ),
            EndpointOption(
                id="intestati",
                name="Owner Lookup",
                path="/visura/intestati",
                method="POST",
                description="Get owners for a specific property (Fase 2)",
            ),
        ],
        default_endpoint_id="visura",
    ),
    FormGroup(
        id="person-search",
        name="Person Search",
        description="National search by codice fiscale.",
        icon="fa-user",
        color="info",
        category="single",
        params=[
            EndpointParam(
                name="codice_fiscale",
                label="Codice Fiscale",
                placeholder="e.g. RSSMRI85E28H501E",
                help_text="16-character tax code",
                example="RSSMRI85E28H501E",
            ),
            _TIPO_CATASTO_TFE,
            _PROVINCIA_OPT,
        ],
        endpoints=[
            EndpointOption(
                id="soggetto",
                name="Person Search",
                path="/visura/soggetto",
                method="POST",
                description="National search by codice fiscale",
            ),
        ],
        default_endpoint_id="soggetto",
    ),
    FormGroup(
        id="company-search",
        name="Company Search",
        description="Search by P.IVA or company name.",
        icon="fa-building",
        color="warning",
        category="single",
        params=[
            EndpointParam(
                name="identificativo",
                label="P.IVA or Company Name",
                placeholder="e.g. 02471840997",
                help_text="Enter 11-digit P.IVA or company denomination",
                example="02471840997",
            ),
            _TIPO_CATASTO_TFE,
            _PROVINCIA_OPT,
        ],
        endpoints=[
            EndpointOption(
                id="persona-giuridica",
                name="Company Search",
                path="/visura/persona-giuridica",
                method="POST",
                description="Search by P.IVA or denomination",
            ),
        ],
        default_endpoint_id="persona-giuridica",
    ),
    FormGroup(
        id="property-list",
        name="Property List",
        description="List all properties in a municipality.",
        icon="fa-list",
        color="success",
        category="single",
        params=[
            _PROVINCIA,
            _COMUNE,
            _TIPO_CATASTO_TF,
            EndpointParam(
                name="foglio",
                label="Sheet (Foglio)",
                placeholder="Optional — filter by sheet",
                required=False,
            ),
            _SEZIONE,
        ],
        endpoints=[
            EndpointOption(
                id="elenco-immobili",
                name="Property List",
                path="/visura/elenco-immobili",
                method="POST",
                description="List all properties in a municipality",
            ),
        ],
        default_endpoint_id="elenco-immobili",
    ),
    FormGroup(
        id="address-search",
        name="Address Search",
        description="Search properties by street address.",
        icon="fa-map-marker-alt",
        color="danger",
        category="single",
        params=[
            _PROVINCIA,
            _COMUNE,
            _TIPO_CATASTO_TF,
            EndpointParam(
                name="indirizzo",
                label="Address",
                placeholder="e.g. VIA ROMA",
                help_text="Street name (partial match supported)",
                example="VIA ROMA",
            ),
        ],
        endpoints=[
            EndpointOption(
                id="indirizzo",
                name="Address Search",
                path="/visura/indirizzo",
                method="POST",
                description="Find properties at a given address",
            ),
        ],
        default_endpoint_id="indirizzo",
    ),
    FormGroup(
        id="nota-search",
        name="Note Search",
        description="Search the formalities of a nota (Ricerca per nota).",
        icon="fa-file-signature",
        color="secondary",
        category="single",
        params=[],  # generated from the CLI command, see generate_single_step_params()
        endpoints=[
            EndpointOption(
                id="nota",
                name="Note Search",
                path="/visura/nota",
                method="POST",
                description="Find properties by nota number and year",
            ),
        ],
        default_endpoint_id="nota",
    ),
    FormGroup(
        id="partita-search",
        name="Partita Search",
        description="Search by partita catastale number.",
        icon="fa-hashtag",
        color="secondary",
        category="single",
        params=[
            _PROVINCIA,
            _COMUNE,
            _TIPO_CATASTO_TF,
            EndpointParam(
                name="partita",
                label="Partita Number",
                placeholder="e.g. 12345",
                help_text="Cadastral partita number",
            ),
        ],
        endpoints=[
            EndpointOption(
                id="partita",
                name="Partita Search",
                path="/visura/partita",
                method="POST",
                description="Search by partita catastale number",
            ),
        ],
        default_endpoint_id="partita",
    ),
    FormGroup(
        id="mappa",
        name="Cadastral Map",
        description="View cadastral map data for a foglio.",
        icon="fa-map",
        color="dark",
        category="single",
        params=[
            _PROVINCIA,
            _COMUNE,
            _FOGLIO,
            EndpointParam(
                name="particella",
                label="Parcel (optional)",
                placeholder="e.g. 50",
                required=False,
            ),
            _SEZIONE,
        ],
        endpoints=[
            EndpointOption(
                id="mappa",
                name="Map View",
                path="/visura/mappa",
                method="POST",
                description="View cadastral map data (EM)",
            ),
            EndpointOption(
                id="export-mappa",
                name="Export Map",
                path="/visura/export-mappa",
                method="POST",
                description="Export cadastral map data (EXPM)",
            ),
        ],
        default_endpoint_id="mappa",
    ),
    FormGroup(
        id="elaborato-planimetrico",
        name="Elaborato Planimetrico",
        description="Retrieve planimetric document for a property.",
        icon="fa-drafting-compass",
        color="dark",
        category="single",
        params=[_PROVINCIA, _COMUNE, _FOGLIO_OPT],
        endpoints=[
            EndpointOption(
                id="elaborato-planimetrico",
                name="Elaborato Planimetrico",
                path="/visura/elaborato-planimetrico",
                method="POST",
                description="Planimetric document (ELPL)",
            ),
        ],
        default_endpoint_id="elaborato-planimetrico",
    ),
    FormGroup(
        id="originali-impianto",
        name="Original Records",
        description="Original registration records and survey points.",
        icon="fa-archive",
        color="dark",
        category="single",
        params=[_PROVINCIA, _COMUNE, _TIPO_CATASTO_TF, _FOGLIO_OPT],
        endpoints=[
            EndpointOption(
                id="originali",
                name="Original Records",
                path="/visura/originali",
                method="POST",
                description="Original registration records (OOII)",
            ),
            EndpointOption(
                id="fiduciali",
                name="Survey Points",
                path="/visura/fiduciali",
                method="POST",
                description="Survey reference points (FID)",
            ),
        ],
        default_endpoint_id="originali",
    ),
    FormGroup(
        id="ispezioni",
        name="Inspections",
        description="Property inspection records (digital and paper).",
        icon="fa-clipboard-check",
        color="dark",
        category="single",
        params=[_PROVINCIA, _COMUNE, _TIPO_CATASTO_TF, _FOGLIO_OPT, _PARTICELLA_OPT],
        endpoints=[
            EndpointOption(
                id="ispezioni",
                name="Digital Inspections",
                path="/visura/ispezioni",
                method="POST",
                description="Property inspection records (ISP)",
            ),
            EndpointOption(
                id="ispezioni-cartacee",
                name="Paper Inspections",
                path="/visura/ispezioni-cartacee",
                method="POST",
                description="Paper inspection records (ISPCART)",
            ),
        ],
        default_endpoint_id="ispezioni",
    ),
    FormGroup(
        id="ispezione-ipotecaria",
        name="Ispezione Ipotecaria",
        description="Paid property inspection (search by property, person, company, or note). Incurs a cost per query.",
        icon="fa-file-invoice-dollar",
        color="danger",
        category="single",
        params=[
            EndpointParam(
                name="tipo_ricerca",
                label="Search Type",
                placeholder="Select search type",
                input_type="select",
                required=True,
                options=[
                    ("immobile", "Property (Immobile)"),
                    ("persona_fisica", "Person (Persona Fisica)"),
                    ("persona_giuridica", "Company (Persona Giuridica)"),
                    ("nota", "Note (Nota)"),
                ],
                help_text="Type of inspection search",
            ),
            _PROVINCIA,
            EndpointParam(
                name="comune",
                label="Municipality",
                placeholder="e.g. ROMA",
                required=False,
                help_text="Municipality name (for immobile search)",
            ),
            _TIPO_CATASTO_TF,
            EndpointParam(
                name="codice_fiscale",
                label="Codice Fiscale",
                placeholder="e.g. RSSMRI85E28H501E",
                required=False,
                help_text="For persona_fisica search",
            ),
            EndpointParam(
                name="identificativo",
                label="P.IVA / Company",
                placeholder="e.g. 02471840997",
                required=False,
                help_text="For persona_giuridica search",
            ),
            EndpointParam(
                name="foglio",
                label="Sheet (Foglio)",
                placeholder="e.g. 100",
                required=False,
                help_text="For immobile search",
            ),
            EndpointParam(
                name="particella",
                label="Parcel (Particella)",
                placeholder="e.g. 50",
                required=False,
                help_text="For immobile search",
            ),
            EndpointParam(
                name="numero_nota",
                label="Note Number",
                placeholder="e.g. 12345",
                required=False,
                help_text="For nota search",
            ),
            EndpointParam(
                name="anno_nota",
                label="Note Year",
                placeholder="e.g. 2024",
                required=False,
                help_text="For nota search",
            ),
            EndpointParam(
                name="auto_confirm",
                label="Auto-confirm cost",
                placeholder="",
                input_type="select",
                required=False,
                options=[("false", "No — show cost first"), ("true", "Yes — auto-approve")],
                help_text="WARNING: Setting to Yes will automatically confirm the cost and charge your account.",
            ),
        ],
        endpoints=[
            EndpointOption(
                id="ispezione-ipotecaria",
                name="Ispezione Ipotecaria",
                path="/visura/ispezione-ipotecaria",
                method="POST",
                description="Paid property inspection (requires cost confirmation)",
            ),
        ],
        default_endpoint_id="ispezione-ipotecaria",
    ),
    FormGroup(
        id="riepilogo",
        name="Query Summary",
        description="View your SISTER query history and pending requests.",
        icon="fa-history",
        color="secondary",
        category="single",
        params=[],  # No parameters needed
        endpoints=[
            EndpointOption(
                id="riepilogo-visure",
                name="Query Summary",
                path="/visura/riepilogo-visure",
                method="POST",
                description="Your SISTER query history (Riepilogo Visure)",
            ),
            EndpointOption(
                id="richieste",
                name="Pending Requests",
                path="/visura/richieste",
                method="POST",
                description="Pending/completed SISTER requests (Richieste)",
            ),
        ],
        default_endpoint_id="riepilogo-visure",
    ),
]


# ---------------------------------------------------------------------------
# Workflow (multi-step) form groups
# ---------------------------------------------------------------------------


def _PRESET_HIDDEN(preset_name):
    return EndpointParam(
        name="preset",
        label="",
        placeholder="",
        input_type="hidden",
        required=True,
        example=preset_name,
    )


_WORKFLOW_DEPTH = EndpointParam(
    name="depth",
    label="Depth",
    placeholder="Select depth",
    input_type="select",
    required=False,
    options=[
        ("light", "Light — core steps only"),
        ("standard", "Standard — with enrichment"),
        ("deep", "Deep — owner expansion + paid"),
        ("full", "Full — multi-hop graph expansion"),
    ],
    help_text="Controls which steps run. Full adds portfolio drill-down, history bundles, and paid encumbrance checks.",
)

_WORKFLOW_PAID = EndpointParam(
    name="include_paid_steps",
    label="Include Paid Steps",
    placeholder="",
    input_type="select",
    required=False,
    options=[("false", "No"), ("true", "Yes — include paid inspections")],
    help_text="Enable paid ispezione ipotecaria steps (requires Deep or Full depth).",
)

_WORKFLOW_CONFIRM = EndpointParam(
    name="auto_confirm",
    label="Auto-confirm Cost",
    placeholder="",
    input_type="select",
    required=False,
    options=[("false", "No — show cost first"), ("true", "Yes — auto-approve")],
    help_text="WARNING: Setting to Yes will automatically confirm paid service costs.",
)

_WORKFLOW_CODICE_FISCALE = EndpointParam(
    name="codice_fiscale",
    label="Codice Fiscale",
    placeholder="e.g. RSSMRI85E28H501E",
    example="RSSMRI85E28H501E",
)

_WORKFLOW_IDENTIFICATIVO = EndpointParam(
    name="identificativo",
    label="P.IVA / Company",
    placeholder="e.g. 02471840997",
    example="02471840997",
)

_WORKFLOW_ADDRESS = EndpointParam(
    name="indirizzo",
    label="Address",
    placeholder="e.g. VIA ROMA",
    help_text="Street name (partial match supported)",
    example="VIA ROMA",
)

_WORKFLOW_MAX_PAID_STEPS = EndpointParam(
    name="max_paid_steps",
    label="Max Paid Steps",
    placeholder="3",
    input_type="text",
    required=False,
    help_text="Maximum number of paid ispezione ipotecaria invocations (default: 3)",
)

_WORKFLOW_HISTORY = EndpointParam(
    name="include_history",
    label="Include Historical Records",
    placeholder="",
    input_type="select",
    required=False,
    options=[("false", "No"), ("true", "Yes — include notes and paper inspections")],
)

_WORKFLOW_NUMERO_NOTA = EndpointParam(
    name="numero_nota",
    label="Note Number",
    placeholder="Optional; used by historical note lookup",
    required=False,
)

_WORKFLOW_PORTFOLIO_CF = EndpointParam(
    name="codice_fiscale",
    label="Person's Codice Fiscale",
    placeholder="e.g. RSSMRI85E28H501E",
    required=False,
    example="RSSMRI85E28H501E",
)

_WORKFLOW_PORTFOLIO_COMPANY = EndpointParam(
    name="identificativo",
    label="Company P.IVA / Name",
    placeholder="e.g. 02471840997",
    required=False,
    example="02471840997",
)

_WORKFLOW_PROPERTY_PARAMS = (_PROVINCIA, _COMUNE, _FOGLIO, _PARTICELLA, _TIPO_CATASTO)
_WORKFLOW_ENTITY_PARAMS = (_TIPO_CATASTO_TFE, _PROVINCIA_OPT)
_WORKFLOW_DEPTH_PARAMS = (_WORKFLOW_DEPTH,)
_WORKFLOW_PAID_PARAMS = (_WORKFLOW_DEPTH, _WORKFLOW_PAID, _WORKFLOW_CONFIRM)

# Preset execution and step descriptions come from aecs4u_workflow.models.
# This table contains only the web-specific labels, styling, and input fields.
_WORKFLOW_FORM_SPECS = {
    "due-diligence": {
        "name": "Due Diligence",
        "icon": "fa-file-contract",
        "color": "primary",
        "description": "Parcel investigation. Depth selects standard or full multi-hop checks; history can be added when needed.",
        "flowchart": "due-diligence",
        "params": (
            *_WORKFLOW_PROPERTY_PARAMS,
            _SEZIONE,
            _SEZIONE_URBANA,
            _WORKFLOW_HISTORY,
            _WORKFLOW_NUMERO_NOTA,
            *_WORKFLOW_PAID_PARAMS,
            _WORKFLOW_MAX_PAID_STEPS,
        ),
    },
    "portfolio": {
        "name": "Portfolio Investigation",
        "icon": "fa-search-dollar",
        "color": "info",
        "description": "Search a person's or company's properties, then expand owners and portfolios by depth.",
        "flowchart": "patrimonio",
        "params": (
            _WORKFLOW_PORTFOLIO_CF,
            _WORKFLOW_PORTFOLIO_COMPANY,
            *_WORKFLOW_ENTITY_PARAMS,
            *_WORKFLOW_PAID_PARAMS,
            _WORKFLOW_MAX_PAID_STEPS,
        ),
    },
    "fondiario": {
        "name": "Land Survey",
        "icon": "fa-mountain",
        "color": "success",
        "params": (_PROVINCIA, _COMUNE, _FOGLIO_OPT, _TIPO_CATASTO_TF, *_WORKFLOW_DEPTH_PARAMS),
    },
    "indirizzo": {
        "name": "Address Lookup",
        "icon": "fa-map-marker-alt",
        "color": "danger",
        "params": (_PROVINCIA, _COMUNE, _WORKFLOW_ADDRESS, _TIPO_CATASTO, *_WORKFLOW_DEPTH_PARAMS),
    },
    "cross-reference": {
        "name": "Cross-Reference",
        "icon": "fa-exchange-alt",
        "color": "dark",
        "params": (
            _WORKFLOW_CODICE_FISCALE,
            _WORKFLOW_IDENTIFICATIVO,
            *_WORKFLOW_ENTITY_PARAMS,
            *_WORKFLOW_DEPTH_PARAMS,
        ),
    },
}


def _workflow_form_group(preset: str, spec: dict) -> FormGroup:
    """Build the web form for a shared workflow preset."""
    description = spec.get("description") or WORKFLOW_PRESETS[preset]["description"]
    endpoint_id = f"workflow-{preset}"
    return FormGroup(
        id=f"wf-{preset}",
        name=spec["name"],
        description=description,
        icon=spec["icon"],
        color=spec["color"],
        category="workflow",
        flowchart=spec.get("flowchart", preset),
        params=[_PRESET_HIDDEN(preset), *spec["params"]],
        endpoints=[
            EndpointOption(
                id=endpoint_id,
                name=spec["name"],
                path="/visura/workflow",
                method="POST",
                description=description,
            )
        ],
        default_endpoint_id=endpoint_id,
    )


WORKFLOW_GROUPS: list[FormGroup] = [
    _workflow_form_group(preset, spec) for preset, spec in _WORKFLOW_FORM_SPECS.items()
]


# ---------------------------------------------------------------------------
# Combined list
# ---------------------------------------------------------------------------

FORM_GROUPS: list[FormGroup] = SINGLE_STEP_GROUPS + WORKFLOW_GROUPS


# ---------------------------------------------------------------------------
# Single-step parameters: generated from the CLI commands
# ---------------------------------------------------------------------------

_CATASTO_OPTIONS = [("", "—"), ("T", "Terreni (T)"), ("F", "Fabbricati (F)"), ("E", "Both (E)")]
_generated = False


def _web_param(fld, required: bool, endpoint_id: str) -> EndpointParam:
    label = fld.param.replace("_", " ").capitalize()
    options = None
    input_type = "text"
    if fld.param == "tipo_catasto":
        input_type, options = "select", _CATASTO_OPTIONS
    elif fld.kind in ("select", "radio") and fld.choices:
        input_type, options = "select", [("", "—")] + [(key, key) for key in fld.choices]
    elif fld.kind == "checkbox":
        input_type, options = "select", [("", "—"), ("true", "Yes"), ("false", "No")]
    return EndpointParam(
        name=fld.param,
        label=label,
        placeholder=fld.help or label,
        required=required,
        input_type=input_type,
        help_text=fld.help or None,
        options=options,
        endpoints=(endpoint_id,),
    )


def generate_single_step_params() -> None:
    """Fill the parameters of every single-step group from the query specs (``sister.query_forms``).

    The spec is the single source: ``sister query <command>`` is generated from it too, so the web form
    and the CLI offer exactly the same inputs, with the same required ones.
    """
    global _generated
    if _generated:
        return
    from .query_forms import QUERY_FORMS, command_for_path

    for group in SINGLE_STEP_GROUPS:
        merged: dict[str, EndpointParam] = {}
        for endpoint in group.endpoints:
            spec = QUERY_FORMS.get(command_for_path(endpoint.path) or "")
            if spec is None:
                continue  # e.g. ispezione ipotecaria (paid): its form is defined by hand above
            seen: set[str] = set()
            # required parameters first, then the form inputs in portal order
            leading = [*spec.required, *(name for group in spec.required_any for name in group)]
            ordered = sorted(
                spec.fields, key=lambda f: leading.index(f.param) if f.param in leading else len(leading)
            )
            for fld in ordered:
                if fld.param in seen:
                    continue
                seen.add(fld.param)
                param = _web_param(fld, fld.param in spec.required, endpoint.id)
                if param.name in merged:
                    old = merged[param.name]
                    merged[param.name] = EndpointParam(
                        **{**old.__dict__, "endpoints": old.endpoints + param.endpoints, "required": old.required and param.required}
                    )
                else:
                    merged[param.name] = param
        if merged:
            group.params = list(merged.values())
    _generated = True


def get_available_form_groups() -> list[FormGroup]:
    """Return form groups that are available."""
    generate_single_step_params()
    return [fg for fg in FORM_GROUPS if fg.available]


def get_single_step_groups() -> list[FormGroup]:
    """Return single-step form groups."""
    generate_single_step_params()
    return [fg for fg in FORM_GROUPS if fg.available and fg.category == "single"]


def get_workflow_groups() -> list[FormGroup]:
    """Return workflow (multi-step) form groups."""
    return [fg for fg in FORM_GROUPS if fg.available and fg.category == "workflow"]


def get_form_group_by_id(group_id: str) -> Optional[FormGroup]:
    """Find a form group by ID."""
    generate_single_step_params()
    return next((fg for fg in FORM_GROUPS if fg.id == group_id), None)


def get_endpoint_by_id(endpoint_id: str) -> Optional[EndpointOption]:
    """Find an endpoint across all form groups."""
    generate_single_step_params()
    for fg in FORM_GROUPS:
        for ep in fg.endpoints:
            if ep.id == endpoint_id:
                return ep
    return None
