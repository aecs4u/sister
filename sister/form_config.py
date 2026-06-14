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

WORKFLOW_GROUPS: list[FormGroup] = [
    FormGroup(
        id="wf-due-diligence",
        name="Due Diligence",
        description="Real estate due diligence: search → intestati → ispezioni → elaborato planimetrico. Optional: owner expansion, ipotecaria.",
        icon="fa-file-contract",
        color="primary",
        category="workflow",
        flowchart="due-diligence",
        params=[
            _PRESET_HIDDEN("due-diligence"),
            _PROVINCIA,
            _COMUNE,
            _FOGLIO,
            _PARTICELLA,
            _TIPO_CATASTO,
            _SEZIONE,
            _SEZIONE_URBANA,
            _WORKFLOW_DEPTH,
            _WORKFLOW_PAID,
            _WORKFLOW_CONFIRM,
        ],
        endpoints=[
            EndpointOption(
                id="workflow-due-diligence",
                name="Due Diligence",
                path="/visura/workflow",
                method="POST",
                description="search → intestati → ispezioni → elaborato planimetrico",
            )
        ],
        default_endpoint_id="workflow-due-diligence",
    ),
    FormGroup(
        id="wf-patrimonio",
        name="Asset Investigation",
        description="Asset investigation: soggetto → drill-down intestati → address lookup. Optional: owner expansion, ipotecaria.",
        icon="fa-search-dollar",
        color="info",
        category="workflow",
        flowchart="patrimonio",
        params=[
            _PRESET_HIDDEN("patrimonio"),
            EndpointParam(
                name="codice_fiscale",
                label="Codice Fiscale",
                placeholder="e.g. RSSMRI85E28H501E",
                example="RSSMRI85E28H501E",
            ),
            _TIPO_CATASTO_TFE,
            _PROVINCIA_OPT,
            _WORKFLOW_DEPTH,
            _WORKFLOW_PAID,
            _WORKFLOW_CONFIRM,
        ],
        endpoints=[
            EndpointOption(
                id="workflow-patrimonio",
                name="Asset Investigation",
                path="/visura/workflow",
                method="POST",
                description="soggetto → drill-down intestati per property",
            )
        ],
        default_endpoint_id="workflow-patrimonio",
    ),
    FormGroup(
        id="wf-fondiario",
        name="Land Survey",
        description="Land survey: elenco → mappa → export mappa → fiduciali → originali → elaborato planimetrico.",
        icon="fa-mountain",
        color="success",
        category="workflow",
        flowchart="fondiario",
        params=[_PRESET_HIDDEN("fondiario"), _PROVINCIA, _COMUNE, _FOGLIO_OPT, _TIPO_CATASTO_TF, _WORKFLOW_DEPTH],
        endpoints=[
            EndpointOption(
                id="workflow-fondiario",
                name="Land Survey",
                path="/visura/workflow",
                method="POST",
                description="elenco → mappa → export mappa → fiduciali → originali → elaborato",
            )
        ],
        default_endpoint_id="workflow-fondiario",
    ),
    FormGroup(
        id="wf-aziendale",
        name="Corporate Audit",
        description="Corporate audit: azienda → drill-down intestati → address lookup. Optional: owner expansion, ipotecaria.",
        icon="fa-briefcase",
        color="warning",
        category="workflow",
        flowchart="aziendale",
        params=[
            _PRESET_HIDDEN("aziendale"),
            EndpointParam(
                name="identificativo", label="P.IVA / Company", placeholder="e.g. 02471840997", example="02471840997"
            ),
            _TIPO_CATASTO_TFE,
            _PROVINCIA_OPT,
            _WORKFLOW_DEPTH,
            _WORKFLOW_PAID,
            _WORKFLOW_CONFIRM,
        ],
        endpoints=[
            EndpointOption(
                id="workflow-aziendale",
                name="Corporate Audit",
                path="/visura/workflow",
                method="POST",
                description="azienda → drill-down intestati per property",
            )
        ],
        default_endpoint_id="workflow-aziendale",
    ),
    FormGroup(
        id="wf-storico",
        name="Parcel History",
        description="Parcel history: search → intestati → nota → ispezioni → originali → elaborato planimetrico. Optional: owner expansion, ipotecaria.",
        icon="fa-history",
        color="dark",
        category="workflow",
        flowchart="storico",
        params=[
            _PRESET_HIDDEN("storico"),
            _PROVINCIA,
            _COMUNE,
            _FOGLIO,
            _PARTICELLA,
            _TIPO_CATASTO,
            _WORKFLOW_DEPTH,
            _WORKFLOW_PAID,
            _WORKFLOW_CONFIRM,
        ],
        endpoints=[
            EndpointOption(
                id="workflow-storico",
                name="Parcel History",
                path="/visura/workflow",
                method="POST",
                description="search → intestati → nota → ispezioni → originali → elaborato",
            )
        ],
        default_endpoint_id="workflow-storico",
    ),
    FormGroup(
        id="wf-indirizzo",
        name="Address Lookup",
        description="Address lookup: indirizzo → search → intestati. Optional: owner expansion.",
        icon="fa-map-marker-alt",
        color="danger",
        category="workflow",
        flowchart="indirizzo",
        params=[
            _PRESET_HIDDEN("indirizzo"),
            _PROVINCIA,
            _COMUNE,
            EndpointParam(
                name="indirizzo",
                label="Address",
                placeholder="e.g. VIA ROMA",
                help_text="Street name (partial match supported)",
                example="VIA ROMA",
            ),
            _TIPO_CATASTO,
            _WORKFLOW_DEPTH,
        ],
        endpoints=[
            EndpointOption(
                id="workflow-indirizzo",
                name="Address Lookup",
                path="/visura/workflow",
                method="POST",
                description="indirizzo → search → intestati",
            )
        ],
        default_endpoint_id="workflow-indirizzo",
    ),
    FormGroup(
        id="wf-cross-reference",
        name="Cross-Reference",
        description="Cross-reference: compare person + company property overlap.",
        icon="fa-exchange-alt",
        color="dark",
        category="workflow",
        flowchart="cross-reference",
        params=[
            _PRESET_HIDDEN("cross-reference"),
            EndpointParam(
                name="codice_fiscale",
                label="Codice Fiscale",
                placeholder="e.g. RSSMRI85E28H501E",
                example="RSSMRI85E28H501E",
            ),
            EndpointParam(
                name="identificativo", label="P.IVA / Company", placeholder="e.g. 02471840997", example="02471840997"
            ),
            _TIPO_CATASTO_TFE,
            _PROVINCIA_OPT,
            _WORKFLOW_DEPTH,
        ],
        endpoints=[
            EndpointOption(
                id="workflow-cross-reference",
                name="Cross-Reference",
                path="/visura/workflow",
                method="POST",
                description="soggetto + azienda → cross-property overlap",
            )
        ],
        default_endpoint_id="workflow-cross-reference",
    ),
    # --- Multi-hop (full depth) presets ---
    FormGroup(
        id="wf-full-due-diligence",
        name="Full Due Diligence",
        description="Multi-hop: seed parcel → owners → portfolios → ranked history → encumbrances → risk scoring.",
        icon="fa-project-diagram",
        color="primary",
        category="workflow",
        flowchart="full-due-diligence",
        params=[
            _PRESET_HIDDEN("full-due-diligence"),
            _PROVINCIA,
            _COMUNE,
            _FOGLIO,
            _PARTICELLA,
            _TIPO_CATASTO,
            _SEZIONE,
            _SEZIONE_URBANA,
            _WORKFLOW_DEPTH,
            _WORKFLOW_PAID,
            _WORKFLOW_CONFIRM,
            EndpointParam(
                name="max_paid_steps",
                label="Max Paid Steps",
                placeholder="3",
                input_type="text",
                required=False,
                help_text="Maximum number of paid ispezione ipotecaria invocations (default: 3)",
            ),
        ],
        endpoints=[
            EndpointOption(
                id="workflow-full-due-diligence",
                name="Full Due Diligence",
                path="/visura/workflow",
                method="POST",
                description="seed → owners → portfolios → history → encumbrances → risk",
            )
        ],
        default_endpoint_id="workflow-full-due-diligence",
    ),
    FormGroup(
        id="wf-full-patrimonio",
        name="Full Portfolio Investigation",
        description="Multi-hop: soggetto → drill-down → owners → portfolios → history → encumbrances → risk scoring.",
        icon="fa-project-diagram",
        color="info",
        category="workflow",
        flowchart="full-patrimonio",
        params=[
            _PRESET_HIDDEN("full-patrimonio"),
            EndpointParam(
                name="codice_fiscale",
                label="Codice Fiscale",
                placeholder="e.g. RSSMRI85E28H501E",
                example="RSSMRI85E28H501E",
            ),
            _TIPO_CATASTO_TFE,
            _PROVINCIA_OPT,
            _WORKFLOW_DEPTH,
            _WORKFLOW_PAID,
            _WORKFLOW_CONFIRM,
            EndpointParam(
                name="max_paid_steps",
                label="Max Paid Steps",
                placeholder="3",
                input_type="text",
                required=False,
                help_text="Maximum number of paid ispezione ipotecaria invocations (default: 3)",
            ),
        ],
        endpoints=[
            EndpointOption(
                id="workflow-full-patrimonio",
                name="Full Portfolio Investigation",
                path="/visura/workflow",
                method="POST",
                description="soggetto → drill → owners → portfolios → history → risk",
            )
        ],
        default_endpoint_id="workflow-full-patrimonio",
    ),
    FormGroup(
        id="wf-full-aziendale",
        name="Full Corporate Audit",
        description="Multi-hop: azienda → drill-down → owners → portfolios → history → encumbrances → risk scoring.",
        icon="fa-project-diagram",
        color="warning",
        category="workflow",
        flowchart="full-aziendale",
        params=[
            _PRESET_HIDDEN("full-aziendale"),
            EndpointParam(
                name="identificativo", label="P.IVA / Company", placeholder="e.g. 02471840997", example="02471840997"
            ),
            _TIPO_CATASTO_TFE,
            _PROVINCIA_OPT,
            _WORKFLOW_DEPTH,
            _WORKFLOW_PAID,
            _WORKFLOW_CONFIRM,
            EndpointParam(
                name="max_paid_steps",
                label="Max Paid Steps",
                placeholder="3",
                input_type="text",
                required=False,
                help_text="Maximum number of paid ispezione ipotecaria invocations (default: 3)",
            ),
        ],
        endpoints=[
            EndpointOption(
                id="workflow-full-aziendale",
                name="Full Corporate Audit",
                path="/visura/workflow",
                method="POST",
                description="azienda → drill → owners → portfolios → history → risk",
            )
        ],
        default_endpoint_id="workflow-full-aziendale",
    ),
]


# ---------------------------------------------------------------------------
# Combined list
# ---------------------------------------------------------------------------

FORM_GROUPS: list[FormGroup] = SINGLE_STEP_GROUPS + WORKFLOW_GROUPS


def get_available_form_groups() -> list[FormGroup]:
    """Return form groups that are available."""
    return [fg for fg in FORM_GROUPS if fg.available]


def get_single_step_groups() -> list[FormGroup]:
    """Return single-step form groups."""
    return [fg for fg in FORM_GROUPS if fg.available and fg.category == "single"]


def get_workflow_groups() -> list[FormGroup]:
    """Return workflow (multi-step) form groups."""
    return [fg for fg in FORM_GROUPS if fg.available and fg.category == "workflow"]


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
