# Analisi gap: SISTER, Comparabilitalia e Borsino Immobiliare

**Rilevazione:** 15 novembre 2026; verifica dell'applicazione Comparabilitalia e del database opendata locale aggiunte il 16 novembre 2026

**Ambito:** funzioni di dati di mercato, stima e analisi immobiliare, confrontate con il codice e la documentazione SISTER disponibili in questo repository.

> Le funzioni di Borsino Immobiliare sono ricavate dalle sue pagine pubbliche, che descrivono l'offerta e riportano anche dichiarazioni commerciali: non ho verificato i numeri dichiarati né le funzioni disponibili dopo l'accesso. Per Comparabilitalia ho invece provato l'applicazione con un account demo (sezione "Verifica diretta di Comparabilitalia"); i conteggi dichiarati restano non verificati. Per SISTER ho esaminato codice e documentazione, senza verifica browser; ho invece interrogato il database PostgreSQL locale di opendata per accertare quali dati di mercato SISTER possa leggere (sezione "Dati di mercato raggiungibili tramite opendata"). Non ho chiamato alcun endpoint a pagamento dei provider.
>
> Il checkout SISTER contiene modifiche preesistenti non committate. L'analisi considera il codice presente nel checkout; non implica che tali capacità siano già verificate nell'interfaccia o distribuite.

## Sintesi

SISTER ha una base utile e distintiva: collega il bene alla sua identità catastale, alla mappa, alla visura e ai diritti dichiarati, poi affianca valore catastale, quotazioni OMI e riferimenti da annunci. Le fonti e i limiti sono già separati in modo esplicito.

Il divario principale rispetto alle due piattaforme è il flusso estimativo professionale. SISTER non presenta un archivio interrogabile di compravendite comparabili, una selezione e correzione dei comparabili, un fascicolo di stima versionato o un rapporto dedicato che spieghi metodo, ipotesi e risultato. Le quotazioni e le stime presenti sono riferimenti per il patrimonio catastale, non un Market Approach completo.

La priorità consigliata è costruire un fascicolo valutativo verificabile collegato ai beni catastali. La ricerca di compravendite e il calcolo comparativo vanno avviati solo dopo aver verificato diritti d'uso e copertura della fonte dati. Una fonte candidata esiste già: i servizi Openapi che opendata incapsula (compravendite registrate, annunci vicini, quotazioni avanzate). SISTER legge già altri dati dallo stesso server PostgreSQL di opendata; il prerequisito tecnico è che opendata salvi lì le risposte, cosa che oggi non fa (le tabelle `realestate_*` sono vuote). Non è necessario replicare tutto il portale pubblico Borsino Immobiliare o il catalogo strumenti di Comparabilitalia per colmare il gap principale.

## Stato attuale di SISTER

| Area | Capacità trovata nel repository | Limite attuale |
|---|---|---|
| Valore catastale | Calcolo della base imponibile IMU per fabbricati e terreni a partire dalla visura. | È un valore fiscale, non un valore di mercato. |
| OMI | Fasce ufficiali per tipologie supportate e stima indicativa per fabbricato. `/omi/valuation` e il relativo codice elaborano serie semestrali di vendita/affitto, rendimenti lordi, confronti di zona e benchmark; il caricamento include anche serie NTN comunali e indicatori territoriali opzionali. | OMI e le statistiche aggregate non identificano transazioni comparabili al singolo bene. Copertura e tipologie dipendono dalle rispettive fonti. La verifica di rendering e disponibilità live è fuori dall'analisi. |
| Riferimenti da annunci | Per i fabbricati residenziali, SISTER legge estratti locali di Immobiliare.it e idealista, mostra €/m², ambito, data, copertura e valore indicativo aggregato. Immobiliare.it usa il dato più recente per comune; idealista usa i quartili dell'ultimo giorno di rilevazione, entro 1,5 km se sono presenti almeno 5 annunci, altrimenti sul comune. | Sono prezzi richiesti, non prezzi conclusi. Le fonti coprono solo i comuni presenti negli estratti; la funzione non restituisce una lista di annunci comparabili utilizzabili uno per uno. |
| Stime inserite dall'utente | Per bene catastale si possono registrare importi o fasce per provider, data, URL e note, scegliere una fonte preferita e applicare correzioni OMI personali. | Le informazioni sono trascritte dall'utente e non verificate da SISTER. Sono dati per bene, non un caso di stima con comparabili e calcoli versionati. |
| Visura e mappa | Identità catastale, visure, geometrie, zone OMI e stime per bene sono già vicine nello stesso flusso di analisi del soggetto. | Manca un workspace dedicato che raccolga il mandato, il subject, le prove di mercato e gli scenari della stima. |
| Persistenza dei calcoli | `calculation_snapshot` salva l'ultimo risultato deterministico per documento/richiesta, utente, impronta degli input e versione dell'algoritmo. | È una cache dell'ultimo calcolo, non un archivio di casi valutativi creati dall'utente con più scenari e storico delle decisioni. |
| Esportazione | Il rapporto PDF del soggetto può riportare indicatori catastali, OMI, riferimenti da annunci e stime manuali con i relativi metadati. | È un rapporto sul soggetto e sul suo patrimonio, non un rapporto estimativo autonomo con metodo, comparabili, rettifiche e conclusione. |

