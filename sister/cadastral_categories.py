"""Descriptions of the cadastral categories of the Catasto dei Fabbricati (D.M. 2 gennaio 1998, n. 28, and later additions).

The visura gives the code, sometimes followed by the portal's own description (``"A/2: ABITAZIONI DI TIPO CIVILE"``), and the
``cadastral_category`` table only has some of them, so the table of the subject pages gets its "Descrizione categoria"
from this complete list. Keys are the canonical codes of :func:`cadastral_values.normalize_category` (``"A2"``).
"""

from __future__ import annotations

from typing import Any

from .cadastral_values import normalize_category

CATEGORY_DESCRIPTIONS: dict[str, str] = {
    # Gruppo A: unità immobiliari a destinazione ordinaria (abitazioni, uffici)
    "A1": "Abitazioni di tipo signorile",
    "A2": "Abitazioni di tipo civile",
    "A3": "Abitazioni di tipo economico",
    "A4": "Abitazioni di tipo popolare",
    "A5": "Abitazioni di tipo ultrapopolare",
    "A6": "Abitazioni di tipo rurale",
    "A7": "Abitazioni in villini",
    "A8": "Abitazioni in ville",
    "A9": "Castelli, palazzi di eminenti pregi artistici o storici",
    "A10": "Uffici e studi privati",
    "A11": "Abitazioni ed alloggi tipici dei luoghi",
    # Gruppo B: unità per uso di alloggi collettivi, scuole, uffici pubblici
    "B1": "Collegi e convitti, educandati, ricoveri, orfanotrofi, ospizi, conventi, seminari, caserme",
    "B2": "Case di cura ed ospedali (senza fine di lucro)",
    "B3": "Prigioni e riformatori",
    "B4": "Uffici pubblici",
    "B5": "Scuole e laboratori scientifici",
    "B6": "Biblioteche, pinacoteche, musei, gallerie, accademie (non in edifici A/9)",
    "B7": "Cappelle ed oratori non destinati all'esercizio pubblico dei culti",
    "B8": "Magazzini sotterranei per depositi di derrate",
    # Gruppo C: unità a destinazione ordinaria commerciale e varia
    "C1": "Negozi e botteghe",
    "C2": "Magazzini e locali di deposito",
    "C3": "Laboratori per arti e mestieri",
    "C4": "Fabbricati e locali per esercizi sportivi (senza fine di lucro)",
    "C5": "Stabilimenti balneari e di acque curative (senza fine di lucro)",
    "C6": "Stalle, scuderie, rimesse e autorimesse (senza fine di lucro)",
    "C7": "Tettoie chiuse o aperte",
    # Gruppo D: unità a destinazione speciale
    "D1": "Opifici",
    "D2": "Alberghi e pensioni (con fine di lucro)",
    "D3": "Teatri, cinematografi, sale per concerti e spettacoli (con fine di lucro)",
    "D4": "Case di cura ed ospedali (con fine di lucro)",
    "D5": "Istituti di credito, cambio ed assicurazione (con fine di lucro)",
    "D6": "Fabbricati e locali per esercizi sportivi (con fine di lucro)",
    "D7": "Fabbricati per speciali esigenze di un'attività industriale, non adattabili senza radicali trasformazioni",
    "D8": "Fabbricati per speciali esigenze di un'attività commerciale, non adattabili senza radicali trasformazioni",
    "D9": "Edifici galleggianti o sospesi assicurati al suolo; ponti privati soggetti a pedaggio",
    "D10": "Fabbricati per funzioni produttive connesse alle attività agricole",
    # Gruppo E: unità a destinazione particolare
    "E1": "Stazioni per servizi di trasporto, terrestri, marittimi ed aerei",
    "E2": "Ponti comunali e provinciali soggetti a pedaggio",
    "E3": "Costruzioni e fabbricati per speciali esigenze pubbliche",
    "E4": "Recinti chiusi per speciali esigenze pubbliche",
    "E5": "Fabbricati costituenti fortificazioni e loro dipendenze",
    "E6": "Fari, semafori, torri per rendere d'uso pubblico l'orologio comunale",
    "E7": "Fabbricati destinati all'esercizio pubblico dei culti",
    "E8": "Fabbricati e costruzioni nei cimiteri (esclusi colombari, sepolcri e tombe di famiglia)",
    "E9": "Edifici a destinazione particolare non compresi nelle altre categorie del gruppo E",
    # Gruppo F: entità urbane senza rendita
    "F1": "Area urbana",
    "F2": "Unità collabente",
    "F3": "Unità in corso di costruzione",
    "F4": "Unità in corso di definizione",
    "F5": "Lastrico solare",
    "F6": "Fabbricato in attesa di dichiarazione",
    "F7": "Infrastrutture di reti pubbliche di comunicazione",
}


def category_description(category: Any) -> str:
    """Description of a visura's ``Categoria`` (``"A/2"``, ``"A02"``, ``"A/2: ABITAZIONI DI TIPO CIVILE"``), or ``""``.

    The list above wins; for a code it does not know, the description the visura itself carries after the colon is used.
    """
    code = normalize_category(category)
    if code in CATEGORY_DESCRIPTIONS:
        return CATEGORY_DESCRIPTIONS[code]
    text = str(category or "")
    if ":" in text:
        own = " ".join(text.split(":", 1)[1].split())
        return own[:1].upper() + own[1:].lower() if own else ""
    return ""
