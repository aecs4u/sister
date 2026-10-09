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
    label="Tipo catasto",
    placeholder="Seleziona il tipo",
    input_type="select",
    required=False,
    options=[("", "Both (T+F)"), ("T", "Terreni (T)"), ("F", "Fabbricati (F)")],
    help_text="T = Terreni, F = Fabbricati. Lascia vuoto per entrambi.",
)

_TIPO_CATASTO_TF = EndpointParam(
    name="tipo_catasto",
    label="Tipo catasto",
    placeholder="Seleziona il tipo",
    input_type="select",
    required=False,
    options=[("T", "Terreni (T)"), ("F", "Fabbricati (F)")],
)

_TIPO_CATASTO_TFE = EndpointParam(
    name="tipo_catasto",
    label="Tipo catasto",
    placeholder="Seleziona il tipo",
    input_type="select",
    required=False,
    options=[("", "Both (E)"), ("T", "Terreni (T)"), ("F", "Fabbricati (F)")],
)

_PROVINCIA = EndpointParam(
    name="provincia",
    label="Provincia",
    placeholder="es. Roma",
    help_text="Nome della provincia",
    example="Roma",
)

_COMUNE = EndpointParam(
    name="comune",
    label="Comune",
    placeholder="es. ROMA",
    help_text="Nome del comune (maiuscolo)",
    example="ROMA",
)

_FOGLIO = EndpointParam(
    name="foglio",
    label="Foglio",
    placeholder="es. 100",
    example="100",
)

_PARTICELLA = EndpointParam(
    name="particella",
    label="Particella",
    placeholder="es. 50",
    example="50",
)

_SEZIONE = EndpointParam(
    name="sezione",
    label="Sezione",
    placeholder="es. A, B, C",
    required=False,
    help_text=(
        "Codice sezione censuaria per i comuni con più sezioni (es. A = RAVENNA, C = SAVIO). "
        "Lascia vuoto se non applicabile."
    ),
)

_SEZIONE_URBANA = EndpointParam(
    name="sezione_urbana",
    label="Sezione urbana",
    placeholder="es. RA",
    required=False,
    help_text="Codice sezione urbana di 2-3 caratteri per i Fabbricati (es. RA, PA)",
)

_SUBALTERNO = EndpointParam(
    name="subalterno",
    label="Subalterno",
    placeholder="es. 3",
    required=False,
    help_text="Obbligatorio per gli intestati dei Fabbricati",
)

_PROVINCIA_OPT = EndpointParam(
    name="provincia",
    label="Provincia",
    placeholder="Lascia vuoto per la ricerca nazionale",
    required=False,
    help_text="Ometti per la ricerca nazionale",
)

_FOGLIO_OPT = EndpointParam(
    name="foglio",
    label="Foglio",
    placeholder="Obbligatorio per i preset immobiliari",
    required=False,
)

_PARTICELLA_OPT = EndpointParam(
    name="particella",
    label="Particella",
    placeholder="Obbligatorio per i preset immobiliari",
    required=False,
)


# ---------------------------------------------------------------------------
# Single-step form groups
# ---------------------------------------------------------------------------

