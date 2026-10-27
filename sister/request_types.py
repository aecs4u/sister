"""Human-readable catalog metadata for the SISTER request types shown in results."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RequestTypeInfo:
    label: str
    description: str
    tags: tuple[str, ...]


_REQUEST_TYPES: dict[str, RequestTypeInfo] = {
    "visura": RequestTypeInfo(
        "Visura per immobile",
        "Consulta i dati catastali di un immobile identificato da comune e riferimenti catastali.",
        ("Catasto", "Immobile"),
    ),
    "intestati": RequestTypeInfo(
        "Intestatari dell'immobile",
        "Mostra i soggetti e i diritti associati a un immobile.",
        ("Catasto", "Immobile", "Intestatari"),
    ),
    "soggetto": RequestTypeInfo(
        "Ricerca per persona fisica",
        "Cerca gli immobili catastali associati al codice fiscale di una persona.",
        ("Catasto", "Soggetto", "Persona fisica"),
    ),
    "persona_giuridica": RequestTypeInfo(
        "Ricerca per persona giuridica",
        "Cerca gli immobili catastali associati alla partita IVA o al codice fiscale di un'azienda.",
        ("Catasto", "Soggetto", "Persona giuridica"),
    ),
    "elenco_immobili": RequestTypeInfo(
        "Elenco immobili del soggetto",
        "Elenca gli immobili trovati per il soggetto indicato.",
        ("Catasto", "Soggetto", "Immobili"),
    ),
    "elenco": RequestTypeInfo(
        "Elenco immobili per comune",
        "Elenca gli immobili presenti nel comune indicato.",
        ("Catasto", "Immobile", "Elenco"),
    ),
    "azienda": RequestTypeInfo(
        "Ricerca per persona giuridica",
        "Cerca gli immobili catastali associati alla partita IVA o al codice fiscale di un'azienda.",
        ("Catasto", "Soggetto", "Persona giuridica"),
    ),
    "elaborato": RequestTypeInfo(
        "Elaborato planimetrico",
        "Recupera l'elaborato planimetrico con la rappresentazione delle unità di un fabbricato.",
        ("Catasto", "Fabbricati", "Elaborato planimetrico"),
    ),
    "elaborato_planimetrico": RequestTypeInfo(
        "Elaborato planimetrico",
        "Recupera l'elaborato planimetrico con la rappresentazione delle unità di un fabbricato.",
        ("Catasto", "Fabbricati", "Elaborato planimetrico"),
    ),
    "export": RequestTypeInfo(
        "Esportazione dati catastali",
        "Esporta i dati catastali restituiti dalla richiesta SISTER.",
        ("Catasto", "Esportazione"),
    ),
    "export_mappa": RequestTypeInfo(
        "Esportazione mappa catastale",
        "Esporta i dati della mappa catastale.",
        ("Catasto", "Cartografia", "Esportazione"),
    ),
    "fiduciali": RequestTypeInfo(
        "Punti fiduciali",
        "Recupera i punti fiduciali catastali del comune indicato.",
        ("Catasto", "Cartografia", "Punti fiduciali"),
    ),
    "indirizzo": RequestTypeInfo(
        "Ricerca per indirizzo",
        "Cerca le unità immobiliari usando l'indirizzo indicato.",
        ("Catasto", "Immobile", "Indirizzo"),
    ),
    "ispezioni": RequestTypeInfo(
        "Ispezioni immobiliari",
        "Cerca le ispezioni immobiliari disponibili per i riferimenti indicati.",
        ("Catasto", "Immobile", "Ispezioni"),
    ),
    "ispezioni_cartacee": RequestTypeInfo(
        "Ispezioni cartacee",
        "Cerca le ispezioni catastali conservate in formato cartaceo.",
        ("Catasto", "Storica", "Ispezioni"),
    ),
    "mappa": RequestTypeInfo(
        "Mappa catastale",
        "Consulta i dati della mappa catastale del comune indicato.",
        ("Catasto", "Cartografia"),
    ),
    "nota": RequestTypeInfo(
        "Ricerca per nota o annotazione",
        "Cerca una nota o annotazione catastale tramite i suoi riferimenti.",
        ("Catasto", "Nota"),
    ),
    "originali": RequestTypeInfo(
        "Originali di impianto",
        "Recupera i documenti catastali originali di impianto.",
        ("Catasto", "Storica", "Documento"),
    ),
    "partita": RequestTypeInfo(
        "Ricerca per partita catastale",
        "Cerca gli immobili usando il numero di partita catastale.",
        ("Catasto", "Immobile", "Partita"),
    ),
    "ispezione_ipotecaria": RequestTypeInfo(
        "Ispezione ipotecaria",
        "Consulta le formalità ipotecarie associate ai dati di ricerca indicati.",
        ("Ipotecaria", "A pagamento"),
    ),
    "richieste": RequestTypeInfo(
        "Richieste SISTER",
        "Consulta l'elenco delle richieste presenti nel portale SISTER.",
        ("SISTER", "Richieste"),
    ),
    "riepilogo": RequestTypeInfo(
        "Riepilogo visure SISTER",
        "Consulta lo storico delle richieste inviate al portale SISTER.",
        ("SISTER", "Richieste", "Storica"),
    ),
    "riepilogo_visure": RequestTypeInfo(
        "Riepilogo visure SISTER",
        "Consulta lo storico delle richieste inviate al portale SISTER.",
        ("SISTER", "Richieste", "Storica"),
    ),
    "visura_storica": RequestTypeInfo(
        "Visura storica per immobile",
        "Consulta le variazioni catastali nel tempo per un immobile.",
        ("Catasto", "Immobile", "Storica"),
    ),
    "soggetto_documento": RequestTypeInfo(
        "Visura per soggetto (documento)",
        "Richiede il documento catastale analitico o sintetico per una persona o un'azienda.",
        ("Catasto", "Soggetto", "Documento"),
    ),
    "soggetto_immobili": RequestTypeInfo(
        "Immobili del soggetto",
        "Elenca gli immobili associati a una persona o a un'azienda, con eventuali intestatari.",
        ("Catasto", "Soggetto", "Immobili"),
    ),
}

_IPOTECARIA_TARGETS = {
    "immobile": ("Ispezione ipotecaria per immobile", "Immobile"),
    "persona_fisica": ("Ispezione ipotecaria per persona fisica", "Persona fisica"),
    "persona_giuridica": ("Ispezione ipotecaria per persona giuridica", "Persona giuridica"),
    "nota": ("Ispezione ipotecaria per nota", "Nota"),
}


def _humanize(code: str) -> str:
    normalized = code.replace(":", "_").replace("-", "_")
    return " ".join(part.capitalize() for part in normalized.split("_") if part)


def request_type_info(code: str | None) -> RequestTypeInfo:
    """Describe a stored request type; unknown open-vocabulary values remain understandable and searchable."""
    raw = str(code or "").strip()
    normalized = raw.casefold().replace("-", "_")
    known = _REQUEST_TYPES.get(normalized)
    if known:
        return known

    if normalized.startswith("ipotecaria_"):
        search_target = normalized.removeprefix("ipotecaria_")
        label, target_tag = _IPOTECARIA_TARGETS.get(
            search_target,
            (f"Ispezione ipotecaria: {_humanize(search_target)}", _humanize(search_target)),
        )
        return RequestTypeInfo(
            label,
            f"Consulta le formalità ipotecarie tramite ricerca per {_humanize(search_target).lower()}.",
            ("Ipotecaria", "A pagamento", target_tag),
        )

    if normalized.startswith(("workflow_", "workflow:")):
        name = _humanize(normalized.removeprefix("workflow_").removeprefix("workflow:"))
        return RequestTypeInfo(
            f"Workflow {name}",
            f"Esecuzione del workflow {name}.",
            ("Workflow", "Procedura"),
        )

    label = _humanize(raw) or "Richiesta SISTER"
    if any(part in normalized for part in ("soggetto", "persona", "azienda")):
        tags = ("Catasto", "Soggetto")
    elif any(part in normalized for part in ("immobile", "immobili", "visura", "subaltern")):
        tags = ("Catasto", "Immobile")
    elif "mappa" in normalized or "fiducial" in normalized:
        tags = ("Catasto", "Cartografia")
    else:
        tags = ("Altro",)
    return RequestTypeInfo(
        label,
        f"Richiesta SISTER di tipo {label}.",
        tags,
    )


def known_request_types() -> list[tuple[str, RequestTypeInfo]]:
    """Return the request-type options emitted by SISTER's current single-query endpoints."""
    codes = [
        "visura",
        "intestati",
        "soggetto",
        "persona_giuridica",
        "elenco_immobili",
        "elaborato",
        "export",
        "fiduciali",
        "indirizzo",
        "ispezione_ipotecaria",
        "ispezioni",
        "ispezioni-cartacee",
        "ipotecaria_immobile",
        "ipotecaria_persona_fisica",
        "ipotecaria_persona_giuridica",
        "ipotecaria_nota",
        "visura-storica",
        "soggetto-documento",
        "soggetto-immobili",
        "richieste",
        "riepilogo",
        "mappa",
        "nota",
        "originali",
        "partita",
    ]
    # Generic single-step forms persist their SISTER ``search_type`` as the request type.
    from .query_forms import QUERY_FORMS

    for form in QUERY_FORMS.values():
        code = str(form.search_type or "").strip()
        if code and code not in codes:
            codes.append(code)
    return [(code, request_type_info(code)) for code in codes]


def known_request_tags() -> list[str]:
    return sorted({tag for _, info in known_request_types() for tag in info.tags}, key=str.casefold)
