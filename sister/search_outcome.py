"""What a subject search ended with, from the error text of a request (one place for the batch tools, the progress bar, reports).

Learned on the bulk runs of 2026-10-26 (about 1,300 searches):

* ``no_match`` is SISTER's *answer*, not a failure: nobody with that code owns anything in the cadastre (or the code is not valid).
  It is final: asking again gets the same answer, so it is neither retried nor counted as an error.
* ``invalid_code`` is a ``no_match`` whose code has a wrong check character (see ``fiscal_code``): SISTER asks for a
  confirmation page and finds nobody anyway.
* ``too_many`` is SISTER's refusal to return a result that is too large, either the 400-row limit or its time-limit
  message asking for narrower criteria. It is final: more parameters would be needed, not another attempt.
* ``session_expired`` is the portal ending the session (about 30 minutes after the login): the run has to stop and wait for a new
  login, every further row would fail the same way.
* ``portal_error`` is an HTTP 500 answer of the portal for one subject: a repeat usually gets it again.
* ``form_timeout`` is a page that did not show an element in time: transient, worth one more attempt later.
"""

from __future__ import annotations

NO_MATCH = "no_match"
INVALID_CODE = "invalid_code"
TOO_MANY = "too_many"
SESSION_EXPIRED = "session_expired"
PORTAL_ERROR = "portal_error"
FORM_TIMEOUT = "form_timeout"
OTHER_ERROR = "error"
FOUND = "found"
PENDING = "pending"

TOO_MANY_MARKER = "Superato il limite massimo di 400 elementi"
TOO_MANY_ERROR = f"{TOO_MANY_MARKER}. Aggiungere altri parametri per la ricerca"
CRITERIA_TIMEOUT_MARKER = "Il sistema non riesce a produrre la risposta nei limiti di tempo previsti"
CRITERIA_TIMEOUT_ERROR = f"{CRITERIA_TIMEOUT_MARKER}. Affinare i criteri di ricerca."

# outcomes that will not change if the search is repeated
FINAL = {FOUND, NO_MATCH, INVALID_CODE, TOO_MANY}

_SESSION_MARKERS = ("sessione_scaduta", "lock.html", "Sessione SISTER scaduta", "Sessione scaduta")


def search_refinement_error(error: str | None) -> str | None:
    """Normalize SISTER's final responses that require narrower search criteria."""
    normalized = " ".join((error or "").split()).casefold()
    if TOO_MANY_MARKER.casefold() in normalized:
        return TOO_MANY_ERROR
    if CRITERIA_TIMEOUT_MARKER.casefold() in normalized:
        return CRITERIA_TIMEOUT_ERROR
    return None


def classify(status: str | None, error: str | None = None) -> str:
    """The outcome of a request/journal row from its ``status`` and ``error`` text."""
    error = error or ""
    if search_refinement_error(error):
        return TOO_MANY
    if status == "completed":
        return FOUND
    if "NESSUNA CORRISPONDENZA" in error:
        return INVALID_CODE if "non valido" in error else NO_MATCH
    if "Superato il limite massimo" in error:
        return TOO_MANY
    if any(marker in error for marker in _SESSION_MARKERS):
        return SESSION_EXPIRED
    if status in ("queued", "running", "incomplete", None):
        return PENDING
    if "Error 500" in error or "NullPointerException" in error:
        return PORTAL_ERROR
    if "Timeout" in error and "exceeded" in error:
        return FORM_TIMEOUT
    return OTHER_ERROR
