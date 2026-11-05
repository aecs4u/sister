"""Which subject rows of a batch plan SISTER has already answered, per kind of query.

Used by ``sister batch run-session`` and ``sister batch prune-list`` to drop rows before a session, so nothing answered is
asked twice. The database queries live in ``command_tools/run_in_session.answered_queries``; this module holds the rules,
without side effects, so they can be tested.

A code answered by one kind of query is not answered for another:

* a **search** (``soggetto``, ``soggetto-immobili``, ``azienda``) is answered by an earlier search of that code over
  Fabbricati and Terreni that found something or got SISTER's "nessuna corrispondenza", or by "oltre 400" for a code. A
  stored visura per soggetto does not count: it is the document of one office, not the national list of properties;
* a **document** (``soggetto-documento``, paid) is answered by an earlier document request of that code with the same
  view (analitica / sintetica), or by a stored ``vs_`` document of that view; for a row limited to one province, of that
  province. A code SISTER answered "nessuna corrispondenza" has no document to request either.

A row asking for the Storica is never treated as answered: the database does not record which "Tipo richiesta" an answer
had, so an Attualità answer would hide it. Rows of any other command are never dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .provinces import province_code
from .query_forms import get_query_form

SEARCH = "search"
DOCUMENT = "document"
_KINDS = {"soggetto": SEARCH, "soggetto-immobili": SEARCH, "azienda": SEARCH, "soggetto-documento": DOCUMENT}
NATIONAL = "*"  # a document request for the whole country (one document per province where the subject has assets)

# vs_<view>_<code>[_<province>][_<comune code>]_<CE|CF|CT>_<date>[_<practice id>] (utils._unique_document_stem)
_VS_NAME = re.compile(
    r"^vs_(?P<view>[a-z]+)_(?P<code>[A-Z0-9]{11,16})(?P<middle>(?:_[A-Z0-9]+)*?)_(?:CE|CF|CT)_\d{4}-\d{2}-\d{2}", re.I
)
_VIEWS = {"att": "analitica", "sin": "sintetica", "sto": "storica"}


def query_kind(command: str | None) -> str | None:
    """``search`` / ``document`` for a subject query (aliases resolved), else None."""
    form = get_query_form(command or "")
    return _KINDS.get(form.command) if form else None


def row_code(params: dict[str, Any]) -> str:
    return str(params.get("codice_fiscale") or params.get("identificativo") or "").strip().upper()


def document_view(params: dict[str, Any]) -> str:
    return "sintetica" if str(params.get("vista") or "").strip().lower() == "sintetica" else "analitica"


def _province(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.upper() == "NAZIONALE":
        return NATIONAL
    return (province_code(text) or text).upper()


def parse_vs_name(name: str) -> tuple[str, str, str] | None:
    """(code, view, province or ``*``) of a stored visura per soggetto file name, else None."""
    match = _VS_NAME.match(name or "")
    if not match:
        return None
    view = _VIEWS.get(match.group("view").lower())
    if view is None:
        return None
    middle = [token for token in match.group("middle").split("_") if token]
    province = middle[0].upper() if middle and len(middle[0]) == 2 and middle[0].isalpha() else NATIONAL
    return match.group("code").upper(), view, province


@dataclass
class Answered:
    searches: set[str] = field(default_factory=set)
    no_match: set[str] = field(default_factory=set)  # SISTER's "nessuna corrispondenza": nothing to search or request
    documents: set[tuple[str, str, str]] = field(default_factory=set)  # (code, view, province or "*")

    @classmethod
    def build(
        cls,
        *,
        searches: Iterable[str] = (),
        no_match: Iterable[str] = (),
        documents: Iterable[tuple[str, str, Iterable[str]]] = (),
        files: Iterable[str] = (),
    ) -> "Answered":
        """``documents`` = (code, vista, provinces requested) of each answered document request."""
        answered = cls(
            searches={str(code).strip().upper() for code in searches if code},
            no_match={str(code).strip().upper() for code in no_match if code},
        )
        for code, vista, provinces in documents:
            code, view = str(code).strip().upper(), document_view({"vista": vista})
            answered.documents.add((code, view, NATIONAL))
            answered.documents.update((code, view, _province(p)) for p in provinces or () if p)
        for name in files:
            if parsed := parse_vs_name(name):
                answered.documents.add(parsed)
        return answered

    def __contains__(self, code: object) -> bool:
        """``code in answered``: the legacy codici lists only hold subject searches."""
        return str(code).strip().upper() in self.searches

    def covers(self, command: str | None, params: dict[str, Any]) -> bool:
        """Whether this row's answer is already known (see the module docstring)."""
        code = row_code(params)
        kind = query_kind(command)
        if not code or kind is None:
            return False
        if str(params.get("tipo_richiesta") or "").strip().lower() == "storica":
            return False
        if kind == SEARCH:
            return code in self.searches
        if code in self.no_match:
            return True
        view, province = document_view(params), _province(params.get("provincia"))
        if province == NATIONAL:  # any document of that view: a national request makes one per province
            return any(c == code and v == view for c, v, _ in self.documents)
        return (code, view, province) in self.documents
