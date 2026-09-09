"""Real-world test fixtures derived from catasto and camerali data.

Source: /data/aecs4u.it/property-scraper/recrowd/downloads/
Values are representative samples — personal data has been anonymised.
"""

# ---------------------------------------------------------------------------
# Company data (from visure_camerali)
# ---------------------------------------------------------------------------

COMPANY_TIGULLIO = {
    "taxCode": "02471840997",
    "companyName": "TIGULLIO IMMOBILIARE SRL",
    "province": "TO",
    "city": "TORINO",
    "address": "VIA FRATEL TEODORETO, 1/H",
    "legalForm": "SR",  # Limited liability company
    "pec": "TIGULLIOIMMOBILIARESRL@PEC.IT",
}

COMPANY_ALTRAVIA = {
    "taxCode": "03618371201",
    "companyName": "ALTRAVIA SERVIZI SOCIETA' A RESPONSABILITA' LIMITATA",
    "province": "BO",
    "city": "BOLOGNA",
    "affiliates": [
        {"companyName": "TERZAROLI SRL", "percentShare": 100, "taxCode": "02561291200"},
        {"companyName": "ARCO GUIDI PROPERTIES SRL", "percentShare": 100, "taxCode": "04229331204"},
        {"companyName": "OPERA LOFT SRL", "percentShare": 45, "taxCode": "03606541203"},
    ],
}

# ---------------------------------------------------------------------------
# Catasto nazionale PG (persona giuridica) search results
# ---------------------------------------------------------------------------

CATASTO_NAZIONALE_PG_ALTRAVIA = {
    "cf_piva": "03618371201",
    "tipo_catasto": "TF",
    "provincia": "NAZIONALE-IT",
    "soggetti": [
        {
            "denominazione": "ALTRAVIA SERVIZI SOCIETA' A RESPONSABILITA' LIMITATA",
            "sede": "ROMA (RM)",
            "cf": "12485671007",
            "catasti": [
                {
                    "citta": "ROMA",
                    "fabbricati": 1,
                    "terreni": 0,
                    "provincia": "RM",
                    "comuni": [{"comune": "ROMA", "fabbricati": 1, "terreni": 0}],
                }
            ],
        }
    ],
}

# ---------------------------------------------------------------------------
# Catasto nazionale PF (persona fisica) search results
# ---------------------------------------------------------------------------

CATASTO_NAZIONALE_PF_ROSSI = {
    "cf_piva": "RSSMRI85E28H501E",
    "tipo_catasto": "TF",
    "provincia": "NAZIONALE-IT",
    "soggetti": [
        {
            "cognome": "ROSSI",
            "nome": "MARIO",
            "data_nascita": "28/05/1985",
            "luogo_nascita": "ROMA (RM)",
            "sesso": "M",
            "cf": "RSSMRI85E28H501E",
            "catasti": [
                {
                    "citta": "ROMA",
                    "fabbricati": 1,
                    "terreni": 0,
                    "provincia": "RM",
                    "comuni": [{"comune": "ROMA", "fabbricati": 1, "terreni": 0}],
                }
            ],
        }
    ],
}

# ---------------------------------------------------------------------------
# Catasto indirizzo search results
# ---------------------------------------------------------------------------

CATASTO_INDIRIZZO_TERNI = {
    "provincia": "TERNI Territorio-TR",
    "comune": "L117#TERNI#0#0",
    "indirizzo": "DEL RIVO",
    "indirizzi": [
        {
            "id_indirizzo": "NDUyODgwIzI0MCNUZXJyaXRvcmlvIzI0IyNERUwgUklWTw==",
            "indirizzo": "VIA DEL RIVO",
        }
    ],
}

# ---------------------------------------------------------------------------
# SISTER API response fixtures (what the sister service returns)
# ---------------------------------------------------------------------------

