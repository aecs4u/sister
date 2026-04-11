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
            {"Foglio": "9", "Particella": "166", "Sub": "3", "Categoria": "A/2",
             "Classe": "5", "Consistenza": "4.5", "Rendita": "500,00",
             "Indirizzo": "VIA ROMA 10", "Partita": "12345"},
            {"Foglio": "9", "Particella": "166", "Sub": "5", "Categoria": "C/6",
             "Classe": "3", "Consistenza": "18", "Rendita": "120,00",
             "Indirizzo": "VIA ROMA 10", "Partita": "12345"},
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
            {"Foglio": "100", "Particella": "50", "Qualita": "SEMINATIVO",
             "Classe": "3", "Superficie": "1.200", "Reddito Dominicale": "15,00",
             "Reddito Agrario": "10,00"},
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
            {"Provincia": "RM", "Comune": "ROMA", "Foglio": "100",
             "Particella": "50", "Sub": "3", "Categoria": "A/2"},
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
            {"Provincia": "TO", "Comune": "TORINO", "Foglio": "45",
             "Particella": "123", "Sub": "1", "Categoria": "D/1"},
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
