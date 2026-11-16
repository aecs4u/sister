"""Parser for the text of an Ispezione ipotecaria note PDF (synthetic text, same layout as the portal's)."""

from sister.mortgage_note_projection import parse_note_text, slug

HEADER = """                                                                                     Ispezione ipotecaria
Ufficio Provinciale di PALERMO - Territorio                                        Data 11/11/2026 Ora 15:36:27
Servizio di Pubblicità Immobiliare
                                                                                                    Pag. {page} - {tail}

Ispezione telematica
Motivazione Esplorazione                                            n. T1 91777 del 11/11/2026
                                                                    Inizio ispezione 11/11/2026 15:05:35
Richiedente RSSMRA per conto di                                     Tassa versata € 4,00
            RSSMRA80A01H501U
Nota di trascrizione
Registro generale n.         11046
Registro particolare n.      8036                                   Presentazione n. 109          del 17/03/2001
"""

NOTE = (
    HEADER.format(page=1, tail="segue")
    + """
Sezione A - Generalità
Dati relativi al titolo
Descrizione           ATTO NOTARILE PUBBLICO
Data                  14/03/2001                                        Numero di repertorio 48237
Notaio                ROSSI MARIO                                       Codice fiscale  RSS MRA 45C08 G273 I
Sede                  PALERMO             (PA)

Dati relativi alla convenzione
Specie               ATTO TRA VIVI
Descrizione          112 COMPRAVENDITA

Altri dati
Sono presenti nella sezione D parti libere relative a sezione B

Sezione B - Immobili
Unità negoziale n.           1

Immobile n. 1
    Comune              G273 - PALERMO                       (PA)
    Catasto             FABBRICATI
    Sezione urbana       -        Foglio        5           Particella      3092            Subalterno     4
    Natura              C2 - MAGAZZINI E LOCALI DI           Consistenza 28 metri quadri
                        DEPOSITO
    Indirizzo           VIA ROMA                                                           N. civico 27/29
    Piano               T
"""
    + HEADER.format(page=2, tail="Fine")
    + """
Immobile n. 2
    Comune              G273 - PALERMO                       (PA)
    Catasto             FABBRICATI
    Sezione urbana       -        Foglio        5           Particella      3104            Subalterno     -
    Natura              A2 - ABITAZIONE DI TIPO
                        CIVILE                               Consistenza 5,0 vani
    Indirizzo           VIA ROMA                                                           N. civico 31

Sezione C - Soggetti
A favore
Soggetto n. 1 In qualità di       ACQUIRENTE
   Cognome VERDI                                                           Nome ANNA
   Nata il 08/04/1982          a CIPRO (EE)
   Sesso F Codice fiscale      VRD NNA 82D48 Z211 J
      Relativamente all'unità negoziale n.  1    Per il diritto di PROPRIETA'
      Per la quota di 1/2
Contro
Soggetto n. 1 In qualità di       VENDITORE
   Denominazione o ragione sociale   ALFA S.R.L.
   Sede            MILANO (MI)
   Codice fiscale  09722490969
      Relativamente all'unità negoziale n.         1      Per il diritto di   NUDA PROPRIETA'
      Per la quota di 1/1
Debitori non datori di ipoteca
Soggetto n. 1 In qualità di    DEBITORE NON DATORE
   Cognome BIANCHI                                                                      Nome LUCA
   Sesso M Codice fiscale   BNC LCU 73C14 G273 K

Sezione D - Ulteriori informazioni
Testo libero
"""
)


def test_slug():
    assert slug("PROPRIETA'") == "proprieta"
    assert slug("NUDA PROPRIETA'") == "nuda_proprieta"
    assert slug("-") is None


def test_parse_note_header_and_title():
    data = parse_note_text(NOTE)
    assert data["nota"]["tipo_nota"] == "trascrizione"
    assert data["nota"]["registro_generale"] == "11046" and data["nota"]["registro_particolare"] == "8036"
    assert data["nota"]["presentazione_data"] == "17/03/2001"
    assert data["ispezione"]["numero"] == "T1 91777" and data["ispezione"]["pagine"] == 2
    title = data["nota"]["sezione_a"]["dati_titolo"]
    assert title["tipo_titolo"] == "atto_notarile_pubblico" and title["numero_repertorio"] == "48237"
    assert title["notaio"]["codice_fiscale"] == "RSSMRA45C08G273I"


def test_parse_properties_across_a_page_break():
    (unit,) = parse_note_text(NOTE)["nota"]["sezione_b"]["unita_negoziali"]
    first, second = unit["immobili"]
    assert (first["foglio"], first["particella"], first["subalterno"]) == ("5", "3092", "4")
    assert first["natura"] == "C2" and first["natura_descrizione"] == "MAGAZZINI E LOCALI DI DEPOSITO"
    assert first["consistenza_mq"] == "28" and first["piano"] == "T" and first["numero_civico"] == "27/29"
    assert second["subalterno"] is None and second["natura_descrizione"] == "ABITAZIONE DI TIPO CIVILE"
    assert second["consistenza_vani"] == "5.0"


def test_parse_parties_and_roles():
    parties = parse_note_text(NOTE)["nota"]["sezione_c"]
    (buyer,) = parties["a_favore"]
    assert buyer["codice_fiscale"] == "VRDNNA82D48Z211J" and buyer["qualita"] == "acquirente"
    assert (buyer["quota_numeratore"], buyer["quota_denominatore"], buyer["tipo_diritto"]) == (1, 2, "proprieta")
    assert buyer["luogo_nascita"] == "CIPRO (EE)"
    (seller,) = parties["contro"]  # debtors who are not grantors are not parties of the title
    assert seller["denominazione"] == "ALFA S.R.L." and seller["tipo_diritto"] == "nuda_proprieta"


def test_not_a_note():
    assert parse_note_text("Ispezione ipotecaria\nElenco sintetico delle formalità") is None