SISTER_SEARCH_RESPONSE_FABBRICATI = {
    "request_id": "req_F_fixture001",
    "tipo_catasto": "F",
    "status": "completed",
    "data": {
        "immobili": [
            {
                "Foglio": "9",
                "Particella": "166",
                "Sub": "3",
                "Categoria": "A/2",
                "Classe": "5",
                "Consistenza": "4.5",
                "Rendita": "500,00",
                "Indirizzo": "VIA ROMA 10",
                "Partita": "12345",
            },
            {
                "Foglio": "9",
                "Particella": "166",
                "Sub": "5",
                "Categoria": "C/6",
                "Classe": "3",
                "Consistenza": "18",
                "Rendita": "120,00",
                "Indirizzo": "VIA ROMA 10",
                "Partita": "12345",
            },
        ],
        "results": [],
        "total_results": 2,
        "intestati": [],
    },
    "timestamp": "2026-04-10T12:00:00",
}

SISTER_SEARCH_RESPONSE_TERRENI = {
    "request_id": "req_T_fixture001",
    "tipo_catasto": "T",
    "status": "completed",
    "data": {
        "immobili": [
            {
                "Foglio": "100",
                "Particella": "50",
                "Qualita": "SEMINATIVO",
                "Classe": "3",
                "Superficie": "1.200",
                "Reddito Dominicale": "15,00",
                "Reddito Agrario": "10,00",
            },
        ],
        "results": [],
        "total_results": 1,
        "intestati": [],
    },
    "timestamp": "2026-04-10T12:05:00",
}

SISTER_INTESTATI_RESPONSE = {
    "request_id": "intestati_F_fixture001",
    "tipo_catasto": "F",
    "status": "completed",
    "data": {
        "immobile": {"Foglio": "9", "Particella": "166", "Sub": "3"},
        "intestati": [
            {
                "Nominativo o denominazione": "ROSSI MARIO",
                "Codice fiscale": "RSSMRI85E28H501E",
                "Titolarità": "Proprietà per 1/1",
            }
        ],
        "total_intestati": 1,
    },
    "timestamp": "2026-04-10T12:10:00",
}

SISTER_SOGGETTO_RESPONSE = {
    "request_id": "soggetto_E_fixture001",
    "tipo_catasto": "E",
    "status": "completed",
    "data": {
        "soggetto": "RSSMRI85E28H501E",
        "immobili": [
            {"Provincia": "RM", "Comune": "ROMA", "Foglio": "100", "Particella": "50", "Sub": "3", "Categoria": "A/2"},
        ],
        "total_results": 1,
    },
    "timestamp": "2026-04-10T12:15:00",
}

SISTER_PNF_RESPONSE = {
    "request_id": "pnf_E_fixture001",
    "tipo_catasto": "E",
    "status": "completed",
    "data": {
        "soggetto": "02471840997",
        "immobili": [
            {
                "Provincia": "TO",
                "Comune": "TORINO",
                "Foglio": "45",
                "Particella": "123",
                "Sub": "1",
                "Categoria": "D/1",
            },
        ],
        "total_results": 1,
    },
    "timestamp": "2026-04-10T12:20:00",
}

SISTER_ELENCO_RESPONSE = {
    "request_id": "eimm_T_fixture001",
    "tipo_catasto": "T",
    "status": "completed",
    "data": {
        "provincia": "Roma",
        "comune": "ROMA",
        "foglio": "100",
        "immobili": [
            {"Foglio": "100", "Particella": "1", "Qualita": "SEMINATIVO"},
            {"Foglio": "100", "Particella": "2", "Qualita": "VIGNETO"},
            {"Foglio": "100", "Particella": "3", "Qualita": "PASCOLO"},
        ],
        "total_results": 3,
    },
    "timestamp": "2026-04-10T12:25:00",
}

SISTER_NESSUNA_CORRISPONDENZA = {
    "request_id": "req_T_notfound",
    "tipo_catasto": "T",
    "status": "completed",
    "data": {
        "immobili": [],
        "results": [],
        "total_results": 0,
        "intestati": [],
        "error": "NESSUNA CORRISPONDENZA TROVATA",
    },
    "timestamp": "2026-04-10T12:30:00",
}

SISTER_ERROR_RESPONSE = {
    "request_id": "req_F_error001",
    "tipo_catasto": "F",
    "status": "error",
    "error": "Session expired during search",
    "timestamp": "2026-04-10T12:35:00",
}