SINGLE_STEP_GROUPS: list[FormGroup] = [
    FormGroup(
        id="property-search",
        name="Ricerca immobili",
        description="Cerca gli immobili per dati catastali (foglio + particella).",
        icon="fa-search",
        color="primary",
        category="single",
        params=[_TIPO_CATASTO, _PROVINCIA, _COMUNE, _SEZIONE, _SEZIONE_URBANA, _FOGLIO, _PARTICELLA, _SUBALTERNO],
        endpoints=[
            EndpointOption(
                id="visura",
                name="Dati immobile",
                path="/visura",
                method="POST",
                description="Trova tutti gli immobili di una particella (Fase 1)",
            ),
            EndpointOption(
                id="intestati",
                name="Ricerca intestatari",
                path="/visura/intestati",
                method="POST",
                description="Trova gli intestatari di un immobile specifico (Fase 2)",
            ),
        ],
        default_endpoint_id="visura",
    ),
    FormGroup(
        id="person-search",
        name="Ricerca persona",
        description="Ricerca nazionale per codice fiscale.",
        icon="fa-user",
        color="info",
        category="single",
        params=[
            EndpointParam(
                name="codice_fiscale",
                label="Codice Fiscale",
                placeholder="es. RSSMRI85E28H501E",
                help_text="Codice fiscale di 16 caratteri",
                example="RSSMRI85E28H501E",
            ),
            _TIPO_CATASTO_TFE,
            _PROVINCIA_OPT,
        ],
        endpoints=[
            EndpointOption(
                id="soggetto",
                name="Ricerca persona",
                path="/visura/soggetto",
                method="POST",
                description="Ricerca nazionale per codice fiscale",
            ),
        ],
        default_endpoint_id="soggetto",
    ),
    FormGroup(
        id="company-search",
        name="Ricerca società",
        description="Cerca per P.IVA o denominazione.",
        icon="fa-building",
        color="warning",
        category="single",
        params=[
            EndpointParam(
                name="identificativo",
                label="P.IVA o denominazione società",
                placeholder="es. 02471840997",
                help_text="Inserisci la P.IVA di 11 cifre o la denominazione della società",
                example="02471840997",
            ),
            _TIPO_CATASTO_TFE,
            _PROVINCIA_OPT,
        ],
        endpoints=[
            EndpointOption(
                id="persona-giuridica",
                name="Ricerca società",
                path="/visura/persona-giuridica",
                method="POST",
                description="Cerca per P.IVA o denominazione",
            ),
        ],
        default_endpoint_id="persona-giuridica",
    ),
    FormGroup(
        id="property-list",
        name="Elenco immobili",
        description="Elenca tutti gli immobili di un comune.",
        icon="fa-list",
        color="success",
        category="single",
        params=[
            _PROVINCIA,
            _COMUNE,
            _TIPO_CATASTO_TF,
            EndpointParam(
                name="foglio",
                label="Foglio",
                placeholder="Facoltativo — filtra per foglio",
                required=False,
            ),
            _SEZIONE,
        ],
        endpoints=[
            EndpointOption(
                id="elenco-immobili",
                name="Elenco immobili",
                path="/visura/elenco-immobili",
                method="POST",
                description="Elenca tutti gli immobili di un comune",
            ),
        ],
        default_endpoint_id="elenco-immobili",
    ),
    FormGroup(
        id="address-search",
        name="Ricerca per indirizzo",
        description="Cerca gli immobili per indirizzo.",
        icon="fa-map-marker-alt",
        color="danger",
        category="single",
        params=[
            _PROVINCIA,
            _COMUNE,
            _TIPO_CATASTO_TF,
            EndpointParam(
                name="indirizzo",
                label="Indirizzo",
                placeholder="es. VIA ROMA",
                help_text="Nome della via (ricerca parziale)",
                example="VIA ROMA",
            ),
        ],
        endpoints=[
            EndpointOption(
                id="indirizzo",
                name="Ricerca per indirizzo",
                path="/visura/indirizzo",
                method="POST",
                description="Trova gli immobili a un indirizzo",
            ),
        ],
        default_endpoint_id="indirizzo",
    ),
    FormGroup(
        id="nota-search",
        name="Ricerca nota",
        description="Cerca le formalità di una nota (ricerca per nota).",
        icon="fa-file-signature",
        color="secondary",
        category="single",
        params=[],  # generated from the CLI command, see generate_single_step_params()
        endpoints=[
            EndpointOption(
                id="nota",
                name="Ricerca nota",
                path="/visura/nota",
                method="POST",
                description="Trova gli immobili per numero e anno della nota",
            ),
        ],
        default_endpoint_id="nota",
    ),
    FormGroup(
        id="partita-search",
        name="Ricerca partita",
        description="Cerca per numero di partita catastale.",
        icon="fa-hashtag",
        color="secondary",
        category="single",
        params=[
            _PROVINCIA,
            _COMUNE,
            _TIPO_CATASTO_TF,
            EndpointParam(
                name="partita",
                label="Numero di partita",
                placeholder="es. 12345",
                help_text="Numero di partita catastale",
            ),
        ],
        endpoints=[
            EndpointOption(
                id="partita",
                name="Ricerca partita",
                path="/visura/partita",
                method="POST",
                description="Cerca per numero di partita catastale",
            ),
        ],
        default_endpoint_id="partita",
    ),
    FormGroup(
        id="mappa",
        name="Mappa catastale",
        description="Visualizza i dati della mappa catastale di un foglio.",
        icon="fa-map",
        color="dark",
        category="single",
        params=[
            _PROVINCIA,
            _COMUNE,
            _FOGLIO,
            EndpointParam(
                name="particella",
                label="Particella (facoltativa)",
                placeholder="es. 50",
                required=False,
            ),
            _SEZIONE,
        ],
        endpoints=[
            EndpointOption(
                id="mappa",
                name="Visualizza mappa",
                path="/visura/mappa",
                method="POST",
                description="Visualizza i dati della mappa catastale (EM)",
            ),
            EndpointOption(
                id="export-mappa",
                name="Esporta mappa",
                path="/visura/export-mappa",
                method="POST",
                description="Esporta i dati della mappa catastale (EXPM)",
            ),
        ],
        default_endpoint_id="mappa",
    ),
    FormGroup(
        id="elaborato-planimetrico",
        name="Elaborato planimetrico",
        description="Recupera l'elaborato planimetrico di un immobile.",
        icon="fa-drafting-compass",
        color="dark",
        category="single",
        params=[_PROVINCIA, _COMUNE, _FOGLIO_OPT],
        endpoints=[
            EndpointOption(
                id="elaborato-planimetrico",
                name="Elaborato planimetrico",
                path="/visura/elaborato-planimetrico",
                method="POST",
                description="Elaborato planimetrico (ELPL)",
            ),
        ],
        default_endpoint_id="elaborato-planimetrico",
    ),
    FormGroup(
        id="originali-impianto",
        name="Originali di impianto",
        description="Originali di impianto e punti fiduciali.",
        icon="fa-archive",
        color="dark",
        category="single",
        params=[_PROVINCIA, _COMUNE, _TIPO_CATASTO_TF, _FOGLIO_OPT],
        endpoints=[
            EndpointOption(
                id="originali",
                name="Originali di impianto",
                path="/visura/originali",
                method="POST",
                description="Originali di impianto (OOII)",
            ),
            EndpointOption(
                id="fiduciali",
                name="Punti fiduciali",
                path="/visura/fiduciali",
                method="POST",
                description="Punti fiduciali (FID)",
            ),
        ],
        default_endpoint_id="originali",
    ),
    FormGroup(
        id="ispezioni",
        name="Ispezioni",
        description="Ispezioni immobiliari (digitali e cartacee).",
        icon="fa-clipboard-check",
        color="dark",
        category="single",
        params=[_PROVINCIA, _COMUNE, _TIPO_CATASTO_TF, _FOGLIO_OPT, _PARTICELLA_OPT],
        endpoints=[
            EndpointOption(
                id="ispezioni",
                name="Ispezioni digitali",
                path="/visura/ispezioni",
                method="POST",
                description="Ispezioni immobiliari (ISP)",
            ),
            EndpointOption(
                id="ispezioni-cartacee",
                name="Ispezioni cartacee",
                path="/visura/ispezioni-cartacee",
                method="POST",
                description="Ispezioni cartacee (ISPCART)",
            ),
        ],
        default_endpoint_id="ispezioni",
    ),
    FormGroup(
        id="ispezione-ipotecaria",
        name="Ispezione ipotecaria",
        description=(
            "Ispezione immobiliare a pagamento (per immobile, persona, società o nota). "
            "Ogni richiesta ha un costo."
        ),
        icon="fa-file-invoice-dollar",
        color="danger",
        category="single",
        params=[
            EndpointParam(
                name="tipo_ricerca",
                label="Tipo di ricerca",
                placeholder="Seleziona il tipo di ricerca",
                input_type="select",
                required=True,
                options=[
                    ("immobile", "Property (Immobile)"),
                    ("persona_fisica", "Person (Persona Fisica)"),
                    ("persona_giuridica", "Company (Persona Giuridica)"),
                    ("nota", "Note (Nota)"),
                ],
                help_text="Tipo di ricerca ispezione",
            ),
            _PROVINCIA,
            EndpointParam(
                name="comune",
                label="Comune",
                placeholder="es. ROMA",
                required=False,
                help_text="Nome del comune (per la ricerca immobile)",
            ),
            _TIPO_CATASTO_TF,
            EndpointParam(
                name="codice_fiscale",
                label="Codice Fiscale",
                placeholder="es. RSSMRI85E28H501E",
                required=False,
                help_text="Per la ricerca persona fisica",
            ),
            EndpointParam(
                name="identificativo",
                label="P.IVA / società",
                placeholder="es. 02471840997",
                required=False,
                help_text="Per la ricerca persona giuridica",
            ),
            EndpointParam(
                name="foglio",
                label="Foglio",
                placeholder="es. 100",
                required=False,
                help_text="Per la ricerca immobile",
            ),
            EndpointParam(
                name="particella",
                label="Particella",
                placeholder="es. 50",
                required=False,
                help_text="Per la ricerca immobile",
            ),
            EndpointParam(
                name="numero_nota",
                label="Numero nota",
                placeholder="es. 12345",
                required=False,
                help_text="Per la ricerca nota",
            ),
            EndpointParam(
                name="anno_nota",
                label="Anno nota",
                placeholder="es. 2024",
                required=False,
                help_text="Per la ricerca nota",
            ),
            EndpointParam(
                name="auto_confirm",
                label="Conferma automatica del costo",
                placeholder="",
                input_type="select",
                required=False,
                options=[("false", "No — mostra prima il costo"), ("true", "Sì — approva automaticamente")],
                help_text="ATTENZIONE: con «Sì» il costo viene confermato automaticamente e addebitato sul tuo conto.",
            ),
        ],
        endpoints=[
            EndpointOption(
                id="ispezione-ipotecaria",
                name="Ispezione ipotecaria",
                path="/visura/ispezione-ipotecaria",
                method="POST",
                description="Ispezione immobiliare a pagamento (richiede la conferma del costo)",
            ),
        ],
        default_endpoint_id="ispezione-ipotecaria",
    ),
    FormGroup(
        id="riepilogo",
        name="Riepilogo query",
        description="Consulta lo storico delle query SISTER e le richieste in sospeso.",
        icon="fa-history",
        color="secondary",
        category="single",
        params=[],  # No parameters needed
        endpoints=[
            EndpointOption(
                id="riepilogo-visure",
                name="Riepilogo query",
                path="/visura/riepilogo-visure",
                method="POST",
                description="Storico delle query SISTER (Riepilogo visure)",
            ),
            EndpointOption(
                id="richieste",
                name="Richieste in sospeso",
                path="/visura/richieste",
                method="POST",
                description="Richieste SISTER in sospeso o completate",
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
    label="Profondità",
    placeholder="Seleziona la profondità",
    input_type="select",
    required=False,
    options=[
        ("light", "Light — core steps only"),
        ("standard", "Standard — con arricchimento"),
        ("deep", "Deep — owner expansion + paid"),
        ("full", "Full — multi-hop graph expansion"),
    ],
    help_text=(
        "Controlla quali passaggi vengono eseguiti. "
        "«Full» aggiunge l'analisi del patrimonio, i pacchetti storici e le verifiche ipotecarie a pagamento."
    ),
)

_WORKFLOW_PAID = EndpointParam(
    name="include_paid_steps",
    label="Includi i passaggi a pagamento",
    placeholder="",
    input_type="select",
    required=False,
    options=[("false", "No"), ("true", "Yes — include paid inspections")],
    help_text="Abilita i passaggi a pagamento di ispezione ipotecaria (richiede profondità Deep o Full).",
)

_WORKFLOW_CONFIRM = EndpointParam(
    name="auto_confirm",
    label="Conferma automatica del costo",
    placeholder="",
    input_type="select",
    required=False,
    options=[("false", "No — mostra prima il costo"), ("true", "Sì — approva automaticamente")],
    help_text="ATTENZIONE: con «Sì» i costi dei servizi a pagamento vengono confermati automaticamente.",
)

_WORKFLOW_CODICE_FISCALE = EndpointParam(
    name="codice_fiscale",
    label="Codice Fiscale",
    placeholder="es. RSSMRI85E28H501E",
    example="RSSMRI85E28H501E",
)

_WORKFLOW_IDENTIFICATIVO = EndpointParam(
    name="identificativo",
    label="P.IVA / società",
    placeholder="es. 02471840997",
    example="02471840997",
)

_WORKFLOW_ADDRESS = EndpointParam(
    name="indirizzo",
    label="Indirizzo",
    placeholder="es. VIA ROMA",
    help_text="Nome della via (ricerca parziale)",
    example="VIA ROMA",
)

_WORKFLOW_MAX_PAID_STEPS = EndpointParam(
    name="max_paid_steps",
    label="Passaggi a pagamento (max)",
    placeholder="3",
    input_type="text",
    required=False,
    help_text="Numero massimo di ispezioni ipotecarie a pagamento (predefinito: 3)",
)

_WORKFLOW_HISTORY = EndpointParam(
    name="include_history",
    label="Includi i dati storici",
    placeholder="",
    input_type="select",
    required=False,
    options=[("false", "No"), ("true", "Sì — includi note e ispezioni cartacee")],
)

_WORKFLOW_NUMERO_NOTA = EndpointParam(
    name="numero_nota",
    label="Numero nota",
    placeholder="Facoltativo; usato dalla ricerca storica delle note",
    required=False,
)

_WORKFLOW_PORTFOLIO_CF = EndpointParam(
    name="codice_fiscale",
    label="Codice fiscale della persona",
    placeholder="es. RSSMRI85E28H501E",
    required=False,
    example="RSSMRI85E28H501E",
)

_WORKFLOW_PORTFOLIO_COMPANY = EndpointParam(
    name="identificativo",
    label="P.IVA / denominazione società",
    placeholder="es. 02471840997",
    required=False,
    example="02471840997",
)

_WORKFLOW_PROPERTY_PARAMS = (_PROVINCIA, _COMUNE, _FOGLIO, _PARTICELLA, _TIPO_CATASTO)
_WORKFLOW_ENTITY_PARAMS = (_TIPO_CATASTO_TFE, _PROVINCIA_OPT)
_WORKFLOW_DEPTH_PARAMS = (_WORKFLOW_DEPTH,)
_WORKFLOW_PAID_PARAMS = (_WORKFLOW_DEPTH, _WORKFLOW_PAID, _WORKFLOW_CONFIRM)

# Italian descriptions for the presets that have none of their own (the package texts are English).
_WORKFLOW_DESCRIPTIONS_IT = {
    "fondiario": (
        "Indagine fondiaria: elenco → mappa → export → fiduciali → originali → elaborato → punteggio di rischio"
    ),
    "indirizzo": (
        "Ricerca per indirizzo: indirizzo → ricerca → intestati → espansione intestatari → punteggio di rischio"
    ),
    "cross-reference": "Incrocio dati: soggetto + società → immobili in comune → punteggio di rischio",
}

# Preset execution and step descriptions come from aecs4u_workflow.models.
# This table contains only the web-specific labels, styling, and input fields.
_WORKFLOW_FORM_SPECS = {
    "due-diligence": {
        "name": "Due Diligence",
        "icon": "fa-file-contract",
        "color": "primary",
        "description": (
            "Indagine su una particella. La profondità sceglie tra verifiche standard e multi-hop complete; "
            "lo storico si può aggiungere se serve."
        ),
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
        "name": "Indagine patrimoniale",
        "icon": "fa-search-dollar",
        "color": "info",
        "description": (
            "Cerca gli immobili di una persona o di una società, poi espande intestatari e patrimoni "
            "in base alla profondità."
        ),
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
        "name": "Indagine fondiaria",
        "icon": "fa-mountain",
        "color": "success",
        "params": (_PROVINCIA, _COMUNE, _FOGLIO_OPT, _TIPO_CATASTO_TF, *_WORKFLOW_DEPTH_PARAMS),
    },
    "indirizzo": {
        "name": "Ricerca per indirizzo",
        "icon": "fa-map-marker-alt",
        "color": "danger",
        "params": (_PROVINCIA, _COMUNE, _WORKFLOW_ADDRESS, _TIPO_CATASTO, *_WORKFLOW_DEPTH_PARAMS),
    },
    "cross-reference": {
        "name": "Incrocio dati",
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
    description = (
        spec.get("description") or _WORKFLOW_DESCRIPTIONS_IT.get(preset) or WORKFLOW_PRESETS[preset]["description"]
    )
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
