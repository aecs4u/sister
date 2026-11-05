# Master NPL partners: Partita IVA research

Research completed 2026-11-05 against all 89 rows in `master_npl.partners`. The P.IVA column records the current Italian VAT number where it could be tied to the partner. Group IVA numbers are shared by distinct companies, so the entity CF column and SISTER lookup ID retain the company-level identity. The final column links the evidence used for the match.

For SISTER, the lookup ID is the legal entity's 11-digit CF when it differs from a shared Group IVA or from the partner's P.IVA; otherwise it is the P.IVA. Iovis is excluded because the only number found belongs to an associated law office and its relationship to the partner identity is unconfirmed. Eight rows have no confirmed Italian P.IVA. Guber and Guber Banca resolve to one legal company. That leaves 79 unique SISTER lookup rows.

| # | Partner row | Matched legal entity | Current P.IVA / Group IVA | Entity CF when different | SISTER lookup ID | Match / note | Source |
|---:|---|---|---|---|---|---|---|
| 1 | AB CarVal | — | — | — | — | No Italian entity or Italian P.IVA identified; excluded. | [AB CarVal](https://www.abcarval.com/us/en-us/investors/home.html) |
| 2 | Abilio | Abilio S.p.A. | 12020720962 (Group IVA) | 02704840392 | 02704840392 | Confirmed; Group IVA shared with illimity. | [Abilio terms](https://www.abilio.com/termini-e-condizioni/) |
| 3 | Agecredit Srl | Agecredit S.r.l. | 10977720969 (Group IVA) | 02316040407 | 02316040407 | Confirmed; Group IVA shared with KRUK Italia. | [Agecredit legal/privacy notice](https://agecredit.it/wp-content/themes/agecredit/assets/files/privacy-e-note-legali-privacy-and-cookies.pdf) |
| 4 | Altilia | Altilia S.r.l. | 10780921002 | — | 10780921002 | Confirmed. | [Altilia](https://www.altilia.ai/) |
| 5 | A&M | Alvarez & Marsal Italia S.r.l. | 04414860967 | — | 04414860967 | Confirmed; P.IVA corroborated by company directory. | [A&M Italy](https://www.alvarezandmarsal.com/global-locations/italy), [company record](https://www.ufficiocamerale.it/3916/alvarez-marsal-italia-srl-e-in-forma-abbreviata-ami-srl) |
| 6 | AMCO - Asset Management | AMCO S.p.A. | 05828330638 | — | 05828330638 | Confirmed. | [Gazzetta Ufficiale](https://www.gazzettaufficiale.it/eli/gu/2020/01/09/4/p2/pdf) |
| 7 | Aquileia Capital Services S.r.l. | Aquileia Capital Services S.p.A. | 02338310309 | — | 02338310309 | Confirmed. | [Gazzetta Ufficiale](https://www.gazzettaufficiale.it/eli/gu/2019/07/18/84/p2/pdf) |
| 8 | Axactor Italy | Axactor Italy S.p.A. | 02417100043 | — | 02417100043 | Confirmed. | [Axactor contacts](https://www.axactor.com/it/contattaci) |
| 9 | Axis Advisory & Asset Management | — | — | — | — | Exact legal entity match and P.IVA not confirmed; excluded. | [supplied partner website](https://axis-advisors.com/) |
| 10 | AZInfo&Collection | A-Zeta Info & Collection S.p.A. | 05892470633 | — | 05892470633 | Confirmed. | [A-Zeta contacts](https://www.azinfocollection.it/contatti/), [public procurement notice](https://adsptirrenocentrale.it/wp-content/uploads/2021/11/determina-SG-n.-141-del-04.11.2021.pdf) |
| 11 | Banca Finint | Banca Finint S.p.A. | 04977190265 (Group IVA) | 04040580963 | 04040580963 | Confirmed; shared group number. | [Banca Finint contacts](https://private-wealth.bancafinint.com/contatti/) |
| 12 | Banca Ifis | Banca Ifis S.p.A. | 04570150278 (Group IVA) | 02505630109 | 02505630109 | Confirmed; shared group number. | [company information](https://www.bancaifis.it/en/company-information/), [Group IVA notice](https://www.bancaifis.it/news/adesione-gruppo-iva-1-gennaio-2020/) |
| 13 | Banco BPM | Banco BPM S.p.A. | 10537050964 (Group IVA) | 09722490969 | 09722490969 | Confirmed; shared group number. | [Banco BPM corporate data](https://gruppo.bancobpm.it/dati-societari/) |
| 14 | Banco Desio | Banco di Desio e della Brianza S.p.A. | 10537880964 (Group IVA) | 01181770155 | 01181770155 | Confirmed; shared group number. | [Banco Desio transparency](https://www.bancodesio.it/it/trasparenza) |
| 15 | BCredit Srl | BCredit S.r.l. | 15492951007 | — | 15492951007 | Confirmed; P.IVA corroborated by LEI/company record. | [BCredit](https://www.bcredit.it/), [LEI record](https://lei.bloomberg.com/leis/view/98450075B52E3A391C51) |
| 16 | BF.direkt AG | BF.direkt AG | — | — | — | German VAT ID DE197316375, not an Italian P.IVA; excluded. | [BF.direkt legal notice](https://www.bf-direkt.de/impressum/) |
| 17 | BHW Bausparkasse AG | BHW Bausparkasse AG, Italian branch | 01666200215 | — | 01666200215 | Confirmed. | [BHW Italy](https://www.bhw.it/) |
| 18 | Blue Factor S.p.A. | Blue Factor S.p.A. | 01426870539 | — | 01426870539 | Confirmed. | [Blue Factor privacy policy](https://bluefactor.eu/privacy-policy/) |
| 19 | BNL BNP Paribas | BNL BNP Paribas S.p.A. | 09339391006 | — | 09339391006 | Confirmed. | [BNL corporate data](https://bnl.it/it/Footer/Dati-Societari) |
| 20 | Borgosesia spa | Borgosesia S.p.A. | 12582130964 (Group IVA) | 00554840017 | 00554840017 | Confirmed; current group number, with company CF for SISTER. | [Borgosesia Group IVA](https://borgosesiaspa.it/gruppo-iva/) |
| 21 | Business Defence | Business Defence S.r.l. | 07780410960 | — | 07780410960 | Confirmed. | [Business Defence contacts](https://www.businessdefence.it/it/contatti) |
| 22 | CdB Avvocati Law Firm | — | — | — | — | Exact billing entity and P.IVA not confirmed; excluded. | [CdB Avvocati](https://www.cdbavvocati.com/) |
| 23 | Cerved Credit Management | Cerved Credit Management S.p.A. | 12022630961 (Group IVA) | 06374460969 | 06374460969 | Confirmed; Cerved Group IVA. | [Cerved Group IVA](https://www.cerved.com/gruppo-iva), [Cerved offices/legal entities](https://cerved.com/le-sedi) |
| 24 | Cerved Group SpA | Cerved Group S.p.A. | 12022630961 (Group IVA) | 08587760961 | 08587760961 | Confirmed; Cerved Group IVA. | [Cerved Group IVA](https://www.cerved.com/gruppo-iva), [Cerved offices/legal entities](https://cerved.com/le-sedi) |
| 25 | Cerved Legal Services Srl | Cerved Legal Services S.r.l. | 12022630961 (Group IVA) | 07082190963 | 07082190963 | Confirmed; Cerved Group IVA. | [Cerved Group IVA](https://www.cerved.com/gruppo-iva), [Cerved offices/legal entities](https://cerved.com/le-sedi) |
| 26 | Cerved Master Services | Cerved Master Services S.p.A. | 12022630961 (Group IVA) | 09842960966 | 09842960966 | Confirmed; Cerved Group IVA. | [Cerved Group IVA](https://www.cerved.com/gruppo-iva), [Cerved offices/legal entities](https://cerved.com/le-sedi) |
| 27 | Cerved Property Services Italy s.r.l. | Cerved Property Services Italy S.r.l. | 12022630961 (Group IVA) | 12082970968 | 12082970968 | Confirmed; group IVA from Cerved, entity CF corroborated by company record. | [Cerved Group IVA](https://www.cerved.com/gruppo-iva), [company record](https://www.ufficiocamerale.it/1100/cerved-property-services-italy-srl) |
| 28 | Cherry bank | Cherry Bank S.p.A. | 04147080289 | — | 04147080289 | Confirmed. | [Cherry Bank account information](https://www.cherrybank.it/wp-content/uploads/2023/03/M380-FIA-CC-ONLINE002_Conto-corrente-online_20032023.pdf) |
| 29 | Cherry Legal | Cherry Legal S.r.l. | 11269980964 | — | 11269980964 | Confirmed. | [Cherry Legal privacy policy](https://www.cherrylegal.it/privacy-policy/) |
| 30 | Coldwell Banker Commercial | Realty Advisory S.p.A. | 08117460967 | — | 08117460967 | Confirmed; operator/legal entity for the brand. | [Coldwell Banker Commercial privacy policy](https://cbcommercial.it/privacy-policy/) |
| 31 | Crediciò srl | Crediciò S.r.l. | 13698121004 | — | 13698121004 | Confirmed. | [Crediciò privacy policy](https://credicio.it/privacy-policy/) |
| 32 | CreditVision | CreditVision S.r.l. | 11855921000 | — | 11855921000 | Confirmed. | [CreditVision privacy notice](https://www.creditvision.it/wp-content/uploads/2025/04/CV_privacy.pdf) |
| 33 | CRIBIS Credit Management | CRIBIS Credit Management S.r.l. | 04133770372 | — | 04133770372 | Confirmed. | [CRIBIS contacts](https://www.cribiscreditmanagement.it/contatti/) |
| 34 | Cyberguru | Cyber Guru S.p.A. | 14324511006 | — | 14324511006 | Confirmed; brand is transitioning to LibraCyber. | [Cyber Guru management](https://www.cyberguru.it/en/management-investors/) |
| 35 | D21.4 | D21.4 S.r.l. | 10994050960 | — | 10994050960 | Confirmed. | [D21.4 terms](https://www.d214.it/D214_Regolamento.pdf) |
| 36 | DIDIMORA | Didimora S.r.l. | 11829510962 | — | 11829510962 | Confirmed. | [Didimora terms](https://www.didimora.com/termini-e-condizioni) |
| 37 | DUEPUNTOZERO | Duepuntozero S.r.l. | 09244530961 | — | 09244530961 | Confirmed for the NPL partner; distinguished from unrelated similarly named businesses. | [Gazzetta Ufficiale](https://www.gazzettaufficiale.it/eli/id/2019/12/28/TX19AAB13948/p2) |
| 38 | Edrasis Group | Edrasis Group S.r.l. | 04861560235 | — | 04861560235 | Confirmed; P.IVA from company record. | [Edrasis Group](https://www.edrasisgroup.com), [company record](https://www.ufficiocamerale.it/5542/edrasis-group-srl) |
| 39 | Ernst and Young | EY S.p.A. | 00891231003 | 00434000584 | 00434000584 | Confirmed; use entity CF, not its different P.IVA, for SISTER. | [EY Italian legal entities](https://www.ey.com/content/dam/ey-unified-site/ey-com/it-it/legal-and-privacy/documents/elenco-legal-entity-ey-ottobre2025.pdf) |
| 40 | Europa Centro Servizi | Europa Centro Servizi S.r.l. | 03687850838 | — | 03687850838 | Confirmed. | [Europa Centro Servizi contacts](https://www.europacentroservizi.it/contatti/) |
| 41 | Europa Investimenti | Europa Investimenti S.p.A. | 11407600961 (Group IVA) | 09439410151 | 09439410151 | Confirmed; current Group IVA is shared; use company CF/own P.IVA for SISTER. | [Gazzetta Ufficiale, 2025](https://www.gazzettaufficiale.it/eli/gu/2025/09/04/105/p2/pdf) |
| 42 | Finint Private Bank | Finint Private Bank S.p.A. | 04977190265 (Group IVA) | 01733820037 | 01733820037 | Confirmed; shared group number. | [Banca Finint Group IVA/contact details](https://private-wealth.bancafinint.com/contatti/), [PagoPA PSP list](https://www.pagopa.gov.it/it/prestatori-servizi-di-pagamento/elenco-PSP-attivi/) |
| 43 | Finware | — | — | — | — | Exact legal entity match and P.IVA not confirmed; excluded. | No reliable exact match found. |
| 44 | FIRE S.p.A. | FIRE S.p.A. | 01787570835 | — | 01787570835 | Confirmed. | [FIRE corporate data](https://fire.eu/dati-societari?l=it) |
| 45 | Fortnes S.p.A. | Fortnes S.p.A. | 04754250654 | — | 04754250654 | Confirmed. | [ESU Verona public document](https://www.esu.vr.it/html/uploads/2026/01/DD_n-291-del-30.12.2025.pdf) |
| 46 | Freshfields Bruckhaus Deringer | Freshfields Bruckhaus Deringer, Italian branch | 06184830963 | 97494540152 | 97494540152 | Confirmed; use branch CF for SISTER. | [Freshfields Italian legal notice](https://www.freshfields.com/en/footer/legal-notices/italy-legal-notice), [government letter](https://www.pariopportunita.gov.it/media/xrgl3nfa/freshfields-lettera.pdf) |
| 47 | Galileo & Partners SpA | Galileo & Partners S.p.A. | 12276671000 | — | 12276671000 | Confirmed; P.IVA corroborated by company record. | [Galileo business units](https://galileoandpartners.com/business-unit/), [company record](https://www.ufficiocamerale.it/6743/galileo-partners-spa) |
| 48 | Generalfinance S.p.A. | Generalfinance S.p.A. | 01363520022 | — | 01363520022 | Confirmed. | [Generalfinance company data](https://generalfinance.eu/it/dati-societari/) |
| 49 | Gruppo BCC ICCREA | Iccrea Banca S.p.A. | 15240741007 (Group IVA) | 04774801007 | 04774801007 | Confirmed; holding company used for the partner group. | [Iccrea Banca](https://www.iccreabanca.it/) |
| 50 | Guber | Guber Banca S.p.A. | 03140600176 | — | 03140600176 | Confirmed; same legal entity as Guber Banca row. | [Guber who we are](https://guber.it/chi-siamo/) |
| 51 | Guber Banca | Guber Banca S.p.A. | 03140600176 | — | — | Confirmed duplicate alias of row 50; one SISTER lookup only. | [Guber who we are](https://guber.it/chi-siamo/) |
| 52 | Hogan Lovells | Hogan Lovells Cadwalader Società tra Avvocati S.r.l. | 13217010159 | — | 13217010159 | Confirmed current successor/rebranded Italian entity (2026). | [Current EU VAT details](https://www.hlc.com/en/legal-notices/eu-vat-numbers), [regulatory information](https://www.hlc.com/en/legal-notices/regulatory-information) |
| 53 | Hoist Finance Italia | Hoist Italia S.r.l. | 15846791000 (Group IVA) | 12898671008 | 12898671008 | Confirmed; use entity CF for SISTER. | [Hoist Finance Italia](https://www.hoistfinance.it/) |
| 54 | i-law Studio Legale | i-law Studio Legale | 10147800964 | — | 10147800964 | Confirmed. | [i-law firm profile](https://i-law.it/lo-studio/) |
| 55 | illimity | illimity Bank S.p.A. | 12020720962 (Group IVA) | 03192350365 | 03192350365 | Confirmed; Group IVA shared with Abilio. | [illimity company profile](https://www.illimity.com/it/chi-siamo) |
| 56 | ILS | ILS S.r.l. | 11668690966 | — | 11668690966 | Confirmed. | [ILS Milan](https://www.ilsmilan.it/) |
| 57 | Interxen | — | — | — | — | Exact legal entity match and P.IVA not confirmed; excluded. | No reliable exact match found. |
| 58 | Intesa Sanpaolo | Intesa Sanpaolo S.p.A. | 11991500015 (Group IVA) | 00799960158 | 00799960158 | Confirmed; shared group number. | [Intesa Sanpaolo corporate data](https://group.intesasanpaolo.com/it/pagine-footer/dati-sociali) |
| 59 | Intrum | Intrum Italy S.p.A. | 10973410961 (Group IVA) | 10311000961 | 10311000961 | Confirmed; shared group number. | [Intrum Italy company information](https://www.intrum.it/le-nostre-soluzioni-per-le-aziende/about-intrum/intrum-italy-publ/) |
| 60 | Iovis | Studio IOVIS / Studio Legale Raffaella Greco association | 02105560789 (associated office only) | — | — | Identity/P.IVA belongs to an associated office; not confirmed as Iovis' own legal entity, excluded. | [Studio IOVIS-associated office](https://www.raffaellagreco.it/), [contacts](https://www.raffaellagreco.it/contatti-studio-raffaella-greco/) |
| 61 | iQera Italia S.p.A. | iQera Italia S.p.A. | 07710101002 | — | 07710101002 | Confirmed. | [iQera Italia](https://iqera.it/) |
| 62 | Kervis Credit Management | Kervis Credit Management S.r.l. | 10498340966 | — | 10498340966 | Confirmed; P.IVA from company record. | [company record](https://www.ufficiocamerale.it/6225/kervis-credit-management-srl) |
| 63 | KRUK Italia S.r.l. | KRUK Italia S.r.l. | 10977720969 (Group IVA) | 09270260962 | 09270260962 | Confirmed; Group IVA shared with Agecredit. | [KRUK company information](https://it.kruk.eu/business-partner/informazioni-sulla-societa) |
| 64 | La Scala Cerved | La Scala Cerved S.r.l. | 10140950964 | — | 10140950964 | Confirmed. | [La Scala Cerved](https://lascalacerved.com/), [government letter](https://www.pariopportunita.gov.it/media/5qfjag2r/la-scala-cerved-lettera.pdf) |
| 65 | Luigi Luzzatti S.C.p.A. | Luigi Luzzatti S.C.p.A. | 14573781003 | — | 14573781003 | Confirmed. | [Luigi Luzzatti company document](https://www.luigiluzzatti.it/wp-content/uploads/2025/11/Luzatti-CS-Nuovo-Presidente_28-03-2023.pdf) |
| 66 | Manteia | Manteia-Memar S.r.l. | 11038990963 | — | 11038990963 | Confirmed; current company name. | [Manteia privacy policy](https://www.manteia.it/privacy-policy) |
| 67 | Mattei & Partners Studio Legale e Notarile | Mattei & Partners Studio Legale SA (Switzerland) | — | — | — | Swiss entity; no Italian P.IVA identified, excluded. | [Mattei & Partners](https://studiomattei.ch/) |
| 68 | Mercatoria SpA | Mercatoria S.p.A. | 01194520076 | — | 01194520076 | Confirmed. | [Mercatoria](https://mercatoriaspa.com), [Gazzetta Ufficiale](https://www.gazzettaufficiale.it/eli/id/2025/01/18/TX25AAB496/p2) |
| 69 | MFLaw Società tra Avvocati per Azioni | MFLaw S.t.A. | 06392021009 | — | 06392021009 | Confirmed. | [MFLaw](https://mflaw.it/), [contacts](https://mflaw.it/contatti/) |
| 70 | Multiversity Group | Multiversity S.p.A. | 15438171009 | — | 15438171009 | Confirmed. | [Multiversity legal/privacy document](https://assets.ctfassets.net/5bcqzxwt09xw/2jMz8AHA26xJIzvm69CUzE/a91eca558a5631096225cbcf89e69958/Press_Release_Multiversity_chatbot_ENG.pdf) |
| 71 | myNPL.it | myNPL S.r.l. | 15626391005 | — | 15626391005 | Confirmed. | [myNPL contacts](https://mynpl.it/index.php/contattaci) |
| 72 | Nardone D'Attorre Improta Oliviero & Partners | Nardone D'Attorre Improta Oliviero & Partners S.t.A. | 09161841219 | — | 09161841219 | Confirmed. | [firm partner profile](https://www.ndiolex.it/personnel/antonio-nardone/) |
| 73 | NextLegal | NextLegal S.r.l. | 09811450965 | — | 09811450965 | Confirmed. | [NextLegal contacts](https://nextlegal.it/contatti/) |
| 74 | OFFICINE CST SpA | Officine CST S.p.A. | 08018981004 | — | 08018981004 | Confirmed; P.IVA corroborated by LEI record. | [LEI record](https://lei.bloomberg.com/leis/view/815600BE734CA886BC26) |
| 75 | Openapi | Openapi S.p.A. | 12485671007 | — | 12485671007 | Confirmed. | [Openapi investor relations](https://openapi.it/investor-relations) |
| 76 | Prelios Credit Servicing | Prelios Credit Servicing S.p.A. | 08360630159 | — | 08360630159 | Confirmed. | [Prelios operating companies](https://prelios.com/it/societa-operative/prelios-credit-servicing/profilo) |
| 77 | PWC Italy | PricewaterhouseCoopers S.p.A. | 12979880155 | — | 12979880155 | Confirmed. | [PwC S.p.A. legal/privacy notice](https://www.pwc.com/it/it/legal-privacy/informative-privacy/antiriciclaggio-antiterrorismo/pwc-spa.html) |
| 78 | Revela Srl | Revela S.r.l. | 08196310968 | — | 08196310968 | Confirmed. | [Revela](https://revela.srl/) |
| 79 | Servizisicuri.com S.r.l. | Servizisicuri.com S.r.l. | 08215860969 | — | 08215860969 | Confirmed. | [Servizisicuri company profile](https://www.servizisicuri.com/chi-siamo/) |
| 80 | Sole 24 ORE Formazione | Sole 24 ORE Formazione S.p.A. | 12609420968 | — | 12609420968 | Confirmed. | [Sole 24 ORE Formazione legal notice](https://sole24oreformazione.it/docenti/giovanna-liberali) |
| 81 | SYDEMA Srl | SYDEMA S.r.l. | 04829360157 | — | 04829360157 | Confirmed. | [SYDEMA offices](https://www.sydema.it/chi-siamo/sede/) |
| 82 | TRIPLE A MANAGEMENT DI DIPALO VALENTINA | Triple A Management di Dipalo Valentina | 08567070720 | — | 08567070720 | Confirmed; sole proprietorship, not a limited company. | [Triple A Management](https://tripleamanagement.it/chi-siamo/) |
| 83 | Tyche S.p.A. | Tyche Bank S.p.A. (successor) | 03104290832 | — | 03104290832 | Confirmed current successor after merger; predecessor P.IVA 03835770409 retired on incorporation. | [Gazzetta Ufficiale merger notice](https://www.gazzettaufficiale.it/eli/id/2024/08/24/TX24AAB8769/P2), [current company notice](https://www.gazzettaufficiale.it/eli/id/2026/05/14/TX26AAB4936/p2) |
| 84 | UnicorNpl srl | UnicorNpl S.r.l. | — | — | — | Legal entity matched but no public P.IVA found; excluded pending confirmation. | [UnicorNpl company profile](https://www.unicornpl.com/chi-siamo/) |
| 85 | Volksbank · Banca Popolare dell’Alto Adige | Banca Popolare dell’Alto Adige S.p.A. | 00129730214 | — | 00129730214 | Confirmed. | [Volksbank corporate data](https://www.volksbank.it/it/dati-societari) |
| 86 | Wikicasa | Wikicasa S.r.l. | 08820060963 | — | 08820060963 | Confirmed. | [Wikicasa privacy/legal notice](https://www.iubenda.com/privacy-policy/65845589/legal) |
| 87 | YARD REAAS | YARD REAAS S.p.A. | 12682830158 | 02774920967 | 02774920967 | Confirmed; use entity CF for SISTER. | [Fiera Milano related-party disclosure](https://www.fieramilano.it/content/dam/fieramilano/documenti/lp-investor-relations/en/corporate-governance/part-correlate/Information%20paper%20%2B%20annexes%20A%20and%20B_20211220.pdf) |
| 88 | Zenith | Zenith Global S.p.A. | 11407600961 (Group IVA) | 02200990980 | 02200990980 | Confirmed current group number; use entity CF for SISTER. | [Gazzetta Ufficiale, 2026](https://www.gazzettaufficiale.it/eli/id/2026/06/25/TX26AAB6333/P2) |
| 89 | [blank partner name] | Wibeats S.r.l. | 11958400019 | — | 11958400019 | Confirmed entity from the website URL on the partner row; source name is blank in the database. | [Wibeats contacts](https://www.wibeats.it/contact-us) |

## SISTER run-list

The marked table below is the only executable table in the corresponding input file. It contains the 79 distinct confirmed identifiers. For group IVA members, the `identificativo` is the entity CF so SISTER can search the individual legal company. Rows without a confirmed Italian identifier and the ambiguous Iovis match are not included.

<!-- sister-batch-table {"command": "azienda", "params": {"tipo_catasto": "E"}} -->
| id | identificativo | intestatario | fonte |
|---|---|---|---|
| master-npl-abilio-02704840392 | `02704840392` | Abilio S.p.A. (Group IVA 12020720962) | https://www.abilio.com/termini-e-condizioni/ |
| master-npl-agecredit-02316040407 | `02316040407` | Agecredit S.r.l. (Group IVA 10977720969) | https://agecredit.it/wp-content/themes/agecredit/assets/files/privacy-e-note-legali-privacy-and-cookies.pdf |
| master-npl-altilia-10780921002 | `10780921002` | Altilia S.r.l. | https://www.altilia.ai/ |
| master-npl-am-04414860967 | `04414860967` | Alvarez & Marsal Italia S.r.l. | https://www.ufficiocamerale.it/3916/alvarez-marsal-italia-srl-e-in-forma-abbreviata-ami-srl |
| master-npl-amco-05828330638 | `05828330638` | AMCO S.p.A. | https://www.gazzettaufficiale.it/eli/gu/2020/01/09/4/p2/pdf |
| master-npl-aquileia-02338310309 | `02338310309` | Aquileia Capital Services S.p.A. | https://www.gazzettaufficiale.it/eli/gu/2019/07/18/84/p2/pdf |
| master-npl-axactor-02417100043 | `02417100043` | Axactor Italy S.p.A. | https://www.axactor.com/it/contattaci |
| master-npl-azeta-05892470633 | `05892470633` | A-Zeta Info & Collection S.p.A. | https://adsptirrenocentrale.it/wp-content/uploads/2021/11/determina-SG-n.-141-del-04.11.2021.pdf |
| master-npl-finint-04040580963 | `04040580963` | Banca Finint S.p.A. (Group IVA 04977190265) | https://private-wealth.bancafinint.com/contatti/ |
| master-npl-ifis-02505630109 | `02505630109` | Banca Ifis S.p.A. (Group IVA 04570150278) | https://www.bancaifis.it/en/company-information/ |
| master-npl-bancobpm-09722490969 | `09722490969` | Banco BPM S.p.A. (Group IVA 10537050964) | https://gruppo.bancobpm.it/dati-societari/ |
| master-npl-bancodesio-01181770155 | `01181770155` | Banco di Desio e della Brianza S.p.A. (Group IVA 10537880964) | https://www.bancodesio.it/it/trasparenza |
| master-npl-bcredit-15492951007 | `15492951007` | BCredit S.r.l. | https://www.bcredit.it/ |
| master-npl-bhw-01666200215 | `01666200215` | BHW Bausparkasse AG, Italian branch | https://www.bhw.it/ |
| master-npl-bluefactor-01426870539 | `01426870539` | Blue Factor S.p.A. | https://bluefactor.eu/privacy-policy/ |
| master-npl-bnl-09339391006 | `09339391006` | BNL BNP Paribas S.p.A. | https://bnl.it/it/Footer/Dati-Societari |
| master-npl-borgosesia-00554840017 | `00554840017` | Borgosesia S.p.A. (Group IVA 12582130964) | https://borgosesiaspa.it/gruppo-iva/ |
| master-npl-business-defence-07780410960 | `07780410960` | Business Defence S.r.l. | https://www.businessdefence.it/it/contatti |
| master-npl-cerved-credit-management-06374460969 | `06374460969` | Cerved Credit Management S.p.A. (Group IVA 12022630961) | https://www.cerved.com/gruppo-iva; https://cerved.com/le-sedi |
| master-npl-cerved-group-08587760961 | `08587760961` | Cerved Group S.p.A. (Group IVA 12022630961) | https://www.cerved.com/gruppo-iva; https://cerved.com/le-sedi |
| master-npl-cerved-legal-07082190963 | `07082190963` | Cerved Legal Services S.r.l. (Group IVA 12022630961) | https://www.cerved.com/gruppo-iva; https://cerved.com/le-sedi |
| master-npl-cerved-master-09842960966 | `09842960966` | Cerved Master Services S.p.A. (Group IVA 12022630961) | https://www.cerved.com/gruppo-iva; https://cerved.com/le-sedi |
| master-npl-cerved-property-12082970968 | `12082970968` | Cerved Property Services Italy S.r.l. (Group IVA 12022630961) | https://www.cerved.com/gruppo-iva; https://www.ufficiocamerale.it/1100/cerved-property-services-italy-srl |
| master-npl-cherrybank-04147080289 | `04147080289` | Cherry Bank S.p.A. | https://www.cherrybank.it/wp-content/uploads/2023/03/M380-FIA-CC-ONLINE002_Conto-corrente-online_20032023.pdf |
| master-npl-cherrylegal-11269980964 | `11269980964` | Cherry Legal S.r.l. | https://www.cherrylegal.it/privacy-policy/ |
| master-npl-cbc-08117460967 | `08117460967` | Realty Advisory S.p.A. (Coldwell Banker Commercial) | https://cbcommercial.it/privacy-policy/ |
| master-npl-credicio-13698121004 | `13698121004` | Crediciò S.r.l. | https://credicio.it/privacy-policy/ |
| master-npl-creditvision-11855921000 | `11855921000` | CreditVision S.r.l. | https://www.creditvision.it/wp-content/uploads/2025/04/CV_privacy.pdf |
| master-npl-cribis-04133770372 | `04133770372` | CRIBIS Credit Management S.r.l. | https://www.cribiscreditmanagement.it/contatti/ |
| master-npl-cyberguru-14324511006 | `14324511006` | Cyber Guru S.p.A. | https://www.cyberguru.it/en/management-investors/ |
| master-npl-d214-10994050960 | `10994050960` | D21.4 S.r.l. | https://www.d214.it/D214_Regolamento.pdf |
| master-npl-didimora-11829510962 | `11829510962` | Didimora S.r.l. | https://www.didimora.com/termini-e-condizioni |
| master-npl-duepuntozero-09244530961 | `09244530961` | Duepuntozero S.r.l. (NPL company) | https://www.gazzettaufficiale.it/eli/id/2019/12/28/TX19AAB13948/p2 |
| master-npl-edrasis-04861560235 | `04861560235` | Edrasis Group S.r.l. | https://www.ufficiocamerale.it/5542/edrasis-group-srl |
| master-npl-ey-00434000584 | `00434000584` | EY S.p.A. (P.IVA 00891231003) | https://www.ey.com/content/dam/ey-unified-site/ey-com/it-it/legal-and-privacy/documents/elenco-legal-entity-ey-ottobre2025.pdf |
| master-npl-ecs-03687850838 | `03687850838` | Europa Centro Servizi S.r.l. | https://www.europacentroservizi.it/contatti/ |
| master-npl-europa-investimenti-09439410151 | `09439410151` | Europa Investimenti S.p.A. (Group IVA 11407600961) | https://www.gazzettaufficiale.it/eli/gu/2025/09/04/105/p2/pdf |
| master-npl-finint-private-bank-01733820037 | `01733820037` | Finint Private Bank S.p.A. (Group IVA 04977190265) | https://private-wealth.bancafinint.com/contatti/ |
| master-npl-fire-01787570835 | `01787570835` | FIRE S.p.A. | https://fire.eu/dati-societari?l=it |
| master-npl-fortnes-04754250654 | `04754250654` | Fortnes S.p.A. | https://www.esu.vr.it/html/uploads/2026/01/DD_n-291-del-30.12.2025.pdf |
| master-npl-freshfields-97494540152 | `97494540152` | Freshfields Bruckhaus Deringer, Italian branch (P.IVA 06184830963) | https://www.freshfields.com/en/footer/legal-notices/italy-legal-notice |
| master-npl-galileo-12276671000 | `12276671000` | Galileo & Partners S.p.A. | https://www.ufficiocamerale.it/6743/galileo-partners-spa |
| master-npl-generalfinance-01363520022 | `01363520022` | Generalfinance S.p.A. | https://generalfinance.eu/it/dati-societari/ |
| master-npl-iccrea-04774801007 | `04774801007` | Iccrea Banca S.p.A. (Group IVA 15240741007) | https://www.iccreabanca.it/ |
| master-npl-guber-03140600176 | `03140600176` | Guber Banca S.p.A. (Guber and Guber Banca aliases) | https://guber.it/chi-siamo/ |
| master-npl-hogan-lovells-13217010159 | `13217010159` | Hogan Lovells Cadwalader S.t.A. S.r.l. | https://www.hlc.com/en/legal-notices/eu-vat-numbers |
| master-npl-hoist-12898671008 | `12898671008` | Hoist Italia S.r.l. (Group IVA 15846791000) | https://www.hoistfinance.it/ |
| master-npl-ilaw-10147800964 | `10147800964` | i-law Studio Legale | https://i-law.it/lo-studio/ |
| master-npl-illimity-03192350365 | `03192350365` | illimity Bank S.p.A. (Group IVA 12020720962) | https://www.illimity.com/it/chi-siamo |
| master-npl-ils-11668690966 | `11668690966` | ILS S.r.l. | https://www.ilsmilan.it/ |
| master-npl-intesa-00799960158 | `00799960158` | Intesa Sanpaolo S.p.A. (Group IVA 11991500015) | https://group.intesasanpaolo.com/it/pagine-footer/dati-sociali |
| master-npl-intrum-10311000961 | `10311000961` | Intrum Italy S.p.A. (Group IVA 10973410961) | https://www.intrum.it/le-nostre-soluzioni-per-le-aziende/about-intrum/intrum-italy-publ/ |
| master-npl-iqera-07710101002 | `07710101002` | iQera Italia S.p.A. | https://iqera.it/ |
| master-npl-kervis-10498340966 | `10498340966` | Kervis Credit Management S.r.l. | https://www.ufficiocamerale.it/6225/kervis-credit-management-srl |
| master-npl-kruk-09270260962 | `09270260962` | KRUK Italia S.r.l. (Group IVA 10977720969) | https://it.kruk.eu/business-partner/informazioni-sulla-societa |
| master-npl-lascala-10140950964 | `10140950964` | La Scala Cerved S.r.l. | https://www.pariopportunita.gov.it/media/5qfjag2r/la-scala-cerved-lettera.pdf |
| master-npl-luzzatti-14573781003 | `14573781003` | Luigi Luzzatti S.C.p.A. | https://www.luigiluzzatti.it/wp-content/uploads/2025/11/Luzatti-CS-Nuovo-Presidente_28-03-2023.pdf |
| master-npl-manteia-11038990963 | `11038990963` | Manteia-Memar S.r.l. | https://www.manteia.it/privacy-policy |
| master-npl-mercatoria-01194520076 | `01194520076` | Mercatoria S.p.A. | https://www.gazzettaufficiale.it/eli/id/2025/01/18/TX25AAB496/p2 |
| master-npl-mflaw-06392021009 | `06392021009` | MFLaw S.t.A. | https://mflaw.it/contatti/ |
| master-npl-multiversity-15438171009 | `15438171009` | Multiversity S.p.A. | https://assets.ctfassets.net/5bcqzxwt09xw/2jMz8AHA26xJIzvm69CUzE/a91eca558a5631096225cbcf89e69958/Press_Release_Multiversity_chatbot_ENG.pdf |
| master-npl-mynpl-15626391005 | `15626391005` | myNPL S.r.l. | https://mynpl.it/index.php/contattaci |
| master-npl-ndio-09161841219 | `09161841219` | Nardone D'Attorre Improta Oliviero & Partners S.t.A. | https://www.ndiolex.it/personnel/antonio-nardone/ |
| master-npl-nextlegal-09811450965 | `09811450965` | NextLegal S.r.l. | https://nextlegal.it/contatti/ |
| master-npl-officinecst-08018981004 | `08018981004` | Officine CST S.p.A. | https://lei.bloomberg.com/leis/view/815600BE734CA886BC26 |
| master-npl-openapi-12485671007 | `12485671007` | Openapi S.p.A. | https://openapi.it/investor-relations |
| master-npl-prelios-08360630159 | `08360630159` | Prelios Credit Servicing S.p.A. | https://prelios.com/it/societa-operative/prelios-credit-servicing/profilo |
| master-npl-pwc-12979880155 | `12979880155` | PricewaterhouseCoopers S.p.A. | https://www.pwc.com/it/it/legal-privacy/informative-privacy/antiriciclaggio-antiterrorismo/pwc-spa.html |
| master-npl-revela-08196310968 | `08196310968` | Revela S.r.l. | https://revela.srl/ |
| master-npl-servizisicuri-08215860969 | `08215860969` | Servizisicuri.com S.r.l. | https://www.servizisicuri.com/chi-siamo/ |
| master-npl-sole24-12609420968 | `12609420968` | Sole 24 ORE Formazione S.p.A. | https://sole24oreformazione.it/docenti/giovanna-liberali |
| master-npl-sydema-04829360157 | `04829360157` | SYDEMA S.r.l. | https://www.sydema.it/chi-siamo/sede/ |
| master-npl-triplea-08567070720 | `08567070720` | Triple A Management di Dipalo Valentina | https://tripleamanagement.it/chi-siamo/ |
| master-npl-tychebank-03104290832 | `03104290832` | Tyche Bank S.p.A. (successor to Tyche S.p.A.) | https://www.gazzettaufficiale.it/eli/id/2024/08/24/TX24AAB8769/P2 |
| master-npl-volksbank-00129730214 | `00129730214` | Banca Popolare dell’Alto Adige S.p.A. | https://www.volksbank.it/it/dati-societari |
| master-npl-wikicasa-08820060963 | `08820060963` | Wikicasa S.r.l. | https://www.iubenda.com/privacy-policy/65845589/legal |
| master-npl-yard-02774920967 | `02774920967` | YARD REAAS S.p.A. (P.IVA 12682830158) | https://www.fieramilano.it/content/dam/fieramilano/documenti/lp-investor-relations/en/corporate-governance/part-correlate/Information%20paper%20%2B%20annexes%20A%20and%20B_20211220.pdf |
| master-npl-zenith-02200990980 | `02200990980` | Zenith Global S.p.A. (Group IVA 11407600961) | https://www.gazzettaufficiale.it/eli/id/2026/06/25/TX26AAB6333/P2 |
| master-npl-wibeats-11958400019 | `11958400019` | Wibeats S.r.l. (partner name blank in DB) | https://www.wibeats.it/contact-us |