SISTER_EXPIRED_RESPONSE = {
    "request_id": "req_F_expired001",
    "status": "expired",
    "message": "Risultato non più disponibile (cache scaduta)",
}

# ---------------------------------------------------------------------------
# Cadastral coordinate sets (for batch/workflow testing)
# ---------------------------------------------------------------------------

PARCELLE_TRIESTE = [
    {"provincia": "Trieste", "comune": "TRIESTE", "foglio": "9", "particella": "166", "tipo_catasto": "F"},
    {"provincia": "Trieste", "comune": "TRIESTE", "foglio": "9", "particella": "167", "tipo_catasto": "F"},
    {"provincia": "Trieste", "comune": "TRIESTE", "foglio": "12", "particella": "45", "tipo_catasto": "T"},
]

PARCELLE_ROMA = [
    {"provincia": "Roma", "comune": "ROMA", "foglio": "100", "particella": "50", "tipo_catasto": "T"},
    {"provincia": "Roma", "comune": "ROMA", "foglio": "200", "particella": "10", "tipo_catasto": "F"},
]

PARCELLE_BOLOGNA = [
    {"provincia": "Bologna", "comune": "BOLOGNA", "foglio": "55", "particella": "30", "tipo_catasto": "F"},
]

CODICI_FISCALI = [
    {"cf": "RSSMRI85E28H501E", "cognome": "ROSSI", "nome": "MARIO", "provincia": "RM"},
    {"cf": "BNCLRA90A41H501Z", "cognome": "BIANCHI", "nome": "LAURA", "provincia": "MI"},
]

PARTITE_IVA = [
    {"piva": "02471840997", "denominazione": "TIGULLIO IMMOBILIARE SRL", "provincia": "TO"},
    {"piva": "03618371201", "denominazione": "ALTRAVIA SERVIZI SRL", "provincia": "BO"},
    {"piva": "12485671007", "denominazione": "ALTRAVIA SERVIZI SRL", "provincia": "RM"},
]

# ---------------------------------------------------------------------------
# Visura soggetto (persona fisica) inputs and responses
# ---------------------------------------------------------------------------

SOGGETTO_ROSSI_INPUT = {
    "codice_fiscale": "RSSMRI85E28H501E",  # nationwide search, both catasti
}

SOGGETTO_BIANCHI_PROVINCIAL_INPUT = {
    "codice_fiscale": "BNCLRA90A41H501Z",
    "tipo_catasto": "F",
    "provincia": "MI",
}

SOGGETTO_TERRENI_ONLY_INPUT = {
    "codice_fiscale": "RSSMRI85E28H501E",
    "tipo_catasto": "T",
}

SISTER_SOGGETTO_EMPTY_RESPONSE = {
    "request_id": "soggetto_F_fixture002",
    "tipo_catasto": "F",
    "status": "completed",
    "data": {
        "soggetto": "BNCLRA90A41H501Z",
        "immobili": [],
        "total_results": 0,
        "error": "NESSUNA CORRISPONDENZA TROVATA",
    },
    "timestamp": "2026-04-10T13:00:00",
}

# ---------------------------------------------------------------------------
# Visura persona giuridica inputs and responses
# ---------------------------------------------------------------------------

PG_TIGULLIO_PIVA_INPUT = {
    "identificativo": "02471840997",  # P.IVA, nationwide
}

PG_ALTRAVIA_PROVINCIAL_INPUT = {
    "identificativo": "03618371201",
    "tipo_catasto": "F",
    "provincia": "BO",
}

PG_BY_NAME_INPUT = {
    "identificativo": "TIGULLIO IMMOBILIARE SRL",
    "tipo_catasto": "E",
}

# SISTER_PNF_RESPONSE already covers the success case

SISTER_PG_EMPTY_RESPONSE = {
    "request_id": "pnf_E_fixture002",
    "tipo_catasto": "E",
    "status": "completed",
    "data": {
        "soggetto": "AZIENDA INESISTENTE SRL",
        "immobili": [],
        "total_results": 0,
        "error": "NESSUNA CORRISPONDENZA TROVATA",
    },
    "timestamp": "2026-04-10T13:05:00",
}

