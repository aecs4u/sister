"""Form group definitions for the sister web UI.

Defines the query forms rendered on the /web/forms page. Each FormGroup
maps to one or more sister API endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


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
    category: str = "general"
    available: bool = True


# ---------------------------------------------------------------------------
# Shared parameter definitions
# ---------------------------------------------------------------------------

_TIPO_CATASTO = EndpointParam(
    name="tipo_catasto", label="Cadastre Type", placeholder="Select type",
    input_type="select", required=False,
    options=[("", "Both (T+F)"), ("T", "Terreni (T)"), ("F", "Fabbricati (F)")],
    help_text="T = Land, F = Buildings. Leave blank for both.",
)

_TIPO_CATASTO_TF = EndpointParam(
    name="tipo_catasto", label="Cadastre Type", placeholder="Select type",
    input_type="select", required=False,
    options=[("T", "Terreni (T)"), ("F", "Fabbricati (F)")],
)

_TIPO_CATASTO_TFE = EndpointParam(
    name="tipo_catasto", label="Cadastre Type", placeholder="Select type",
    input_type="select", required=False,
    options=[("", "Both (E)"), ("T", "Terreni (T)"), ("F", "Fabbricati (F)")],
)

_PROVINCIA = EndpointParam(
    name="provincia", label="Province", placeholder="e.g. Roma",
    help_text="Province name", example="Roma",
)

_COMUNE = EndpointParam(
    name="comune", label="Municipality", placeholder="e.g. ROMA",
    help_text="Municipality name (uppercase)", example="ROMA",
)

_FOGLIO = EndpointParam(
    name="foglio", label="Sheet (Foglio)", placeholder="e.g. 100",
    example="100",
)

_PARTICELLA = EndpointParam(
    name="particella", label="Parcel (Particella)", placeholder="e.g. 50",
    example="50",
)

_SEZIONE = EndpointParam(
    name="sezione", label="Section", placeholder="Optional",
    required=False, help_text="Census section (if applicable)",
)

_SUBALTERNO = EndpointParam(
    name="subalterno", label="Sub-unit (Subalterno)", placeholder="e.g. 3",
    required=False, help_text="Required for Fabbricati intestati",
)

_PROVINCIA_OPT = EndpointParam(
    name="provincia", label="Province", placeholder="Leave blank for national search",
    required=False, help_text="Omit for nationwide search",
)


# ---------------------------------------------------------------------------
# Form group definitions
# ---------------------------------------------------------------------------

FORM_GROUPS: list[FormGroup] = [
    FormGroup(
        id="property-search",
        name="Property Search",
        description="Search for properties by cadastral coordinates (sheet + parcel). Optionally retrieve owner information.",
        icon="fa-search",
        color="primary",
        category="property",
        params=[_TIPO_CATASTO, _PROVINCIA, _COMUNE, _SEZIONE, _FOGLIO, _PARTICELLA, _SUBALTERNO],
        endpoints=[
            EndpointOption(
                id="visura", name="Property Data",
                path="/visura", method="POST",
                description="Find all properties on a parcel (Fase 1)",
            ),
            EndpointOption(
                id="intestati", name="Owner Lookup",
                path="/visura/intestati", method="POST",
                description="Get owners for a specific property (Fase 2). Requires tipo_catasto and subalterno for buildings.",
            ),
        ],
        default_endpoint_id="visura",
    ),

    FormGroup(
        id="person-search",
        name="Person Search",
        description="National search by codice fiscale. Find all properties owned by a person across Italy.",
        icon="fa-user",
        color="info",
        category="subject",
        params=[
            EndpointParam(
                name="codice_fiscale", label="Codice Fiscale",
                placeholder="e.g. RSSMRI85E28H501E",
                help_text="16-character tax code", example="RSSMRI85E28H501E",
            ),
            _TIPO_CATASTO_TFE,
            _PROVINCIA_OPT,
        ],
        endpoints=[
            EndpointOption(
                id="soggetto", name="Person Search",
                path="/visura/soggetto", method="POST",
                description="National search by codice fiscale (Persona Fisica)",
            ),
        ],
        default_endpoint_id="soggetto",
    ),

    FormGroup(
        id="company-search",
        name="Company Search",
        description="Search by P.IVA or company name. Find all properties owned by a legal entity.",
        icon="fa-building",
        color="warning",
        category="subject",
        params=[
            EndpointParam(
                name="identificativo", label="P.IVA or Company Name",
                placeholder="e.g. 02471840997",
                help_text="Enter 11-digit P.IVA or company denomination",
                example="02471840997",
            ),
            _TIPO_CATASTO_TFE,
            _PROVINCIA_OPT,
        ],
        endpoints=[
            EndpointOption(
                id="persona-giuridica", name="Company Search",
                path="/visura/persona-giuridica", method="POST",
                description="Search by P.IVA or denomination (Persona Giuridica)",
            ),
        ],
        default_endpoint_id="persona-giuridica",
    ),

    FormGroup(
        id="property-list",
        name="Property List",
        description="List all properties in a municipality, optionally filtered by sheet number.",
        icon="fa-list",
        color="success",
        category="property",
        params=[_PROVINCIA, _COMUNE, _TIPO_CATASTO_TF, EndpointParam(
            name="foglio", label="Sheet (Foglio)", placeholder="Optional — filter by sheet",
            required=False, help_text="Leave blank to list all",
        ), _SEZIONE],
        endpoints=[
            EndpointOption(
                id="elenco-immobili", name="Property List",
                path="/visura/elenco-immobili", method="POST",
                description="List all properties in a municipality (Elenco Immobili)",
            ),
        ],
        default_endpoint_id="elenco-immobili",
    ),

    FormGroup(
        id="address-search",
        name="Address Search",
        description="Search properties by street address within a municipality.",
        icon="fa-map-marker-alt",
        color="danger",
        category="location",
        params=[
            _PROVINCIA, _COMUNE, _TIPO_CATASTO_TF,
            EndpointParam(
                name="indirizzo", label="Address",
                placeholder="e.g. VIA ROMA",
                help_text="Street name (partial match supported)",
                example="VIA ROMA",
            ),
        ],
        endpoints=[
            EndpointOption(
                id="indirizzo", name="Address Search",
                path="/visura/indirizzo", method="POST",
                description="Find properties at a given address",
            ),
        ],
        default_endpoint_id="indirizzo",
    ),

    FormGroup(
        id="partita-search",
        name="Partita Search",
        description="Search by partita catastale number.",
        icon="fa-hashtag",
        color="secondary",
        category="property",
        params=[
            _PROVINCIA, _COMUNE, _TIPO_CATASTO_TF,
            EndpointParam(
                name="partita", label="Partita Number",
                placeholder="e.g. 12345",
                help_text="Cadastral partita number",
            ),
        ],
        endpoints=[
            EndpointOption(
                id="partita", name="Partita Search",
                path="/visura/partita", method="POST",
                description="Search by partita catastale number",
            ),
        ],
        default_endpoint_id="partita",
    ),

    FormGroup(
        id="workflow",
        name="Workflow",
        description="Run a multi-phase investigation workflow with preset configurations.",
        icon="fa-project-diagram",
        color="dark",
        category="advanced",
        params=[
            EndpointParam(
                name="preset", label="Preset", placeholder="Select workflow",
                input_type="select", required=True,
                options=[
                    ("due-diligence", "Due Diligence — search → intestati → ispezioni"),
                    ("patrimonio", "Asset Investigation — soggetto → drill-down"),
                    ("fondiario", "Land Survey — elenco → mappa → fiduciali → originali"),
                    ("aziendale", "Corporate Audit — azienda → drill-down"),
                    ("storico", "Parcel History — search → intestati → nota → ispezioni"),
                ],
                help_text="Select a predefined workflow",
            ),
            _PROVINCIA, _COMUNE,
            EndpointParam(name="foglio", label="Sheet", placeholder="Required for property presets", required=False),
            EndpointParam(name="particella", label="Parcel", placeholder="Required for property presets", required=False),
            EndpointParam(name="codice_fiscale", label="Codice Fiscale", placeholder="For patrimonio preset", required=False),
            EndpointParam(name="identificativo", label="P.IVA / Company", placeholder="For aziendale preset", required=False),
        ],
        endpoints=[
            EndpointOption(
                id="workflow", name="Run Workflow",
                path="/visura/workflow", method="POST",
                description="Execute a multi-phase investigation workflow",
            ),
        ],
        default_endpoint_id="workflow",
        available=True,
    ),

    FormGroup(
        id="batch",
        name="Batch Upload",
        description="Submit multiple queries from CSV. Paste CSV rows below — one query per line.",
        icon="fa-file-csv",
        color="dark",
        category="advanced",
        params=[
            EndpointParam(
                name="command", label="Query Type", placeholder="Select type",
                input_type="select", required=True,
                options=[
                    ("search", "Property Search"),
                    ("intestati", "Owner Lookup"),
                    ("soggetto", "Person Search (CF)"),
                    ("persona-giuridica", "Company Search (P.IVA)"),
                    ("elenco-immobili", "Property List"),
                    ("indirizzo", "Address Search"),
                    ("partita", "Partita Search"),
                    ("workflow-due-diligence", "Workflow: Due Diligence"),
                    ("workflow-patrimonio", "Workflow: Asset Investigation"),
                    ("workflow-fondiario", "Workflow: Land Survey"),
                    ("workflow-aziendale", "Workflow: Corporate Audit"),
                    ("workflow-storico", "Workflow: Parcel History"),
                ],
                help_text="All rows will use this query type",
            ),
            EndpointParam(
                name="csv_data", label="CSV Data", placeholder="provincia,comune,foglio,particella,tipo_catasto\nRoma,ROMA,100,50,T\nTrieste,TRIESTE,9,166,F",
                input_type="textarea", required=True,
                help_text="First line = header, subsequent lines = data rows. Required columns depend on the query type.",
            ),
        ],
        endpoints=[
            EndpointOption(
                id="batch", name="Batch Submit",
                path="/web/api/batch", method="POST",
                description="Submit all rows as queued requests",
            ),
        ],
        default_endpoint_id="batch",
    ),
]


def get_available_form_groups() -> list[FormGroup]:
    """Return form groups that are available."""
    return [fg for fg in FORM_GROUPS if fg.available]


def get_form_group_by_id(group_id: str) -> Optional[FormGroup]:
    """Find a form group by ID."""
    return next((fg for fg in FORM_GROUPS if fg.id == group_id), None)


def get_endpoint_by_id(endpoint_id: str) -> Optional[EndpointOption]:
    """Find an endpoint across all form groups."""
    for fg in FORM_GROUPS:
        for ep in fg.endpoints:
            if ep.id == endpoint_id:
                return ep
    return None
