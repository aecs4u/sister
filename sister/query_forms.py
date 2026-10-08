"""Single source of truth for the inputs of every single-step SISTER query form.

Each :class:`FormField` ties one input element of the real SISTER form (its ``name`` attribute) to the
parameter exposed by the CLI (``sister query <command> --<param>``), the API and the ``/web/forms`` page.
The CLI options, the web form parameters and the browser form-filling are all generated from this table,
and ``tests/test_query_forms.py`` checks it against saved copies of the portal forms
(``tests/fixtures/portal_forms/*.html``), so a form that changes on the portal shows up as a failing test.

``handled=True`` marks the parameters that the dedicated code of the query (``sister/utils.py``) already
fills in; every other parameter is filled by the generic form filler (``_apply_form_fields``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "portal_forms"


@dataclass(frozen=True)
class FormField:
    """One input element of a SISTER form."""

    portal: str | tuple[str, ...]  # ``name`` attribute(s) of the element (the form variants differ)
    param: str  # CLI/API/web parameter
    kind: str = "text"  # text | select | radio | checkbox | date
    help: str = ""
    choices: dict[str, str] = field(default_factory=dict)  # parameter value -> portal value (radio/select)
    handled: bool = False  # filled by dedicated code in the run_* function
    page: int = 1  # form page of a multi-page query (the saved fixture holds page 1)
    requires: tuple[str, str] | None = None  # (mode parameter, mode value): the input only exists in that mode

    @property
    def portal_names(self) -> tuple[str, ...]:
        return (self.portal,) if isinstance(self.portal, str) else self.portal


@dataclass(frozen=True)
class QueryForm:
    """One single-step query: its inputs and how it is submitted (CLI command, API path, client method)."""

    command: str
    fixture: str | None  # file name (without .html) in tests/fixtures/portal_forms; None = no portal form
    fields: tuple[FormField, ...]
    # parameters of the command that are not inputs of the form (office, legacy filters)
    extra_params: tuple[str, ...] = ("provincia",)
    summary: str = ""  # help text of the CLI command
    path: str = ""  # API path
    method: str = "generic_search"  # VisuraClient method; generic_search = POST /visura/{search_type}
    search_type: str = ""  # route name of a generic query
    dedicated: tuple[str, ...] = ()  # parameters that are explicit arguments of the client method
    required: tuple[str, ...] = ()
    required_any: tuple[tuple[str, ...], ...] = ()  # at least one of each group (e.g. codice_fiscale | cognome)


def _f(portal, param, kind="text", help="", choices=None, handled=False, page=1) -> FormField:
    return FormField(portal, param, kind, help, choices or {}, handled, page)


# --- shared fields --------------------------------------------------------------------------------------

_RICHIEDENTE = _f("richiedente", "richiedente", help="Richiesta effettuata per conto di (default: ADE_USERNAME)")
_MOTIVO = _f("motivoText", "motivo", help="Motivazione della richiesta (default: Esplorazione)")
_TIPO_CATASTO = _f("tipoCatasto", "tipo_catasto", "select", "T = Terreni, F = Fabbricati, E = entrambi", handled=True)
_COMUNE_CAT = _f("comuneCat", "comune", "select", "Comune", handled=True)
_COMUNE_DENOM = _f("denomComune", "comune", "select", "Comune", handled=True)
_SEZIONE = _f(
    "sezione", "sezione", "select", "Sezione (obbligatoria per i comuni con sezioni, es. Ravenna)", handled=True
)
# for the queries whose code does not select the sezione itself
_SEZIONE_AUTO = _f("sezione", "sezione", "select", "Sezione (obbligatoria per i comuni con sezioni, es. Ravenna)")
_SEZIONE_URBANA = _f("sezUrb", "sezione_urbana", help="Sezione urbana")
_FOGLIO = _f("foglio", "foglio", help="Foglio")
_RESTRIZIONE_FOGLIO = _f(
    "abilitaRestrizioniFoglio", "restrizione_foglio", "checkbox", "Abilita la restrizione per sezione/foglio"
)

_TIPO_DENUNCIA_CHOICES = {"assente": "A", "protocollo": "p", "scheda": "s", "variazione": "v"}
_DEFINITIVO_PROVVISORIO = {"definitivo": "d", "provvisorio": "p"}

# --- one form per command -------------------------------------------------------------------------------

_IMMOBILE = (
    _TIPO_CATASTO,
    _COMUNE_DENOM,
    _SEZIONE,
    _f("tipoIdentificativo", "identificativo_tipo", "radio", "definitivo | provvisorio", _DEFINITIVO_PROVVISORIO),
    _f("sezUrb", "sezione_urbana", help="Sezione urbana", handled=True),
    _f("foglio", "foglio", help="Foglio", handled=True),
    _f("particella1", "particella", help="Particella (numero)", handled=True),
    _f("particella2", "denominatore", help="Particella (denominatore)"),
    _f("subalterno1", "subalterno", help="Subalterno", handled=True),
    _f(
        "tipoDenuncia",
        "tipo_denuncia",
        "select",
        "Identificativo provvisorio: assente | protocollo | scheda | variazione",
        _TIPO_DENUNCIA_CHOICES,
    ),
    _f("numero1", "numero_denuncia", help="Identificativo provvisorio: numero"),
    _f("anno", "anno_denuncia", help="Identificativo provvisorio: anno"),
    _RICHIEDENTE,
    _MOTIVO,
)

_BASE_FORMS: dict[str, QueryForm] = {
    "search": QueryForm("search", "immobile", _IMMOBILE),
    "intestati": QueryForm("intestati", "immobile", _IMMOBILE),
    "soggetto": QueryForm(
        "soggetto",
        "soggetto",
        (
            _TIPO_CATASTO,
            _RESTRIZIONE_FOGLIO,
            _SEZIONE_AUTO,
            _SEZIONE_URBANA,
            _FOGLIO,
            _f(
                "selDatiAna",
                "ricerca_per",
                "radio",
                "cf | cognome (default: cf se indicato)",
                {"cognome": "cognome", "cf": "CF_PF"},
            ),
            _f("cognome", "cognome", help="Cognome"),
            _f("nome", "nome", help="Nome"),
            _f("gg_nascita", "giorno_nascita", help="Giorno di nascita"),
            _f("mm_nascita", "mese_nascita", help="Mese di nascita"),
            _f("anno_nascita", "anno_nascita", help="Anno di nascita"),
            _f("sesso", "sesso", "select", "M | F"),
            _f("provincia_amm_pf", "provincia_nascita", "select", "Provincia di nascita (sigla)"),
            _f("luogo_nasc", "comune_nascita", "select", "Comune di nascita"),
            _f("cod_fisc_pf", "codice_fiscale", help="Codice fiscale", handled=True),
            _RICHIEDENTE,
            _MOTIVO,
        ),
    ),
    "azienda": QueryForm(
        "azienda",
        "azienda",
        (
            _TIPO_CATASTO,
            _RESTRIZIONE_FOGLIO,
            _SEZIONE_AUTO,
            _SEZIONE_URBANA,
            _FOGLIO,
            _f(
                "selCfDn",
                "ricerca_per",
                "radio",
                "cf | denominazione (default: cf)",
                {"denominazione": "denominazione", "cf": "CF_PNF"},
            ),
            _f("denominazione", "denominazione", help="Denominazione"),
            _f("provincia_amm", "provincia_sede", "select", "Provincia della sede (sigla)"),
            _f("sede", "comune_sede", "select", "Comune della sede"),
            _f(
                "tipo_ispezione",
                "ristretta",
                "checkbox",
                "Solo soggetti con dati anagrafici uguali (ricerca ristretta)",
                {"true": "R"},
            ),
            _f("cod_fisc", "identificativo", help="Partita IVA / codice fiscale", handled=True),
            _RICHIEDENTE,
            _MOTIVO,
        ),
    ),
    "elenco": QueryForm(
        "elenco",
        "elenco",
        (
            _TIPO_CATASTO,
            _COMUNE_CAT,
            _SEZIONE,
            _SEZIONE_URBANA,
            _f("foglio", "foglio", help="Foglio", handled=True),
            _f("numero", "particella", help="Particella (numero)"),
            _f("denominatore", "denominatore", help="Particella (denominatore)"),
            _f("subannoDal", "subalterno_dal", help="Subalterno da"),
            _f("subannoAl", "subalterno_al", help="Subalterno a"),
            _f("partSpeciale", "partita_speciale", "select", "Partita speciale"),
            _f("categoria", "categoria", "select", "Categoria (es. A/2, o A% per tutto il gruppo A)"),
            _RICHIEDENTE,
            _MOTIVO,
        ),
    ),
    "indirizzo": QueryForm(
        "indirizzo",
        "indirizzo",
        (
            _COMUNE_CAT,
            _SEZIONE,
            _f("toponimo", "toponimo", "select", "Toponimo (es. VIA; default TUTTI)"),
            _f("indirizzo", "indirizzo", help="Indirizzo (parte di parola o parola intera)", handled=True),
            _f(
                "parIntera",
                "parola_intera",
                "radio",
                "true = parola intera, false = parte di parola",
                {"false": "0", "true": "1"},
            ),
            _RICHIEDENTE,
            _MOTIVO,
            # second page: the addresses found, then the civic numbers
            _f(
                "indirizzoSel", "indirizzo_selezionato", "select", "Indirizzo trovato da usare (default: tutti)", page=2
            ),
            _f("numCivicoDal", "civico_dal", help="Numero civico dal", page=2),
            _f("numCivicoAl", "civico_al", help="Numero civico al", page=2),
        ),
        extra_params=("provincia", "tipo_catasto"),
    ),
    "partita": QueryForm(
        "partita",
        "partita",
        (
            _TIPO_CATASTO,
            _COMUNE_CAT,
            _SEZIONE,
            _f("numPart", "partita", help="Partita numero", handled=True),
            _f("numFoglio", "foglio", help="Eventuale limitazione al foglio"),
            _RICHIEDENTE,
            _MOTIVO,
        ),
    ),
    "nota": QueryForm(
        "nota",
        "nota",
        (
            _TIPO_CATASTO,
            _f("comuneCat", "comune", "select", "Comune"),
            _SEZIONE_AUTO,
            _f("numNota", "numero_nota", help="Numero nota", handled=True),
            _f("progressivo", "progressivo", help="Progressivo"),
            _f("anno", "anno_nota", help="Anno", handled=True),
            _f("numRepertorio", "numero_repertorio", help="Numero repertorio"),
            _f("giornoEff", "giorno_efficacia", help="Data di efficacia: giorno"),
            _f("meseEff", "mese_efficacia", help="Data di efficacia: mese"),
            _f("annoEff", "anno_efficacia", help="Data di efficacia: anno"),
            _f("tipoNota", "tipo_nota", "select", "Tipo nota (obbligatorio per SISTER)"),
            _MOTIVO,
        ),
    ),
    "mappa": QueryForm(
        "mappa",
        "mappa",
        (
            _f("tipoRichiesta", "tipo_richiesta", "radio", "attualita | storica", {"attualita": "0", "storica": "1"}),
            _COMUNE_CAT,
            _SEZIONE,
            _f("foglio", "foglio", help="Foglio", handled=True),
            _f("particelle", "particella", help="Particelle"),
            _f("interoFoglio", "intero_foglio", "checkbox", "Intero foglio", {"true": "true"}),
            _f("mappeLimitrofe", "mappe_limitrofe", "checkbox", "Mappe limitrofe", {"true": "true"}),
            _f("a3", "formato_mappa", "radio", "a3 | a4 | originaria", {"a3": "3", "a4": "4", "originaria": "0"}),
            _f("foglioStorico", "foglio_storico", help="Richiesta storica: foglio"),
            _f("particellaStorico", "particella_storica", help="Richiesta storica: particella"),
            _f("dataAl", "data_al", "date", "Richiesta storica: data al (GG/MM/AAAA, dal 01/01/2014)"),
            _MOTIVO,
        ),
        extra_params=("provincia", "tipo_catasto"),
    ),
    "export-mappa": QueryForm(
        "export-mappa",
        "export-mappa",
        (
            _COMUNE_CAT,
            _SEZIONE,
            _f(
                "sistemaCoordinate",
                "sistema_coordinate",
                "select",
                "originario | roma40 | etrf2000",
                {"originario": "00", "roma40": "GB", "etrf2000": "WM"},
            ),
            _f(
                "formato",
                "formato",
                "select",
                "cxf | cmf | dxf | geojson",
                {"cxf": "C", "cmf": "M", "dxf": "D", "geojson": "J"},
            ),
            _MOTIVO,
        ),
        extra_params=("provincia", "tipo_catasto", "foglio"),
    ),
    "originali": QueryForm(
        "originali",
        "originali",
        (_COMUNE_CAT, _SEZIONE_AUTO, _MOTIVO),
        extra_params=("provincia", "tipo_catasto", "foglio"),
    ),
    "fiduciali": QueryForm(
        "fiduciali",
        "fiduciali",
        (
            _COMUNE_CAT,
            _SEZIONE_AUTO,
            _f("foglio", "foglio", help="Ricerca per foglio: foglio"),
            _f("allegato", "allegato", help="Ricerca per foglio: allegato"),
            _f(
                "tipoRic",
                "tipo_ricerca",
                "radio",
                "punto | attendibilita | dominio",
                {"punto": "1", "attendibilita": "2", "dominio": "3"},
            ),
            _f("fiduciale", "numero_fiduciale", help="Punto fiduciale: numero"),
            _f("attendMin", "attendibilita_min", help="Attendibilità minima"),
            _f("attendMax", "attendibilita_max", help="Attendibilità massima"),
            _f("nordMin", "nord_min", help="Dominio: nord minimo"),
            _f("nordMax", "nord_max", help="Dominio: nord massimo"),
            _f("estMin", "est_min", help="Dominio: est minimo"),
            _f("estMax", "est_max", help="Dominio: est massimo"),
            _f("tipoFile", "tipo_file", "radio", "pdf | txt", {"pdf": "pdf", "txt": "dat"}),
            _MOTIVO,
        ),
        extra_params=("provincia", "tipo_catasto"),
    ),
    "ispezioni": QueryForm(
        "ispezioni",
        "ispezioni",
        (
            _COMUNE_DENOM,
            _TIPO_CATASTO,
            _f("sezCens", "sezione_censuaria", "select", "Sezione censuaria"),
            _f(
                "tipoIdentificativo",
                "identificativo_tipo",
                "radio",
                "definitivo | provvisorio",
                {"definitivo": "0", "provvisorio": "1"},
            ),
            _f("sezUrb", "sezione_urbana", help="Sezione urbana"),
            _f("foglio", "foglio", help="Foglio", handled=True),
            _f("particella1", "particella", help="Particella (numero)", handled=True),
            _f("particella2", "denominatore", help="Particella (denominatore)"),
            _f("subalterno1", "subalterno", help="Subalterno"),
            _f(
                "tipoDenuncia",
                "tipo_denuncia",
                "select",
                "protocollo | scheda | variazione",
                {"protocollo": "P", "scheda": "S", "variazione": "V"},
            ),
            _f("numero1", "numero_denuncia", help="Identificativo provvisorio: numero"),
            _f("anno", "anno_denuncia", help="Identificativo provvisorio: anno"),
            _f("dataInizio", "data_inizio", "date", "Formalità dal (GG/MM/AAAA)"),
            _f("dataFine", "data_fine", "date", "Formalità al (GG/MM/AAAA)"),
            _f("TRASCRIZIONI", "trascrizioni", "checkbox", "Includi le trascrizioni", {"true": "T"}),
            _f("ISCRIZIONI", "iscrizioni", "checkbox", "Includi le iscrizioni", {"true": "I"}),
            _f("ANNOTAZIONI", "annotazioni", "checkbox", "Includi le annotazioni", {"true": "A"}),
            _f(
                "escl_ipo_canc",
                "escludi_ipoteche_cancellate",
                "checkbox",
                "Escludi ipoteche non rinnovate o cancellate",
                {"true": "1"},
            ),
            _f(
                "escl_trascr_norinn",
                "escludi_trascrizioni_non_rinnovate",
                "checkbox",
                "Escludi trascrizioni non rinnovate",
                {"true": "1"},
            ),
            _RICHIEDENTE,
            _MOTIVO,
        ),
    ),
    "ispezioni-cartacee": QueryForm(
        "ispezioni-cartacee",
        "ispezioni-cartacee",
        (
            _f(
                "tipoNota",
                "tipo_nota",
                "select",
                "trascrizioni | iscrizioni | annotazioni | privilegi_agrari | privilegi_speciali | privilegi_minerari",
                {
                    "trascrizioni": "T",
                    "iscrizioni": "I",
                    "annotazioni": "A",
                    "privilegi_agrari": "PA",
                    "privilegi_speciali": "PS",
                    "privilegi_minerari": "PM",
                },
            ),
            _f("registro", "registro", "radio", "particolare | generale", {"particolare": "0", "generale": "1"}),
            _f("regPart", "registro_particolare", help="Registro particolare n°"),
            _f("regPartBis", "registro_particolare_bis", help="Registro particolare: bis"),
            _f("annoNotaPart", "anno_nota_particolare", help="Registro particolare: anno"),
            _f("regGen", "registro_generale", help="Registro generale n°"),
            _f("annoNotaGen", "anno_nota_generale", help="Registro generale: anno"),
            _f("informazioniAgg", "informazioni_aggiuntive", help="Informazioni aggiuntive per l'ufficio"),
            _f("esclAllegati", "escludi_allegati", "checkbox", "Con esclusione degli allegati", {"true": "1"}),
            _RICHIEDENTE,
            _MOTIVO,
        ),
        extra_params=("provincia", "comune", "tipo_catasto", "foglio", "particella"),
    ),
    "elaborato-planimetrico": QueryForm(
        "elaborato-planimetrico",
        "elaborato-planimetrico",
        (
            _COMUNE_DENOM,
            _SEZIONE_AUTO,
            _SEZIONE_URBANA,
            _f("foglio", "foglio", help="Foglio", handled=True),
            _f("particella1", "particella", help="Particella (numero, obbligatoria per SISTER)", handled=True),
            _f("particella2", "denominatore", help="Particella (denominatore)"),
            _MOTIVO,
        ),
    ),
    "riepilogo": QueryForm(
        "riepilogo",
        "riepilogo",
        (
            _f("giorno", "data_visura", "date", "Data visura (GG/MM/AAAA)"),
            _RICHIEDENTE,
        ),
        extra_params=(),
    ),
}


# --- command metadata -----------------------------------------------------------------------------------

# long/short flags of the parameters whose CLI option is not just ``--<param-with-dashes>``
FLAGS: dict[str, tuple[str, ...]] = {
    "provincia": ("--provincia", "-P"),
    "comune": ("--comune", "-C"),
    "foglio": ("--foglio", "-F"),
    "particella": ("--particella", "-p"),
    "tipo_catasto": ("--tipo-catasto", "-t"),
    "subalterno": ("--subalterno", "-sub"),
    "codice_fiscale": ("--cf", "-i"),
    "identificativo": ("--id", "-i"),
    "indirizzo": ("--indirizzo", "-a"),
    "numero_nota": ("--numero", "-n"),
}

_EXTRA_HELP = {
    "provincia": "Province name: selects the SISTER office (omit for a national search)",
    "tipo_catasto": "T = Terreni, F = Fabbricati, E = both (this form has no catasto input)",
    "foglio": "Sheet number (not an input of this form)",
    "comune": "Municipality (not an input of this form)",
    "particella": "Parcel number (not an input of this form)",
}

_P = ("provincia", "comune")
# command -> (summary, API path, client method, search_type, dedicated args, required, required_any)
_META: dict[str, tuple] = {
    "search": (
        "Submit an immobili search on SISTER (POST /visura).",
        "/visura",
        "search",
        "",
        ("provincia", "comune", "foglio", "particella", "tipo_catasto", "sezione", "subalterno"),
        (*_P, "foglio", "particella"),
        (),
    ),
    "intestati": (
        "Submit an owners (intestati) lookup on SISTER (POST /visura/intestati).",
        "/visura/intestati",
        "intestati",
        "",
        ("provincia", "comune", "foglio", "particella", "tipo_catasto", "subalterno", "sezione"),
        (*_P, "foglio", "particella", "tipo_catasto"),
        (),
    ),
    "soggetto": (
        "National search by codice fiscale on SISTER (POST /visura/soggetto).",
        "/visura/soggetto",
        "soggetto",
        "",
        ("codice_fiscale", "tipo_catasto", "provincia"),
        (),
        (("codice_fiscale", "cognome"),),
    ),
    "azienda": (
        "Search by legal entity (P.IVA or company name) on SISTER (POST /visura/persona-giuridica).",
        "/visura/persona-giuridica",
        "persona_giuridica",
        "",
        ("identificativo", "tipo_catasto", "provincia"),
        ("identificativo",),
        (),
    ),
    "elenco": (
        "List all properties in a comune (POST /visura/elenco-immobili).",
        "/visura/elenco-immobili",
        "elenco_immobili",
        "",
        ("provincia", "comune", "tipo_catasto", "foglio", "sezione"),
        _P,
        (),
    ),
    "indirizzo": (
        "Search by street address (IND) on SISTER.",
        "/visura/indirizzo",
        "generic_search",
        "indirizzo",
        (),
        (*_P, "indirizzo"),
        (),
    ),
    "partita": (
        "Search by partita catastale number (PART) on SISTER.",
        "/visura/partita",
        "generic_search",
        "partita",
        (),
        (*_P, "partita"),
        (),
    ),
    "nota": (
        "Search by annotation/note reference (NOTA) on SISTER.",
        "/visura/nota",
        "generic_search",
        "nota",
        (),
        ("provincia", "numero_nota"),
        (),
    ),
    "mappa": (
        "View cadastral map data (EM) on SISTER.",
        "/visura/mappa",
        "generic_search",
        "mappa",
        (),
        (*_P, "foglio"),
        (),
    ),
    "export-mappa": (
        "Export cadastral map data (EXPM) on SISTER.",
        "/visura/export-mappa",
        "generic_search",
        "export-mappa",
        (),
        _P,
        (),
    ),
    "originali": (
        "Retrieve original registration records (OOII) on SISTER.",
        "/visura/originali",
        "generic_search",
        "originali",
        (),
        _P,
        (),
    ),
    "fiduciali": (
        "Retrieve survey reference points (FID) on SISTER.",
        "/visura/fiduciali",
        "generic_search",
        "fiduciali",
        (),
        _P,
        (),
    ),
    "ispezioni": (
        "Search property inspection records (ISP) on SISTER.",
        "/visura/ispezioni",
        "generic_search",
        "ispezioni",
        (),
        _P,
        (),
    ),
    "ispezioni-cartacee": (
        "Search paper inspection records (ISPCART) on SISTER.",
        "/visura/ispezioni-cartacee",
        "generic_search",
        "ispezioni-cartacee",
        (),
        ("provincia",),
        (),
    ),
    "elaborato-planimetrico": (
        "Retrieve Elaborato Planimetrico (ELPL) on SISTER.",
        "/visura/elaborato-planimetrico",
        "generic_search",
        "elaborato-planimetrico",
        (),
        _P,
        (),
    ),
    "riepilogo": (
        "View your SISTER query history (Riepilogo Visure).",
        "/visura/riepilogo-visure",
        "generic_search",
        "riepilogo-visure",
        (),
        (),
        (),
    ),
}


# inputs that the portal only enables in one mode of a radio (e.g. the provisional identifier of an immobile)
_PROVVISORIO = ("identificativo_tipo", "provvisorio")
_DEFINITIVO = ("identificativo_tipo", "definitivo")
_REQUIRES: dict[tuple[str, str], tuple[str, str]] = {}
for _cmd in ("search", "intestati", "ispezioni"):
    for _param in ("tipo_denuncia", "numero_denuncia", "anno_denuncia"):
        _REQUIRES[(_cmd, _param)] = _PROVVISORIO
    _REQUIRES[(_cmd, "denominatore")] = _DEFINITIVO
for _param in (
    "cognome",
    "nome",
    "giorno_nascita",
    "mese_nascita",
    "anno_nascita",
    "sesso",
    "provincia_nascita",
    "comune_nascita",
):
    _REQUIRES[("soggetto", _param)] = ("ricerca_per", "cognome")
for _param in ("denominazione", "provincia_sede", "comune_sede", "ristretta"):
    _REQUIRES[("azienda", _param)] = ("ricerca_per", "denominazione")
for _param in ("foglio_storico", "particella_storica", "data_al"):
    _REQUIRES[("mappa", _param)] = ("tipo_richiesta", "storica")
for _param in ("particella", "intero_foglio", "mappe_limitrofe", "formato_mappa"):
    _REQUIRES[("mappa", _param)] = ("tipo_richiesta", "attualita")
for _param in ("registro_particolare", "registro_particolare_bis", "anno_nota_particolare"):
    _REQUIRES[("ispezioni-cartacee", _param)] = ("registro", "particolare")
for _param in ("registro_generale", "anno_nota_generale"):
    _REQUIRES[("ispezioni-cartacee", _param)] = ("registro", "generale")


def _finalise(base: dict[str, QueryForm]) -> dict[str, QueryForm]:
    forms: dict[str, QueryForm] = {}
    for command, form in base.items():
        summary, path, method, search_type, dedicated, required, required_any = _META[command]
        extras = tuple(
            FormField((), name, "select" if name == "tipo_catasto" else "text", _EXTRA_HELP.get(name, ""))
            for name in form.extra_params
            if name not in {f.param for f in form.fields}
        )
        fields = tuple(
            replace(fld, requires=_REQUIRES[(command, fld.param)]) if (command, fld.param) in _REQUIRES else fld
            for fld in form.fields
        )
        forms[command] = QueryForm(
            command,
            form.fixture,
            fields + extras,
            (),
            summary,
            path,
            method,
            search_type,
            dedicated,
            required,
            required_any,
        )
    return forms


def _extra(command, summary, path, search_type, params, required=(), required_any=()):
    """A query without a portal form: only its parameters (CLI/API/batch)."""
    fields = tuple(FormField((), name, kind, help_) for name, kind, help_ in params)
    return QueryForm(
        command, None, fields, (), summary, path, "generic_search", search_type, (), required, required_any
    )


QUERY_FORMS: dict[str, QueryForm] = _finalise(_BASE_FORMS)
_PROVINCIA_HELP = _EXTRA_HELP["provincia"]
_NO_FORM = (
    _extra(
        "richieste-sister",
        "View pending/completed requests on SISTER (Richieste).",
        "/visura/richieste",
        "richieste",
        (),
    ),
    _extra(
        "ipotecaria-stato",
        "Check Ispezioni Ipotecarie automation status (Stato dell'automazione).",
        "/visura/ipotecaria-stato",
        "ipotecaria-stato",
        (),
    ),
    _extra(
        "ipotecaria-elenchi",
        "View billed lists (Elenchi contabilizzati) from Ispezioni Ipotecarie.",
        "/visura/ipotecaria-elenchi",
        "ipotecaria-elenchi",
        (),
    ),
    _extra(
        "visura-storica",
        "Historical visura per immobile (Storica Analitica), without the per-owner step.",
        "/visura/visura-storica",
        "visura-storica",
        (
            ("provincia", "text", _PROVINCIA_HELP),
            ("comune", "text", "Municipality"),
            ("foglio", "text", "Sheet number"),
            ("particella", "text", "Parcel number"),
            ("tipo_catasto", "select", "T = Terreni, F = Fabbricati"),
            ("subalterno", "text", "Sub-unit"),
            ("sezione", "text", "Section"),
        ),
        ("provincia", "comune", "foglio", "particella"),
    ),
    _extra(
        "soggetto-documento",
        "Request the Visura per Soggetto document (XML) of a persona fisica.",
        "/visura/soggetto-documento",
        "soggetto-documento",
        (
            ("provincia", "text", _PROVINCIA_HELP),
            ("codice_fiscale", "text", "Codice fiscale"),
            ("tipo_catasto", "select", "T = Terreni, F = Fabbricati, E = both"),
            ("vista", "select", "analitica | sintetica"),
        ),
        ("codice_fiscale",),
    ),
    _extra(
        "soggetto-immobili",
        "List the properties of a person or company (optionally with each property's owners).",
        "/visura/soggetto-immobili",
        "soggetto-immobili",
        (
            ("provincia", "text", _PROVINCIA_HELP),
            ("codice_fiscale", "text", "Codice fiscale / partita IVA"),
            ("tipo_catasto", "select", "T = Terreni, F = Fabbricati, E = both"),
            ("con_intestati", "checkbox", "Also read the owners of each property"),
            ("azienda", "checkbox", "The identifier is a partita IVA (persona giuridica)"),
        ),
        ("codice_fiscale",),
    ),
)
QUERY_FORMS.update({q.command: q for q in _NO_FORM})


# --- helpers --------------------------------------------------------------------------------------------


def normalize_query(name: str) -> str:
    """Spec key for a CLI command / search type (``export_mappa`` and ``export-mappa`` are the same)."""
    return name.strip().lower().replace("_", "-")


_ALIASES = {
    "ispezioni-cart": "ispezioni-cartacee",
    "elenco-immobili": "elenco",
    "persona-giuridica": "azienda",
    "riepilogo-visure": "riepilogo",
    "richieste": "richieste-sister",
    "punti-fiduciali": "fiduciali",
    "originali-impianto": "originali",
    "ricerca": "search",
    "visura": "search",
}


def get_query_form(name: str) -> QueryForm | None:
    key = normalize_query(name)
    return QUERY_FORMS.get(_ALIASES.get(key, key))


def param_names(query: str) -> list[str]:
    """Every parameter of a query (form inputs and the command's own), once each, in spec order."""
    names: list[str] = []
    for fld in QUERY_FORMS[query].fields:
        if fld.param not in names:
            names.append(fld.param)
    return names


def portal_names(query: str) -> set[str]:
    """Names of the portal inputs the spec covers on page 1 (compared with the saved fixture)."""
    return {name for fld in QUERY_FORMS[query].fields if fld.page == 1 for name in fld.portal_names}


def command_for_path(path: str) -> str | None:
    """The ``sister query`` command behind an API path (``/visura/elenco-immobili`` → ``elenco``)."""
    path = "/" + path.strip().strip("/")
    return next((q.command for q in QUERY_FORMS.values() if q.path == path), None)


def option_flags(param: str) -> tuple[str, ...]:
    return FLAGS.get(param, ("--" + param.replace("_", "-"),))


def validate_params(command: str, params: dict) -> None:
    """Raise ``ValueError`` when required parameters are missing or a choice is not one of the allowed values."""
    form = QUERY_FORMS[command]
    given = {k for k, v in params.items() if v not in (None, "")}
    missing = [name for name in form.required if name not in given]
    for group in form.required_any:
        if not given & set(group):
            missing.append(" | ".join(group))
    if missing:
        raise ValueError(f"parametri obbligatori mancanti per '{command}': {', '.join(missing)}")
    for fld in form.fields:
        value = params.get(fld.param)
        if fld.choices and fld.kind in ("radio", "select") and value not in (None, ""):
            allowed = set(fld.choices) | set(fld.choices.values())
            if str(value).lower() not in {a.lower() for a in allowed}:
                raise ValueError(f"valore non valido per '{fld.param}': {value!r} (valori: {', '.join(fld.choices)})")