# ---------------------------------------------------------------------------
# Elenco immobili inputs and responses
# ---------------------------------------------------------------------------

ELENCO_TRIESTE_TERRENI_INPUT = {
    "provincia": "Trieste",
    "comune": "TRIESTE",
    "tipo_catasto": "T",
}

ELENCO_TRIESTE_SHEET_INPUT = {
    "provincia": "Trieste",
    "comune": "TRIESTE",
    "tipo_catasto": "F",
    "foglio": "9",
}

ELENCO_ROMA_NO_SHEET_INPUT = {
    "provincia": "Roma",
    "comune": "ROMA",
    # tipo_catasto omitted — should default to "T"
}

SISTER_ELENCO_FABBRICATI_RESPONSE = {
    "request_id": "eimm_F_fixture001",
    "tipo_catasto": "F",
    "status": "completed",
    "data": {
        "provincia": "Trieste",
        "comune": "TRIESTE",
        "foglio": "9",
        "immobili": [
            {"Foglio": "9", "Particella": "166", "Sub": "1", "Categoria": "A/2"},
            {"Foglio": "9", "Particella": "166", "Sub": "3", "Categoria": "A/2"},
            {"Foglio": "9", "Particella": "167", "Sub": "1", "Categoria": "C/2"},
        ],
        "total_results": 3,
    },
    "timestamp": "2026-04-10T13:10:00",
}

# ---------------------------------------------------------------------------
# Generic single-step requests (indirizzo, partita, nota, mappa, …)
# Each dict matches the keyword args of richiedi_generic_sister()
# ---------------------------------------------------------------------------

GENERIC_INDIRIZZO_ARGS = {
    "search_type": "indirizzo",
    "provincia": "Terni",
    "comune": "TERNI",
    "tipo_catasto": "F",
    "params": {"indirizzo": "VIA DEL RIVO"},
}

GENERIC_PARTITA_ARGS = {
    "search_type": "partita",
    "provincia": "Roma",
    "comune": "ROMA",
    "tipo_catasto": "T",
    "params": {"partita": "123456"},
}

GENERIC_NOTA_ARGS = {
    "search_type": "nota",
    "provincia": "Roma",
    "tipo_catasto": "T",
    "params": {"numero_nota": "12345", "anno_nota": "2020"},
}

GENERIC_MAPPA_ARGS = {
    "search_type": "mappa",
    "provincia": "Trieste",
    "comune": "TRIESTE",
    "tipo_catasto": "T",
    "params": {"foglio": "9"},
}

GENERIC_EXPORT_MAPPA_ARGS = {
    "search_type": "export_mappa",
    "provincia": "Trieste",
    "comune": "TRIESTE",
    "tipo_catasto": "T",
    "params": {"foglio": "9"},
}

GENERIC_ORIGINALI_ARGS = {
    "search_type": "originali",
    "provincia": "Trieste",
    "comune": "TRIESTE",
    "tipo_catasto": "T",
    "params": {},
}

GENERIC_FIDUCIALI_ARGS = {
    "search_type": "fiduciali",
    "provincia": "Roma",
    "comune": "ROMA",
    "tipo_catasto": "T",
    "params": {},
}

GENERIC_ISPEZIONI_ARGS = {
    "search_type": "ispezioni",
    "provincia": "Trieste",
    "comune": "TRIESTE",
    "tipo_catasto": "T",
    "params": {"foglio": "9"},
}

GENERIC_ISPEZIONI_CART_ARGS = {
    "search_type": "ispezioni_cart",
    "provincia": "Trieste",
    "comune": "TRIESTE",
    "tipo_catasto": "T",
    "params": {},
}

GENERIC_ELABORATO_ARGS = {
    "search_type": "elaborato_planimetrico",
    "provincia": "Roma",
    "comune": "ROMA",
    "tipo_catasto": "F",
    "params": {},
}