Riferimenti interni: [documentazione sulla valutazione](valuation.md), [caricamento delle quotazioni di mercato](../sister/market_quotes.py), [calcolo dei KPI patrimoniali](../sister/land_registry_kpis.py), [pagina OMI](../sister/web.py) e [riferimento mercato nella scheda catastale](../sister/templates/catasto/entity.html).

## Confronto delle capacità

| Capacità | Comparabilitalia | Borsino Immobiliare / BorsinoPro | Gap SISTER e priorità |
|---|---|---|---|
| Dati di mercato | La pagina funzionalità dichiara oltre 3,5 milioni di comparabili, ricerca delle compravendite e accesso a dati degli atti. Verificato nell'applicazione: ricerca di singole compravendite con prezzo, data, indirizzo, foglio e mappale; l'atto è a pagamento. | Il portale pubblica quotazioni e andamenti territoriali; BorsinoPro dichiara oltre 4 milioni di vendite e dati sugli annunci storici. | **P0 – persistenza e diritti.** SISTER non ha una banca dati di compravendite selezionabile. Una fonte esiste (Openapi `IT-rm_value`, tramite opendata) e SISTER raggiunge il database opendata, ma le tabelle delle risposte sono vuote e nessun codice le popola: manca la persistenza lato opendata. Restano da verificare diritti di conservazione e uso nei report, copertura, aggiornamento e costo per area (vedi la sezione dedicata). I conteggi dei concorrenti sono dichiarazioni pubbliche, non verifiche indipendenti. |
| Ricerca per area e trend | Ricerca nazionale, archivio, mappa e analisi storiche dei prezzi per segmento. | Quotazioni per comune e zona, variazioni annue, numero di vendite e analisi di microarea; BorsinoPro descrive anche dati territoriali e socioeconomici. | **P1.** SISTER già elabora serie OMI semestrali, confronti/benchmark di zona e contesto NTN/territoriale nel codice locale. Il gap è un workspace unico che esponga trend per segmento e distingua con chiarezza quotazioni, prezzi richiesti e transazioni. |
| Selezione dei comparabili | Ricerca, filtri, archivio personale, mappa, dati immobiliari personali e schede di comparabili. | Analisi di immobili venduti o in vendita per microarea e confronto con un prezzo desiderato. | **P1 – dopo la licenza dati.** Mancano ricerca di singole compravendite, criteri di inclusione/esclusione, archivio di lavoro e confronto del subject con i comparabili. |
| Metodo di stima | eMCA/Comparator con caratteristiche, indici mercantili, prezzi marginali e prezzi corretti; anche stima del saggio di capitalizzazione ed eDCF. Comparator ed eDCF verificati con dati ipotetici; eMCA non eseguito. | BorsinoPro descrive più strumenti di valutazione e calcolatori per tipologie diverse. | **P1.** SISTER calcola riferimenti e aggregati; non offre ancora un calcolo comparativo con rettifiche dichiarate e risultati riproducibili. |
| Report professionale | Generazione di un rapporto di valutazione editabile, dichiarato coerente con CVI e IVS. Verificato: il Comparator scarica un rapporto `.rtf`; la conformità dichiarata non è stata valutata. | Report personalizzati di stima, secondo la descrizione pubblica di BorsinoPro. | **P2.** Manca un rapporto autonomo che descriva incarico, data, subject, fonti, metodo, comparabili, rettifiche, ipotesi, limiti e conclusione. |
| Calcoli economici e fiscali | Saggio di capitalizzazione, eDCF, Property Value e monitoraggio di portafoglio sono descritti tra i prodotti. | Portale con calcolatori; BorsinoPro descrive, tra gli altri, valore catastale/IMU, capitalizzazione, redditività, nuda proprietà e imposte. | **P3.** Estendere i calcolatori solo quando c'è un caso d'uso prioritario e le assunzioni sono esplicite. L'analisi OMI di SISTER include già riferimenti vendita/affitto e rendimento lordo per zona. |
| Monitoraggio | Comparabilitalia descrive un portafoglio immobiliare con aggiornamento dei valori e avvisi. | Borsino Immobiliare pubblica aggiornamenti e andamento territoriale; il livello di allerta automatica non è verificato. | **P3.** SISTER aggrega informazioni dei soggetti, ma non è stato trovato un monitoraggio periodico del valore di mercato con variazioni e notifiche. |
| Distribuzione e onboarding | Demo gratuita, corso e formazione sono descritti sul sito. | Portale pubblico con quotazioni, guide, calcolatori, API e prodotto professionale separato. | **P4.** Un esploratore pubblico delle quotazioni o API di stima è un prodotto distinto. Da considerare solo dopo il flusso interno e i diritti sui dati. |

