"""Optional, confidence-gated TypeSafe Jev decisions for SISTER workflows.

Jev only receives a short description of a cadastral select control and its
candidate labels. It never receives raw HTML, screenshot bytes, fiscal codes,
property rows, or selected values. Calls are disabled unless both
SISTER_JEV_ENABLED and TYPESAFE_API_KEY are configured.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

logger = logging.getLogger("sister.jev")


def _load_environment() -> None:
    """Load project and shared-workspace .env files without overriding env vars."""
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env", override=False)
    load_dotenv(project_root.parents[1] / ".env", override=False)


_load_environment()

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
_SAFE_SELECTORS = {
    "select[name='listacom']": "cadastral province",
    "select[name='denomComune']": "cadastral municipality",
    "select[name='comuneCat']": "cadastral municipality",
    "select[name='sezione']": "cadastral section",
}
_DECISION_TASKS = {
    "select[name='listacom']": (
        "Choose the cadastral province/office requested by the user. If the requested label is "
        "NAZIONALE, choose only the nationwide option. Do not choose another province because "
        "its code or name shares a prefix or substring."
    ),
    "select[name='denomComune']": (
        "Choose the requested cadastral municipality (comune) from the options in this SISTER "
        "municipality selector. Match the municipality name at this level; do not substitute a "
        "province, district, similarly named comune, or a partial text match."
    ),
    "select[name='comuneCat']": (
        "Choose the requested cadastral municipality (comune) from the options in this SISTER "
        "municipality selector. Match the municipality name at this level; do not substitute a "
        "province, district, similarly named comune, or a partial text match."
    ),
    "select[name='sezione']": (
        "Choose the requested cadastral section (sezione) from this SISTER section selector. "
        "Match the section label/code, not a municipality, urban section, or unrelated number."
    ),
}
_DECISION_RULES = (
    "Return the ID of one supplied candidate only. Compare the complete requested label with "
    "the complete candidate labels, using the DOM control label and section label to confirm "
    "what this selector represents. Ignore case and surrounding whitespace; use an abbreviation "
    "or equivalent spelling only when it identifies one clear candidate. Do not infer or invent "
    "an unavailable location. If the candidates do not contain a clear match, report low "
    "confidence so the caller can stop for operator selection."
)
_FISCAL_CODE = re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b", re.IGNORECASE)
_VAT_NUMBER = re.compile(r"\b\d{11}\b")


def _contains_personal_identifier(value: str) -> bool:
    return bool(_FISCAL_CODE.search(value) or _VAT_NUMBER.search(value))


class AmbiguousOptionError(ValueError):
    """Raised when a tied cadastral option needs operator selection."""


class JevAbstentionError(AmbiguousOptionError):
    """Raised when Jev cannot confidently distinguish cadastral choices."""


@dataclass(frozen=True)
class JevChoice:
    """A validated choice, or an explicit abstention when confidence is low."""

    option_value: str | None
    confidence: float | None
    abstained: bool = False


def jev_enabled() -> bool:
    """Return true only when the operator explicitly enables Jev and has a key."""
    enabled = os.getenv("SISTER_JEV_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    return enabled and bool(os.getenv("TYPESAFE_API_KEY", "").strip())


def ambiguous_option_policy() -> str:
    """Return how a tie without an accepted Jev answer is handled.

    ``local`` (default) keeps the local top-ranked option; ``operator`` stops
    so an operator can choose. Unknown values fall back to ``local``.
    """
    value = os.getenv("SISTER_AMBIGUOUS_OPTION_POLICY", "local").strip().lower()
    return "operator" if value == "operator" else "local"


def _min_confidence() -> float:
    try:
        value = float(os.getenv("SISTER_JEV_MIN_CONFIDENCE", "0.95"))
    except ValueError:
        return 0.95
    return min(1.0, max(0.0, value))


def _page_path(page_url: str) -> str:
    """Keep only a short path hint; discard query strings and dynamic IDs."""
    path = urlsplit(page_url).path
    parts = ["{id}" if re.fullmatch(r"[A-Za-z0-9_-]{12,}", part) else part for part in path.split("/")]
    return "/".join(parts)[:160]


def _build_payload(
    *,
    selector: str,
    requested: str,
    page_url: str,
    candidates: list[tuple[str, str]],
    dom_context: dict[str, str] | None = None,
) -> tuple[dict, dict[str, str]]:
    """Build a typed choice request and a local option-ID map.

    Only allowlisted location dropdowns can reach the hosted model. Candidate
    values remain local; the request contains opaque IDs and their visible
    cadastral labels.
    """
    if selector not in _SAFE_SELECTORS:
        raise ValueError("Jev is restricted to cadastral location dropdowns")
    if not 2 <= len(candidates) <= 255:
        raise ValueError("Jev Choice requires between 2 and 255 candidates")
    if _contains_personal_identifier(requested) or any(
        _contains_personal_identifier(label) for _, label in candidates
    ):
        raise ValueError("Jev page description must not contain fiscal or VAT identifiers")

    candidate_ids: dict[str, str] = {}
    criteria: dict[str, str] = {}
    for index, (value, label) in enumerate(candidates):
        choice_id = f"option_{index}"
        candidate_ids[choice_id] = value
        criteria[choice_id] = label[:120]

    safe_dom_context = {}
    if dom_context:
        for key in ("role", "control_label", "section_label"):
            value = dom_context.get(key)
            if isinstance(value, str) and value.strip() and not _contains_personal_identifier(value):
                safe_dom_context[key] = value.strip()[:120]

    page_description = {
        "portal": "Agenzia delle Entrate SISTER",
        "page_path": _page_path(page_url),
        "control": _SAFE_SELECTORS[selector],
        "decision_required": _DECISION_TASKS[selector],
        "requested_label": requested[:120],
        "available_choices": list(criteria.values()),
    }
    if safe_dom_context:
        page_description["dom_context"] = safe_dom_context

    payload = {
        "state": {
            "page_description": page_description,
        },
        "model": MODEL,
        "questions": {
            "best_option": {
                "type": "choice",
                "instructions": _DECISION_RULES,
                "criteria": criteria,
            }
        },
    }
    return payload, candidate_ids


def _build_batch_payload(
    *,
    page_url: str,
    decisions: list[dict[str, Any]],
) -> tuple[dict, dict[str, dict[str, str]]]:
    """Build one Jev request containing independent choice questions for a page."""
    if not 1 <= len(decisions) <= 12:
        raise ValueError("A Jev page query requires between 1 and 12 decision points")

    question_ids: set[str] = set()
    description_decisions = []
    questions = {}
    candidate_maps: dict[str, dict[str, str]] = {}

    for decision in decisions:
        question_id = decision.get("id")
        selector = decision.get("selector")
        requested = decision.get("requested")
        candidates = decision.get("candidates")
        if not isinstance(question_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", question_id):
            raise ValueError("Jev decision IDs must be short lowercase identifiers")
        if question_id in question_ids:
            raise ValueError("Jev decision IDs must be unique within a page query")
        question_ids.add(question_id)
        if selector not in _SAFE_SELECTORS:
            raise ValueError("Jev is restricted to allowlisted cadastral location dropdowns")
        if not isinstance(requested, str) or not requested.strip():
            raise ValueError(f"Jev decision {question_id!r} needs a requested label")
        if not isinstance(candidates, list) or not 2 <= len(candidates) <= 255:
            raise ValueError("Each Jev choice requires between 2 and 255 candidates")
        if _contains_personal_identifier(requested) or any(
            not isinstance(candidate, (tuple, list))
            or len(candidate) != 2
            or not isinstance(candidate[1], str)
            or _contains_personal_identifier(candidate[1])
            for candidate in candidates
        ):
            raise ValueError("Jev page description must not contain fiscal or VAT identifiers")

        option_map: dict[str, str] = {}
        criteria: dict[str, str] = {}
        for index, (value, label) in enumerate(candidates):
            if not isinstance(value, str) or not isinstance(label, str):
                raise ValueError("Jev candidate values and labels must be strings")
            option_id = f"option_{index}"
            option_map[option_id] = value
            criteria[option_id] = label[:120]

        dom_context = decision.get("dom_context") or {}
        safe_dom_context = {}
        for key in ("role", "control_label", "section_label"):
            value = dom_context.get(key) if isinstance(dom_context, dict) else None
            if isinstance(value, str) and value.strip() and not _contains_personal_identifier(value):
                safe_dom_context[key] = value.strip()[:120]

        description = {
            "id": question_id,
            "control": _SAFE_SELECTORS[selector],
            "decision_required": _DECISION_TASKS[selector],
            "requested_label": requested[:120],
            "available_choices": list(criteria.values()),
        }
        if safe_dom_context:
            description["dom_context"] = safe_dom_context
        description_decisions.append(description)
        questions[question_id] = {
            "type": "choice",
            "instructions": f"{_DECISION_TASKS[selector]} {_DECISION_RULES}",
            "criteria": criteria,
        }
        candidate_maps[question_id] = option_map

    payload = {
        "state": {
            "page_description": {
                "portal": "Agenzia delle Entrate SISTER",
                "page_path": _page_path(page_url),
                "decision_points": description_decisions,
            }
        },
        "model": MODEL,
        "questions": questions,
    }
    return payload, candidate_maps


def _parse_choice(
    body: dict,
    candidate_ids: dict[str, str],
    question_id: str = "best_option",
) -> JevChoice | None:
    answer = body.get("answers", {}).get(question_id, {})
    choice_id = answer.get("choice")
    if answer.get("type") != "choice" or choice_id not in candidate_ids:
        return None

    try:
        confidence = float(answer.get("confidence", 0.0))
        probabilities = {key: float(value) for key, value in (answer.get("probabilities") or {}).items()}
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in probabilities.values()):
        return None

    selected_probability = probabilities.get(choice_id, 0.0)
    second_probability = max((value for key, value in probabilities.items() if key != choice_id), default=0.0)
    if (
        confidence < _min_confidence()
        or selected_probability < _min_confidence()
        or selected_probability - second_probability < 0.15
    ):
        return JevChoice(option_value=None, confidence=confidence, abstained=True)
    return JevChoice(option_value=candidate_ids[choice_id], confidence=confidence)


def describe_dom_context_from_html(html: str, selector: str) -> dict[str, str]:
    """Reduce the current DOM to the focused control's safe semantic labels.

    The source HTML remains local. Field values, option values, hidden inputs,
    table rows, body text, and screenshots are deliberately excluded.
    """
    if selector not in _SAFE_SELECTORS or not isinstance(html, str):
        return {}
    try:
        soup = BeautifulSoup(html, "html.parser")
        control = soup.select_one(selector)
    except Exception:
        return {}
    if control is None or control.has_attr("hidden") or control.get("aria-hidden") == "true":
        return {}
    for parent in control.parents:
        if parent.has_attr("hidden") or parent.get("aria-hidden") == "true":
            return {}
        style = re.sub(r"\s+", "", str(parent.get("style", "")).lower())
        if "display:none" in style or "visibility:hidden" in style:
            return {}

    def safe_text(value) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text or len(text) > 120 or _contains_personal_identifier(text):
            return ""
        if re.search(r"\d{7,}", text):
            return ""
        return text

    label = safe_text(control.get("aria-label"))
    if not label and control.get("id"):
        labels = soup.find_all("label", attrs={"for": control.get("id")})
        label = safe_text(" ".join(item.get_text(" ", strip=True) for item in labels))
    if not label:
        parent_label = control.find_parent("label")
        if parent_label:
            label = safe_text(parent_label.get_text(" ", strip=True))
    if not label:
        row = control.find_parent("tr")
        if row:
            cells = row.find_all(["th", "td"], recursive=False)
            if cells and cells[0] is not control and not cells[0].select_one(selector):
                label = safe_text(cells[0].get_text(" ", strip=True))

    section = ""
    fieldset = control.find_parent("fieldset")
    if fieldset:
        legend = fieldset.find("legend", recursive=False)
        section = safe_text(legend.get_text(" ", strip=True) if legend else "")

    result = {"role": "combobox", "control_label": label, "section_label": section}
    return {key: value for key, value in result.items() if value}


async def describe_live_dom(page, selector: str) -> dict[str, str]:
    """Read the live Playwright DOM locally and return only safe control labels."""
    if selector not in _SAFE_SELECTORS or page is None:
        return {}
    try:
        html = await page.content()
    except Exception as exc:
        logger.debug("Unable to read live SISTER DOM for Jev (%s)", type(exc).__name__)
        return {}
    return describe_dom_context_from_html(html, selector)


async def _query_jev(payload, candidate_maps, transport=None):
    """Make one Jev request and require a valid answer for every named question."""
    try:
        api_key = os.environ["TYPESAFE_API_KEY"].strip()
        timeout = float(os.getenv("SISTER_JEV_TIMEOUT_SECONDS", "4"))
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(
                API_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
        response.raise_for_status()
        body = response.json()
        decisions = {}
        for question_id, candidate_ids in candidate_maps.items():
            decision = _parse_choice(body, candidate_ids, question_id)
            if decision is None:
                logger.warning("Jev returned an invalid cadastral choice for %s; retaining local decision", question_id)
                return None
            if decision.abstained:
                logger.info(
                    "Jev abstained on cadastral decision %s (confidence %.3f)",
                    question_id,
                    decision.confidence,
                )
            decisions[question_id] = decision
        return decisions
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Jev cadastral choice unavailable (%s); retaining local decision", type(exc).__name__)
        return None


async def choose_cadastral_option(
    *,
    selector: str,
    requested: str,
    page_url: str,
    candidates: list[tuple[str, str]],
    page=None,
    _transport: httpx.AsyncBaseTransport | None = None,
) -> JevChoice | None:
    """Ask Jev to break one local fuzzy-match tie."""
    if not jev_enabled():
        return None

    try:
        dom_context = await describe_live_dom(page, selector) if page is not None else None
        payload, candidate_ids = _build_payload(
            selector=selector,
            requested=requested,
            page_url=page_url,
            candidates=candidates,
            dom_context=dom_context,
        )
        choices = await _query_jev(payload, {"best_option": candidate_ids}, _transport)
        return choices.get("best_option") if choices else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Jev cadastral choice unavailable (%s); retaining local decision", type(exc).__name__)
        return None


async def choose_cadastral_options(
    *,
    page_url: str,
    decisions: list[dict[str, Any]],
    page=None,
    _transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, JevChoice] | None:
    """Ask Jev several independent cadastral questions about one current page.

    Each decision has ``id``, ``selector``, ``requested``, and ``candidates``.
    All answers are validated before the caller receives the map, so a malformed
    or missing answer cannot cause a partial set of page selections.
    """
    if not jev_enabled():
        return None

    try:
        page_decisions = []
        for decision in decisions:
            item = dict(decision)
            selector = item.get("selector")
            if page is not None:
                item["dom_context"] = await describe_live_dom(page, selector)
            page_decisions.append(item)
        payload, candidate_maps = _build_batch_payload(page_url=page_url, decisions=page_decisions)
        return await _query_jev(payload, candidate_maps, _transport)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Jev page decisions unavailable (%s); retaining local decisions", type(exc).__name__)
        return None