SISTER_GENERIC_QUEUED_RESPONSE = {
    "status": "queued",
    "tipo_catasto": "T",
}

SISTER_INDIRIZZO_RESPONSE = {
    "request_id": "indirizzo_F_fixture001",
    "tipo_catasto": "F",
    "status": "completed",
    "data": {
        "indirizzi": [{"indirizzo": "VIA DEL RIVO", "id_indirizzo": "NDUyODgw"}],
        "immobili": [{"Foglio": "12", "Particella": "100", "Categoria": "A/2"}],
        "total_results": 1,
    },
    "timestamp": "2026-04-10T13:15:00",
}

# ---------------------------------------------------------------------------
# Ispezione ipotecaria inputs (paid service, 4 search types)
# Field names match IspezioneIpotecariaInput aliases
# ---------------------------------------------------------------------------

IPOTECARIA_IMMOBILE_INPUT = {
    "search_type": "immobile",
    "province": "Roma",
    "municipality": "ROMA",
    "sheet": "100",
    "parcel": "50",
    "cadastre_type": "F",
    "auto_confirm": False,
}

IPOTECARIA_PF_INPUT = {
    "search_type": "persona_fisica",
    "province": "Roma",
    "fiscal_code": "RSSMRI85E28H501E",
    "auto_confirm": False,
}

IPOTECARIA_PG_INPUT = {
    "search_type": "persona_giuridica",
    "province": "Roma",
    "identifier": "02471840997",
    "auto_confirm": False,
}

IPOTECARIA_NOTA_INPUT = {
    "search_type": "nota",
    "province": "Roma",
    "note_number": "12345",
    "note_year": "2020",
    "auto_confirm": False,
}

SISTER_IPOTECARIA_RESPONSE = {
    "request_id": "ipotecaria_immobile_fixture001",
    "tipo_ricerca": "immobile",
    "status": "completed",
    "data": {
        "search_type": "immobile",
        "note": [{"tipo": "Ipoteca volontaria", "numero": "12345/2020", "importo": "200.000,00"}],
        "total_note": 1,
    },
    "timestamp": "2026-04-10T13:25:00",
}

# ---------------------------------------------------------------------------
# Multi-step workflow inputs (all 10 presets)
# ---------------------------------------------------------------------------

WORKFLOW_DUE_DILIGENCE_INPUT = {
    "preset": "due-diligence",
    "provincia": "Roma",
    "comune": "ROMA",
    "foglio": "100",
    "particella": "50",
}

WORKFLOW_PATRIMONIO_INPUT = {
    "preset": "patrimonio",
    "codice_fiscale": "RSSMRI85E28H501E",
}

WORKFLOW_FONDIARIO_INPUT = {
    "preset": "fondiario",
    "provincia": "Trieste",
    "comune": "TRIESTE",
    "foglio": "9",
}

WORKFLOW_AZIENDALE_INPUT = {
    "preset": "aziendale",
    "identificativo": "02471840997",
}

WORKFLOW_STORICO_INPUT = {
    "preset": "storico",
    "provincia": "Trieste",
    "comune": "TRIESTE",
    "foglio": "9",
    "particella": "166",
}

WORKFLOW_INDIRIZZO_INPUT = {
    "preset": "indirizzo",
    "provincia": "Terni",
    "comune": "TERNI",
    "indirizzo": "VIA DEL RIVO 1",
}

WORKFLOW_CROSS_REF_INPUT = {
    "preset": "cross-reference",
    "codice_fiscale": "RSSMRI85E28H501E",
    "identificativo": "02471840997",
}

WORKFLOW_FULL_DUE_DILIGENCE_INPUT = {
    "preset": "full-due-diligence",
    "provincia": "Roma",
    "comune": "ROMA",
    "foglio": "100",
    "particella": "50",
}

WORKFLOW_FULL_PATRIMONIO_INPUT = {
    "preset": "full-patrimonio",
    "codice_fiscale": "RSSMRI85E28H501E",
}

WORKFLOW_FULL_AZIENDALE_INPUT = {
    "preset": "full-aziendale",
    "identificativo": "02471840997",
}
