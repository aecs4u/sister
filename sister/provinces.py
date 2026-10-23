"""Italian provinces: sigla ↔ name, for the places the portal spells both ways.

The portal's forms take province *names* (``Roma``, ``Forlì-Cesena``) while the database stores the two-letter sigla
(``RM``). ``province_code`` accepts either, ignores case, accents and punctuation, and returns None for anything that is
not a province (for example the portal's ``NAZIONALE`` office level).

The table was generated from ISTAT's provinces (110 rows, 2026-02-21) so the application has no runtime dependency on
the ``istat`` schema. ``LEGACY`` lists spellings still found in stored portal data. A two-letter value is returned as it is (upper-cased) even
when ISTAT no longer lists it (the portal still prints ``SU`` for Sud Sardegna): stored places keep the sigla the portal gave.
"""

from __future__ import annotations

import re
import unicodedata

PROVINCES: dict[str, str] = {
    "AG": "Agrigento",
    "AL": "Alessandria",
    "AN": "Ancona",
    "AO": "Valle d'Aosta/Vallée d'Aoste",
    "AP": "Ascoli Piceno",
    "AQ": "L'Aquila",
    "AR": "Arezzo",
    "AT": "Asti",
    "AV": "Avellino",
    "BA": "Bari",
    "BG": "Bergamo",
    "BI": "Biella",
    "BL": "Belluno",
    "BN": "Benevento",
    "BO": "Bologna",
    "BR": "Brindisi",
    "BS": "Brescia",
    "BT": "Barletta-Andria-Trani",
    "BZ": "Bolzano/Bozen",
    "CA": "Cagliari",
    "CB": "Campobasso",
    "CE": "Caserta",
    "CH": "Chieti",
    "CI": "Sulcis Iglesiente",
    "CL": "Caltanissetta",
    "CN": "Cuneo",
    "CO": "Como",
    "CR": "Cremona",
    "CS": "Cosenza",
    "CT": "Catania",
    "CZ": "Catanzaro",
    "EN": "Enna",
    "FC": "Forlì-Cesena",
    "FE": "Ferrara",
    "FG": "Foggia",
    "FI": "Firenze",
    "FM": "Fermo",
    "FR": "Frosinone",
    "GE": "Genova",
    "GO": "Gorizia",
    "GR": "Grosseto",
    "IM": "Imperia",
    "IS": "Isernia",
    "KR": "Crotone",
    "LC": "Lecco",
    "LE": "Lecce",
    "LI": "Livorno",
    "LO": "Lodi",
    "LT": "Latina",
    "LU": "Lucca",
    "MB": "Monza e della Brianza",
    "MC": "Macerata",
    "ME": "Messina",
    "MI": "Milano",
    "MN": "Mantova",
    "MO": "Modena",
    "MS": "Massa-Carrara",
    "MT": "Matera",
    "NA": "Napoli",
    "NO": "Novara",
    "NU": "Nuoro",
    "OG": "Ogliastra",
    "OR": "Oristano",
    "OT": "Gallura Nord-Est Sardegna",
    "PA": "Palermo",
    "PC": "Piacenza",
    "PD": "Padova",
    "PE": "Pescara",
    "PG": "Perugia",
    "PI": "Pisa",
    "PN": "Pordenone",
    "PO": "Prato",
    "PR": "Parma",
    "PT": "Pistoia",
    "PU": "Pesaro e Urbino",
    "PV": "Pavia",
    "PZ": "Potenza",
    "RA": "Ravenna",
    "RC": "Reggio Calabria",
    "RE": "Reggio nell'Emilia",
    "RG": "Ragusa",
    "RI": "Rieti",
    "RM": "Roma",
    "RN": "Rimini",
    "RO": "Rovigo",
    "SA": "Salerno",
    "SI": "Siena",
    "SO": "Sondrio",
    "SP": "La Spezia",
    "SR": "Siracusa",
    "SS": "Sassari",
    "SV": "Savona",
    "TA": "Taranto",
    "TE": "Teramo",
    "TN": "Trento",
    "TO": "Torino",
    "TP": "Trapani",
    "TR": "Terni",
    "TS": "Trieste",
    "TV": "Treviso",
    "UD": "Udine",
    "VA": "Varese",
    "VB": "Verbano-Cusio-Ossola",
    "VC": "Vercelli",
    "VE": "Venezia",
    "VI": "Vicenza",
    "VR": "Verona",
    "VS": "Medio Campidano",
    "VT": "Viterbo",
    "VV": "Vibo Valentia",
}

# province spellings that stored portal data uses
LEGACY: dict[str, str] = {
    "FORLI CESENA": "FC",
    "MASSA CARRARA": "MS",
    "REGGIO EMILIA": "RE",
    "REGGIO NELL EMILIA": "RE",
    "PESARO URBINO": "PU",
    "MONZA BRIANZA": "MB",
    "MONZA E BRIANZA": "MB",
    "BOLZANO": "BZ",
    "BOZEN": "BZ",
    "AOSTA": "AO",
    "VALLE D AOSTA": "AO",
    "VERBANIA": "VB",
}


def _fold(value: str) -> str:
    """Upper case, accents removed, punctuation → single spaces: ``Forlì-Cesena`` → ``FORLI CESENA``."""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", text)).strip()


_BY_NAME: dict[str, str] = {}
for _code, _name in PROVINCES.items():
    _BY_NAME[_fold(_name)] = _code
    for _part in re.split(r"[/]", _name):  # 'Bolzano/Bozen', "Valle d'Aosta/Vallée d'Aoste"
        _BY_NAME.setdefault(_fold(_part), _code)
for _alias, _code in LEGACY.items():
    _BY_NAME.setdefault(_fold(_alias), _code)


def province_code(value: str | None) -> str | None:
    """The sigla for a sigla or a province name; None when ``value`` is empty or not a province."""
    if not value:
        return None
    folded = _fold(str(value))
    if not folded:
        return None
    if len(folded) == 2 and folded.isalpha():
        return folded
    return _BY_NAME.get(folded)


def province_name(code: str | None) -> str | None:
    """The province name for a sigla (``RM`` → ``Roma``)."""
    return PROVINCES.get((code or "").strip().upper())