## Verifica diretta di Comparabilitalia

**Data:** 16 novembre 2026. **Accesso:** `app.comparabilitalia.it`, account con pacchetto di prova "Business Demo" (valido 15/11–05/12/2026). I servizi che restituiscono dati sono stati eseguiti due volte con input diversi (Roma e Milano), tranne Maps Explorer e il calcolatore dei prezzi medi, eseguiti una volta. Non ho acquistato nulla, non ho inviato richieste a terzi e non ho caricato dati reali.

### Servizi che restituiscono dati

| Servizio | Input | Cosa restituisce | Esempio rilevato |
|---|---|---|---|
| Analisi di Mercato eTrend | Provincia, comune, categoria catastale, anno, periodo (annuale o semestre); zona OMI facoltativa | Numero di compravendite, prezzo medio unitario con scarto, superficie media, prezzo medio, istogramma per fascia di €/m², mappa, confronto con il periodo precedente ed elenco delle singole compravendite | Roma, A/2, 2025: 14.354 compravendite (su 14.425), 3.124,54 €/m², 100,11 m², 321.046 €. Milano, A/3, 2024: 10.475 (su 10.759), 4.065,43 €/m², 77,24 m², 343.255 € |
| Trova Prezzi Immobiliari (BIP) | Indirizzo sulla mappa, categoria (obbligatoria), anno, fascia di prezzo | Compravendite nell'area visualizzata: coordinate, indirizzo, prezzo, anno e mese, consistenza, zona OMI, categoria, €/m², superficie commerciale | Roma, Via del Corso: A02 in Via del Gambero 7, 1.250.000 €, 70,30 m², 09/2025. Milano, Corso Buenos Aires: 18 compravendite nell'area |
| Scheda dati compravendita | Si apre da eTrend, BIP o eVIA | Indirizzo, coordinate, anno e mese, comune, foglio, mappale, numero di registro particolare, unità nell'atto, categoria, consistenza, zona OMI, quota, superficie stimata e commerciale, prezzo, €/m² | Reg. part. 039739/2021: Via Etruria 37, Roma, foglio 893, mappale 203, A04, 56 m², 63.678 €, 1.137,11 €/m² |
| eVIA | Provincia, comune, settore; periodo e prezzo facoltativi | Elenco di compravendite di categorie speciali, 10 per pagina, con categoria, indirizzo (spesso assente), prezzo, unità, superfici e data | Roma e Milano, settore Industriale: 3 pagine ciascuno, soprattutto D01 |
| Property Value | Indirizzo, categoria, superficie, data, valore di mercato, durata del mutuo | Valore "sostenibile" a 10, 20 e 30 anni, media dei prezzi del periodo, numero di dati usati e raggio, grafici | Roma, Via del Corso: 89 dati entro 250 m. Milano, Corso Buenos Aires: 143 dati entro 250 m; 400.000 € → 375.616 € a 20 anni |
| Maps Explorer (FindIT) | Regione, provincia, comune, foglio, particella; CAPTCHA per attivare la ricerca | Posizione della particella sulla mappa con livello catastale e zone OMI, coordinate, nome della zona OMI | Bologna, foglio 100, particella 1: 44,5117 N 11,2897 E, zona OMI "Bologna – E5"; tabella delle quotazioni OMI vuota |
| Market Place | Indirizzo, comune, anno, categoria, tipologia edilizia, diritto, tipologia e fascia di prezzo | Comparabili in vendita sulla mappa: categoria, anno, tipologia, tipo di prezzo, diritto, presenza dell'atto; il prezzo non compare nella scheda prima dell'acquisto | Roma, A/2: "Appartamento – 3 vani", preliminare registrato, proprietà, 2021 |
| Comparator | Subject e fino a 10 comparabili inseriti a mano | Prezzi marginali, tabella di valutazione, prezzi corretti, divergenza, valore di stima, rapporto `.rtf` editabile | Tre comparabili da 365.000 a 455.000 €: valore di stima 396.365,72 €, divergenza 3,01% |
| eDCF | Redditi, costi, saggio di capitalizzazione, saggio di variazione dei prezzi, durata | TIR, saggio netto, valore iniziale e finale, esito di sostenibilità, grafico e tabella annuale | 36.000 € di redditi, 7.000 € di costi, 6%, 1,5%, 15 anni: TIR 5,84%, valore da 600.000 a 762.820 € |
| Calcolatore prezzi medi | Compravendite selezionate; la superficie catastale va inserita a mano | Prezzo unitario medio ed "Estratto campione del mercato immobiliare" in `.rtf` | 509.034 € su 1.200 m² ipotetici: 424,20 €/m² |

### Limiti rilevati

- **eVIA** restituisce risultati solo per le categorie speciali: la guida indica che al momento si cerca solo nel settore industriale (D1). Le categorie D07, D08 ed E01 risultano bloccate; una ricerca residenziale non trova nulla.
- **Superficie:** le compravendite non riportano la superficie catastale. La guida chiede di ricavarla da una visura dell'immobile prima di calcolare il prezzo medio.
- **Parti delle compravendite:** la scheda non riporta acquirente né venditore.
- **Comparator:** nelle due prove ha calcolato rettifiche solo per data e superficie, anche con piano e servizi diversi tra subject e comparabili.
- **Maps Explorer:** richiede un CAPTCHA risolto a mano; la tabella delle quotazioni OMI della particella è rimasta vuota.
- **Archivio personale:** l'archivio dei comparabili e quello degli atti sono vuoti per l'account demo; l'inserimento di comparabili richiede un account verificato, quindi eMCA non è stato eseguito.

### Servizi non eseguiti

- **Estrazione dati atti pubblici** (acquisto dell'atto dalla scheda) e **completamento del comparabile con IA**: a pagamento in tutti i piani. La conferma richiede di dichiarare un uso professionale e non mostra il prezzo.
- **AsSister:** incrocia due file salvati dall'utente dal portale SISTER dell'Agenzia delle Entrate (elenco immobili del catasto e ispezioni ipotecarie), oppure due CSV con provincia e comune. Due file HTML di prova con dati fittizi sono stati rifiutati ("Errore nell'apertura dei file HTML"). La guida è indicata come in aggiornamento.
- **Due Diligence AS:** modulo di richiesta di preventivo verso una società esterna.

### Piani e costi dichiarati nell'applicazione

| Piano | Prezzo | Ricerche BIP | Note |
|---|---|---|---|
| Professional | 35 €/mese (20 €/mese con pagamento annuale) | 200 al mese | eMCA, AsSister, archivio e rapporto di valutazione inclusi |
| Business | 50 €/mese (30 €/mese con pagamento annuale) | 400 al mese | Aggiunge eVIA, eTrend, Comparator, eDCF e completamento con IA; Property Value a pagamento |
| Corporate | Su richiesta | Illimitate | Property Value incluso, area manager con 10 accessi, monitoraggio condiviso |

L'estrazione dei dati dagli atti è a pagamento in tutti i piani. La guida di eTrend dichiara oltre 2 milioni di transazioni dal 2019; la pagina pubblica delle funzionalità dichiara oltre 3,5 milioni di comparabili.

### Cosa cambia per SISTER

- La banca dati di compravendite di Comparabilitalia è reale e interrogabile per comune, categoria, periodo e area, con riferimenti catastali (foglio, mappale) e numero di registro particolare. Il gap P0 sui dati di mercato è confermato.
- Le compravendite sono già collegate all'identità catastale del bene. È lo stesso collegamento su cui si basa SISTER, quindi una fonte di transazioni con foglio e mappale si aggancerebbe ai beni già presenti in visura.
- Comparabilitalia non automatizza il portale SISTER: AsSister e il calcolo del prezzo medio partono da visure ed elenchi che l'utente scarica a mano. L'acquisizione automatica di visure, superfici e intestatari resta l'elemento distintivo di SISTER.
- Il calcolo comparativo e l'eDCF sono moduli a inserimento manuale con rapporto `.rtf`. Le Fasi 3 e 4 del piano restano valide; il riferimento da superare è un rapporto editabile generato in pochi secondi.

## Dati di mercato raggiungibili tramite opendata

**Data:** 16 novembre 2026. **Verifica:** interrogazione in sola lettura del database PostgreSQL locale `opendata` (127.0.0.1:5432), solo conteggi e struttura; nessuna chiamata ai provider.

### Cosa SISTER legge già e cosa c'è nel database

SISTER legge già dal server opendata, in sola lettura e con degrado silenzioso se la fonte manca: i nomi di soggetti ([`subject_names.py`](../sister/subject_names.py)), i prezzi richiesti dei database `immobiliare` e `idealista`, ottenuti sostituendo il nome del database nel DSN ([`market_quotes.py`](../sister/market_quotes.py)), e le quotazioni OMI. Il canale per leggere dati di mercato quindi esiste.

Per le risposte dei servizi immobiliari la situazione verificata è questa:

| Elemento | Stato |
|---|---|
| `realestate_rmv`, `realestate_sqm_value_start`, `realestate_sqm_value_advanced` | Esistono, ma nello schema `empty` (non in `opendata`) e **con 0 righe** |
| Tabelle Comparabili (4) | Stessa situazione: schema `empty`, vuote. Riguardano il prodotto legacy `comparabili.openapi.it` |
| Chi le popola | **Nessuno.** Nel repository opendata non ho trovato codice che scriva in queste tabelle né migrazioni che le creino; le rotte le leggono soltanto (`/realestate/results`) |
| Dove finiscono le risposte | Come file JSON in `outputs/openapi/` (in locale due file di prova per le quotazioni al m²), non nel database |
| Schema `_cadastral_sales` | Contiene la vista `raw_references` (comune, sezione, foglio, particella), ricavata da `cadastral_queries` (415 righe, 17 comuni, 133 particelle) e `cadastral_prospects` (52 righe). Sono riferimenti catastali, **senza prezzi né date** |
| Variabile del DSN | Il codice legge `OPENDATA_DATABASE_DSN`, mentre `.env` definisce `OPENDATA_DATABASE_DSN_LOCALHOST`. Non ho trovato dove le due si raccordano: se non lo fanno, le letture opzionali restano disattivate senza avviso |

**Conclusione:** oggi SISTER non può leggere dal database risposte di compravendite, quotazioni o annunci. Il collegamento c'è; manca la **persistenza lato opendata**.

### Architettura consigliata

opendata chiama il provider (chiave, credito, limiti di frequenza) e salva ogni risposta; SISTER legge soltanto, senza chiavi né costi, come già fa per immobiliare e idealista.

- **Costo:** nessuna chiamata a pagamento per ogni visualizzazione di pagina. Si paga alla richiesta, una volta per zona, categoria e semestre, e il risultato è condiviso.
- **Riproducibilità:** un caso archiviato punta alle righe salvate, quindi gli stessi input e risultati si ricostruiscono anche se la fonte live cambia (criterio di accettazione della Fase 1).
- **Controllo:** un solo punto in cui si decidono quote, wallet e aggiornamenti.
- **Limiti:** il dato è fermo alla data della richiesta; la conservazione nel database richiede diritti d'uso espliciti (P0); SISTER dovrebbe usare un ruolo di sola lettura limitato alle tabelle di mercato.

### Cosa salvare (proposta)

Le tabelle attuali non bastano: `realestate_rmv` ha una sola colonna JSON con una lista, mentre la risposta reale è annidata (`sales[]` con più unità per atto). Conviene salvare in tabelle normalizzate, nello schema `opendata`, creandole con una migrazione, e conservare la risposta grezza per l'audit.

| Tabella | Contenuto | Come SISTER la aggancia |
|---|---|---|
| Richieste | endpoint, parametri, id del provider, stato ed esito, data, crediti spesi | provenienza e data per ogni valore mostrato |
| Compravendite (`IT-rm_value`) | codice univoco, prezzo, data di registrazione, coordinate, superficie totale, distanza; per unità: categoria catastale, superficie dichiarata ed effettiva, zona OMI, settore, codice Belfiore del comune, ufficio, indirizzo | comune + zona OMI + categoria + superficie + prossimità |
| Annunci (`IT-comparable`) | data della richiesta, tipo, categoria, prezzo, €/m², m², locali, piano, ascensore, comune ISTAT | comune e raggio (la risposta non ha coordinate) |
| Quotazioni (`IT-sqm_*`) | indirizzo, tipo, contratto, min/med/max dell'offerta, contesto (mix annunci, giorni di vendita, dinamica, rischio sismico) | indirizzo geocodificato o comune |

**Attenzione al legame con il bene:** la risposta delle compravendite non riporta foglio né particella, solo il codice del comune (Belfiore), la zona OMI, la categoria e le coordinate. Il collegamento al bene catastale avviene quindi per comune, zona, categoria e prossimità, non per particella. Serve inoltre una colonna per il codice catastale del comune in SISTER, oggi assente in `visura_properties` (lacuna già annotata in `CLAUDE.md`).

### Effetto sul piano

- **Fase 0:** la fonte di compravendite non va più cercata, va verificata: termini di conservazione e riuso, copertura misurata (il provider dichiara dati dal 2019, ritardo di 15–35 giorni e copertura dell'87%, non verificata) e costo per area.
- **Fase 1:** il fascicolo salva il riferimento alla richiesta e alle righe usate, non una copia dei risultati.
- **Fase 2:** la ricerca di compravendite diventa una query sulle tabelle locali (comune, zona OMI, categoria, periodo, raggio), con esclusioni motivate; l'aggiornamento è un lavoro periodico di opendata.
- **Fasi 3 e 4:** calcoli e rapporto citano l'id della richiesta e la data, e le righe incluse o escluse.
- **Pagina zona OMI:** ogni compravendita porta la propria zona OMI, quindi le aggregazioni per zona sono esatte; gli annunci, senza coordinate né zona, restano indicativi.
- **Fase 5:** gli snapshot periodici degli annunci e la serie mensile €/m² per comune (`IT-avg_sqm_value_history`) alimentano il confronto con le serie OMI semestrali.

### Stato delle query in opendata

Corretto il 16 novembre 2026 nel working tree di opendata (non ancora committato):

- **Richieste valide:** costruttori che validano come il provider (tipo numerico per le quotazioni; mesi `"01"`–`"12"`, raggio 50–20.000 m, codici `cc` e periodo dal 2019 per le compravendite). Una richiesta non valida non consuma né chiamate né crediti.
- **Quotazioni "avanzate":** il form e la rotta non inviano più condizione, piano e ascensore, che il provider non legge (la richiesta richiede solo indirizzo, tipo e contratto).
- **Risposte in attesa:** una risposta `in_erogazione` non viene più salvata in cache (prima restava in cache per sempre). Si attende per un tempo limitato leggendo `GET …/{id}`; oltre i 50 secondi la rotta risponde 202 con un token firmato, utilizzabile solo da chi ha pagato, per `GET /realestate/api/request_status`.
- **Crediti:** le due rotte delle quotazioni ora richiedono l'accesso e scalano i crediti già previsti (3 e 5), come le compravendite.
- **Test:** 50 nuovi test sul codice reale; i 115 esistenti restano verdi.

Non toccato, da decidere:

- la **persistenza** descritta sopra;
- l'incapsulamento di `IT-comparable`, `IT-omi` e `IT-avg_sqm_value_history`, presenti nella specifica ma non nel codice;
- la differenza di nome `IT-rmv` / `IT-rm_value`: il percorso attivo non è verificato in produzione;
- i listini interni (0,04 / 0,05 / 0,10 € per chiamata) rispetto ai prezzi attuali dei provider (da 0,35 €);
- i due gestori `submit_rmv` e `submit_sqm_value`, che non fanno nulla;
- i test che avviano l'intera applicazione, che non partono per un conflitto di versione tra logfire e SQLAlchemy (precedente a queste modifiche).

## Piano di sviluppo proposto

Le fasi sono ordinate per dipendenze, non per una stima temporale. La disponibilità e i diritti sulla fonte delle compravendite sono il principale elemento che può modificare l'ordine e l'ampiezza del lavoro.

### Fase 0 — Perimetro, fonti e qualità dati (**P0**)

- Definire gli utenti e gli usi prioritari: supporto a due diligence e patrimonio, valutazione di mercato per compravendita, credito o investimento. Non trattarli come lo stesso valore.
- Inventariare le coperture correnti di OMI, Immobiliare.it e idealista: comuni, periodi, tipologie, superfici, data di estrazione, numerosità e frequenza di aggiornamento.
- Verificare diritti e condizioni d'uso delle fonti già collegate (OMI, Immobiliare.it e idealista) e dei servizi Openapi incapsulati da opendata (compravendite, annunci, quotazioni), oltre a una base legale documentata per **conservare** le loro risposte nel database condiviso. Verificare ricerca, visualizzazione, esportazione, conservazione, condivisione, uso nei report e riutilizzo dei documenti.
- Definire unità di confronto e regole per deduplica, superfici, coordinate, data della transazione e identificativi catastali incompleti.
- Produrre una matrice di copertura per aree pilota e un dizionario delle fonti con significato, data, metodo, unità e limiti.

**Uscita:** perimetro approvato, copertura misurata e fonte dati utilizzabile identificata. Se non è disponibile una fonte legittima di transazioni, procedere solo con un fascicolo di evidenze OMI/annunci chiaramente etichettato come indicativo.

### Fase 1 — Fascicolo di valutazione collegato al bene (**P1**)

- Creare un caso di valutazione distinto dal record catastale: scopo, utente, data di riferimento, bene o beni, tipo di diritto, quota, assunzioni e stato del caso.
- Partire da un bene in visura e acquisire una fotografia immutabile degli input utilizzati: identità catastale completa, superfici e fonte, caratteristiche note, posizione e diritti.
- Aggregare nel fascicolo le evidenze disponibili senza confonderle: valore catastale, OMI, riferimento da annunci e valutazioni inserite dall'utente.
- Salvare per ogni evidenza fonte, ambito geografico, data, periodo di riferimento, unità di misura, numerosità quando disponibile, modalità di acquisizione e note sui limiti.
- Riutilizzare `calculation_snapshot` per i calcoli ripetibili quando opportuno, ma mantenere i casi valutativi e i loro scenari come record di lavoro autonomi e storicizzati.
- Permettere di duplicare il caso e confrontare scenari senza sovrascrivere il precedente.

**Criterio di accettazione:** riaprendo un caso archiviato si ricostruiscono gli stessi input e risultati, anche se la fonte live è cambiata o non è disponibile.

### Fase 2 — Ricerca e selezione delle compravendite (**P1, subordinata alla Fase 0**)

- Ricercare per comune, zona, raggio o microarea, tipologia, periodo, superficie, prezzo e prezzo unitario, con filtri compatibili con i campi effettivamente concessi dal provider. La ricerca interroga le tabelle di compravendite salvate da opendata (vedi "Dati di mercato raggiungibili tramite opendata"), non il provider.
- Offrire vista elenco e mappa, mostrando per ogni risultato i metadati disponibili e la provenienza, senza esporre dati personali non autorizzati.
- Consentire di includere/escludere un comparabile con motivazione; segnalare record incompleti, duplicati, troppo distanti o non confrontabili.
- Conservare nel caso una copia o un riferimento stabile dell'evidenza secondo il contratto dati; non affidare la riproducibilità a una ricerca live.
- Distinguere in interfaccia transazioni concluse, prezzi richiesti e quotazioni statistiche.

**Criterio di accettazione:** ogni comparabile usato nel calcolo è rintracciabile, ha una fonte e un periodo dichiarati, e l'utente può spiegare perché è stato incluso.

### Fase 3 — Confronto e calcolo trasparente (**P1**)

- Presentare il subject e i comparabili affiancati con caratteristiche, superficie, prezzo e €/m², evidenziando i campi mancanti.
- Consentire rettifiche manuali motivabili sulle caratteristiche rilevanti; mostrare valore prima e dopo ogni rettifica e l'effetto sul risultato.
- Separare statistiche descrittive non corrette (mediana, intervallo, dispersione e numerosità) dal valore stimato tramite metodo comparativo.
- Aggiungere analisi di sensibilità: effetto della selezione dei comparabili e delle assunzioni principali sul risultato.
- Non introdurre coefficienti automatici o modelli predittivi senza dati sufficienti, validazione su aree diverse e misure di errore documentate.

**Criterio di accettazione:** il risultato è ricalcolabile dal caso; ogni rettifica, esclusione e assunzione ha autore, data e motivazione.

### Fase 4 — Rapporto dedicato (**P2**)

- Generare un rapporto distinto dal PDF generale del soggetto catastale.
- Includere: scopo e data di valutazione, identificazione del bene e del diritto, superfici e fonti, mercato e copertura, fonti e comparabili selezionati, metodo, rettifiche, risultato/intervallo, assunzioni, incertezze e limiti.
- Mostrare separatamente i riferimenti OMI, gli annunci e le transazioni concluse; spiegare quando una sezione manca per indisponibilità dati.
- Esportare inizialmente un PDF tracciabile; valutare un formato editabile solo quando siano definite gestione delle modifiche, provenienza e responsabilità del professionista.
- Sottoporre la struttura del rapporto a valutatori qualificati prima di dichiarare conformità a standard o idoneità per finalità specifiche.

**Criterio di accettazione:** il PDF consente a un revisore di risalire dal risultato a fonti, comparabili, input, formule e versioni del caso.

### Fase 5 — Trend, portafoglio e strumenti economici (**P3/P4**)

- Aggiungere analisi di mercato per comune/zona con prezzo richiesto e transazioni su serie separate, numerosità e intervalli temporali espliciti.
- Consentire il monitoraggio periodico di un portafoglio con differenza rispetto allo snapshot precedente, soglie configurabili e notifiche; evitare di presentare una variazione di quotazione come rivalutazione certa del singolo bene.
- Valutare calcolo di redditività, capitalizzazione e DCF dopo aver definito input su canoni, costi, vacancy, durata, tassi e valore terminale, con scenari e sensitivity analysis.
- Considerare API o widget pubblici come linea di prodotto separata, con controllo di licenze, quote e isolamento dei dati.

## Requisiti trasversali

- **Semantica:** non sommare né fondere valore catastale, OMI, prezzo richiesto, prezzo di compravendita e stima professionale.
- **Provenienza:** ogni valore deve portare fonte, data/periodo, ambito, copertura, unità e versione dell'algoritmo.
- **Identità:** mantenere il collegamento esatto a comune, sezione, foglio, particella e subalterno, distinguendo bene, diritto e quota.
- **Incertezza:** rendere espliciti dato mancante, copertura insufficiente e qualità debole; assenza di quotazione non equivale a valore zero.
- **Isolamento:** casi, comparabili privati, note e preferenze devono rispettare l'identità e i permessi dell'utente.
- **Resilienza:** una fonte opzionale indisponibile non deve interrompere la visura; mostrare ultimo aggiornamento ed eventuale dato non aggiornato.
- **Audit:** mantenere modifiche, inclusioni/esclusioni, ricalcoli ed esportazioni con autore e timestamp.

## Metriche di rilascio

- Copertura: percentuale di beni e comuni con ciascuna fonte, sempre per fonte e tipologia.
- Completezza: quota di comparabili con data, prezzo, superficie, localizzazione e fonte disponibili.
- Riproducibilità: percentuale di casi archiviati che si ricalcolano sugli stessi snapshot e versioni.
- Utilizzo: casi creati, completati, duplicati e rapporti esportati; misurare separatamente i passaggi in cui l'utente corregge o rifiuta dati.
- Qualità: distribuzione degli scarti tra stime e risultati osservabili su un campione autorizzato, con metodologia e periodo dichiarati.

## Rischi e decisioni da prendere

1. **Licenza dei dati di compravendita:** senza diritti adeguati non sviluppare una banca dati concorrente basata su acquisizione non autorizzata; usare le fonti già disponibili mantenendo i limiti attuali. Vale anche per il salvataggio delle risposte Openapi nel database opendata, letto da SISTER.
2. **Persistenza lato opendata:** finché le risposte non vengono salvate (tabelle vuote, schema `empty`, nessuna migrazione), SISTER non ha nulla da leggere. Decidere schema, migrazione, ruolo di sola lettura e frequenza di aggiornamento prima di scrivere codice in SISTER.
3. **Scopo della stima:** scegliere il primo caso d'uso e la popolazione utente. Il supporto patrimoniale derivato da visura ha requisiti diversi da una valutazione per credito o da una perizia.
4. **Copertura e freschezza:** fissare soglie minime e modalità di visualizzazione per aree con pochi dati; dichiarare l'ultimo aggiornamento.
5. **Diritti reali e superfici:** non applicare automaticamente una rettifica di mercato alla quota catastale o a diritti diversi dalla piena proprietà senza input e metodo convalidati.
6. **Standard professionali:** ogni dichiarazione di conformità o certificazione richiede revisione da parte di esperti e non deve derivare dal solo formato del rapporto.

## Fonti consultate

### Concorrenti

- [Comparabilitalia — pagina iniziale](https://comparabilitalia.it/) — posizionamento e panoramica dell'applicazione.
- [Comparabilitalia — funzionalità](https://comparabilitalia.it/funzionalita/) — comparabili, ricerca, eMCA, rapporti, trend, eDCF, portafoglio e IA come descritti pubblicamente.
- [Comparabilitalia — applicazione](https://app.comparabilitalia.it/new/) — servizi provati con account demo il 16 novembre 2026 (richiede l'accesso).
- [Comparabilitalia — guide](https://app.comparabilitalia.it/new/guide) — guide dei servizi, lette integralmente (richiede l'accesso).
- [Borsino Immobiliare — pagina iniziale](https://borsinoimmobiliare.it/) — quotazioni, andamenti, guide, calcolatori e API.
- [BorsinoPro — piattaforma professionale](https://borsinoimmobiliare.it/software/borsino-pro/) — moduli e capacità descritti per il prodotto professionale.
- [Borsino Immobiliare — quotazioni di Roma](https://borsinoimmobiliare.it/quotazioni-immobiliari/lazio/roma-provincia/roma/) — esempio pubblico di indicatori comunali e quotazioni per zona.
- [Borsino Immobiliare — capitalizzazione dei redditi](https://borsinoimmobiliare.it/calcolatori/calcola-capitalizzazione-redditi/) — esempio pubblico di calcolatore.
- [Borsino Immobiliare — API dati immobiliari](https://borsinoimmobiliare.it/software/api-mercato-immobiliare/) — offerta API come presentata pubblicamente.

### SISTER

- [Valutazione catastale e OMI](valuation.md)
- [Quotazioni da annunci](../sister/market_quotes.py)
- [KPI e valori del patrimonio](../sister/land_registry_kpis.py)
- [Panoramica OMI per zona](../sister/omi_zone_overview.py)
- [Pagina di approfondimento OMI](../sister/templates/catasto/omi_valuation.html)
- [Riferimento mercato nella scheda catastale](../sister/templates/catasto/entity.html)
- [Salvataggio delle valutazioni per bene](../sister/db_models.py)
- [Lettura dei nomi dal database opendata](../sister/subject_names.py)
- [Client dei servizi immobiliari in opendata](../../opendata/opendata/openapi/realestate.py) — costruttori di richiesta, risposte in attesa, quotazioni e compravendite.
- [Rotte immobiliari di opendata](../../opendata/opendata/routers/realestate.py) — crediti, validazione e `request_status`.
- [Specifica Openapi immobiliare](../../opendata/configuration/realestate.openapi.json) — `IT-rm_value`, `IT-comparable`, `IT-sqm_value_*`, `IT-omi`, `IT-avg_sqm_value_history`.
- [Test delle query immobiliari](../../opendata/tests/test_realestate_payloads.py)
