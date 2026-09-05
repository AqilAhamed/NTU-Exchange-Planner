"""Profile extraction with an LLM, layered over the deterministic pass.

Two rules keep this agent honest:

* **The fallback runs first and always.** The model merges over it and may only
  fill fields the regexes left blank. A model cannot overwrite a programme code
  that was matched against the database.
* **Every value the model returns is re-validated against the database.** A
  hallucinated programme ("Quantum Engineering") or country ("Wakanda") is
  dropped, not stored.

Context is bounded to the last few turns rather than the whole conversation.
The previous build concatenated the entire message history into every intake
prompt, which made the token cost of a long chat grow without limit.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agents.intake_fallback import Extraction, extract
from data.destinations import resolve_country
from data.programmes import by_code, resolve_programme
from graph.domain import Profile
from graph.providers import (
    LLMProvider,
    LLMRequest,
    provider_from_env,
    usage_counters,
)
from graph.terms import CANONICAL_TERMS, resolve_term

# How much conversation the model sees. Enough to resolve a short follow-up
# against the previous turn, bounded so cost does not grow with chat length.
MAX_HISTORY_TURNS = 4
MAX_CHARS_PER_TURN = 400
_CORRECTION_PATTERN = re.compile(
    r"\b(?:i\s+mean|meant|what\s+i\s+meant|correction|actually|"
    r"(?:not|rather)\s+[^.;!?]+\s+(?:but|instead))\b",
    re.IGNORECASE,
)
_CORRECTION_REPLACEMENT_PATTERN = re.compile(
    r"\b(?:change|replace|swap|correct|update|switch)\b[\s\S]{0,80}?"
    r"\b(?:to|with|for|instead\s+of|rather\s+than)\b",
    re.IGNORECASE,
)
_MODULE_ADDITION_PATTERN = re.compile(
    r"\b(?:add|also|plus|keep|include|including|append|retain|"
    r"alongside|along\s+with|in\s+addition(?:\s+to)?|as\s+well|too)\b",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """You extract a student's exchange-planning profile from their message.

Return ONLY a JSON object with these keys, using null when the student did not say:
  "programme": their NTU degree programme, as they wrote it (string or null)
  "term": one of {terms} (string or null)
  "programme_type": "GEMX" for overseas exchange or "SUSEP" for local (string or null)
  "destination": a single country, or null for a region like "Europe" or "anywhere"
  "named_university": a specific partner university they asked about (string or null)
  "cgpa": their CGPA on a 5.0 scale (number or null)
  "budget_sgd": a monthly budget in Singapore dollars (number or null)
  "destination_region": a region or continent such as Europe (string or null)
  "module_codes": NTU module codes the student wants included (array of strings)
  "add_module_codes": module codes to add to an existing module filter
    (array of strings)
  "remove_module_codes": module codes to remove from an existing module filter
    (array of strings)
  "excluded_module_types": mapping types the student wants omitted (array of strings)
  "included_module_types": mapping types the student wants kept (array of strings)
  "add_excluded_module_types": mapping types to add to the omitted set
  "remove_excluded_module_types": mapping types to restore from the omitted set
  "add_included_module_types": mapping types to add to an active keep-list
  "remove_included_module_types": mapping types to remove from the keep-list
  "show_module_types": mapping types to restore, such as "show BDEs"
  "clear_filters": true when the student explicitly asks to remove all active
    shortlist filters, otherwise false
  "clear_module_filters": true when the student asks for any module rather than
    the previously selected module/type restrictions, otherwise false

Rules:
- Treat the most recent message as the source of new facts. Earlier student
  messages may also be used to resolve a clear conversational reference.
- An explicit country or region in the most recent message replaces the
  previous destination scope. For example, if the student narrows a previous
  region to a country, return the country and leave ``destination_region``
  null; never repeat the older region as well. The same rule applies when a
  country is broadened to a region.
- Resolve an omitted referent only when it is unambiguous: location words such
  as "there", "that university", "that place" or "the same uni" refer to the
  latest specific university under discussion; phrases such as "that semester",
  "that period", "during then" or an equivalent time reference refer to the
  latest explicit exchange term. If there is no single clear referent, return
  null rather than guessing.
- Do not silently copy unrelated filters, modules, budget, CGPA or programme
  details into a new request. Those are carried by the application's structured
  session state only when the current request is a refinement of the same plan.
- Never guess. Null is the correct answer when the student did not say.
- A region ("Europe", "Asia", "anywhere") is NOT a country. Return null.
- A student does GEM Explorer or SUSEP, never both. SUSEP is only for
  Singapore/local exchange; a non-Singapore destination, region, or university
  is GEM Explorer. Infer that distinction when the location makes it clear.
- Do not infer a programme from a module code alone.
- A request for specific modules is a shortlist constraint, not the student's
  degree programme. Preserve every module code mentioned.
- Treat "only/just/limited to/restrict to [codes]" as a replacement module
  filter. Treat additive wording such as "add/include [codes] too", "also",
  "as well", "in addition", "alongside", "keep", "append" or "plus" as an
  incremental edit to the current filter. Do not return an incremental
  addition as a replacement list. If "too/as well/in addition" appears with
  "filter by" or similar wording, it still means add to the active filter
  unless the student explicitly says only/just/exclusively.
- A correction such as "I mean [new code]", "actually [new code]", "change X
  to Y", "replace X with Y", "swap X for Y", "Y instead of X" or "not X but
  Y" refers to the immediately preceding student turn. Replace only the
  corrected module edit, preserving older module codes and all other active
  filters. Use the add/remove delta fields rather than returning only the
  corrected code.
- The same replacement-versus-delta rule applies to mapping types. "Show
  BDEs" or "remove the BDE filter" restores BDE from an exclusion; it must not
  create a BDE-only whitelist when no whitelist was active. Restored types are
  additive to an active module-code filter, so "include BDE" brings back the
  BDE mappings alongside the previously requested module codes.
- Treat commands such as "remove filters", "clear my filters" or "start over"
  as explicit reset operations. Treat "any module", "any course" or "remove
  the module filter" as an explicit module-filter reset. These reset flags are
  different from an omitted field and must not be returned as null.
- Interpret natural-language mapping categories such as broadening electives,
  university electives and major professional electives as their Coursefinder
  types (BDE, UE and Major-PE).
- A budget is a maximum monthly SGD amount when the student says "only", "up
  to", "under", "maximum" or equivalent; do not invent a budget otherwise.
"""


def _bounded_history(history: list[dict[str, Any]] | None) -> list[tuple[str, str]]:
    """The student's own recent messages, each truncated.

    **Only the student's turns.** The assistant's replies are excluded on
    purpose: including them let the model read the system's own shortlist back
    as though the student had said it, and "extract" a destination the student
    never mentioned. A profile silently gains ``destination_pref: TAIWAN``
    because Taiwan happened to top the last list, and every later shortlist is
    then filtered to Taiwan without the student ever asking.
    """
    turns: list[tuple[str, str]] = []
    for entry in list(history or []):
        if entry.get("role") == "assistant":
            continue
        content = str(entry.get("content") or "")[:MAX_CHARS_PER_TURN]
        if content.strip():
            turns.append(("human", content))
    return turns[-MAX_HISTORY_TURNS:]


def _parse_json(raw: str) -> dict[str, Any]:
    """Read a JSON object out of a model response, tolerating fenced output."""
    text = (raw or "").strip()
    if not text:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return {}
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_float(value: Any, low: float, high: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if low < number <= high else None


def _validate(raw: dict[str, Any]) -> Extraction:
    """Turn model output into an Extraction, discarding anything unverifiable.

    Every field is checked against the database or the canonical term list, so
    a confident hallucination becomes a blank rather than a wrong shortlist.
    """
    found: list[str] = []
    result = Extraction()

    programme_text = raw.get("programme")
    if isinstance(programme_text, str) and programme_text.strip():
        programme = by_code(programme_text) or resolve_programme(programme_text)
        if programme is not None:
            result.school_code, result.school_name = programme.code, programme.name
            found.append("programme")

    term_text = raw.get("term")
    if isinstance(term_text, str) and term_text.strip():
        term = term_text.strip() if term_text.strip() in CANONICAL_TERMS else resolve_term(term_text)
        if term:
            result.preferred_semester = term
            found.append("term")

    kind = raw.get("programme_type")
    if isinstance(kind, str) and kind.strip().upper() in {"GEMX", "SUSEP"}:
        result.programme_type = kind.strip().upper()
        found.append("programme_type")

    destination = raw.get("destination")
    if isinstance(destination, str) and destination.strip():
        country = resolve_country(destination)
        if country:
            result.destination_pref = country
            found.append("destination")

    university = raw.get("named_university")
    if isinstance(university, str) and university.strip():
        from agents.intake_fallback import named_university

        matched = named_university(university)
        if matched:
            result.named_university = matched
            found.append("named_university")

    cgpa = _as_float(raw.get("cgpa"), 0.0, 5.0)
    if cgpa is not None:
        result.cgpa = cgpa
        found.append("cgpa")

    budget = _as_float(raw.get("budget_sgd"), 49.0, 1_000_000.0)
    if budget is not None:
        result.budget_sgd = budget
        found.append("budget")

    region = raw.get("destination_region")
    if isinstance(region, str) and region.strip():
        from data.destinations import resolve_region

        resolved = resolve_region(region)
        if resolved:
            result.destination_region, result.destination_countries = resolved
            found.append("destination_region")

    raw_codes = raw.get("module_codes")
    if isinstance(raw_codes, list):
        from agents.intake_fallback import _module_codes

        codes: list[str] = []
        for value in raw_codes:
            if isinstance(value, str):
                codes.extend(_module_codes(value))
        result.module_codes = list(dict.fromkeys(codes))
        if result.module_codes:
            found.append("module_codes")

    from agents.intake_fallback import _module_codes

    for field_name in ("add_module_codes", "remove_module_codes"):
        values = raw.get(field_name)
        if isinstance(values, list):
            codes: list[str] = []
            for value in values:
                if isinstance(value, str):
                    codes.extend(_module_codes(value))
            setattr(result, field_name, list(dict.fromkeys(codes)))
            if codes:
                found.append(field_name)

    from agents.intake_fallback import _types_in_phrase

    for field_name, target in (
        ("excluded_module_types", result.excluded_module_types),
        ("included_module_types", result.included_module_types),
        ("add_excluded_module_types", result.add_excluded_module_types),
        ("remove_excluded_module_types", result.remove_excluded_module_types),
        ("add_included_module_types", result.add_included_module_types),
        ("remove_included_module_types", result.remove_included_module_types),
        ("show_module_types", result.show_module_types),
    ):
        values = raw.get(field_name)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str):
                    for canonical in _types_in_phrase(value):
                        if canonical not in target:
                            target.append(canonical)
        if target:
            found.append(field_name)

    for field_name in ("clear_filters", "clear_module_filters"):
        if raw.get(field_name) is True:
            setattr(result, field_name, True)
            found.append(field_name)

    result.found = found
    return result


def _last_student_message(history: list[dict[str, Any]] | None) -> str:
    """Return the latest student turn available to resolve a correction."""
    for entry in reversed(list(history or [])):
        if entry.get("role") == "assistant":
            continue
        content = str(entry.get("content") or "").strip()
        if content:
            return content
    return ""


def _correction_pair(
    message: str,
    previous_codes: list[str],
) -> tuple[str, str] | None:
    """Find an explicit old-to-new module-code correction.

    The previous edit is the authority for the old side of a replacement.
    Requiring that side to be present prevents two unrelated codes in an
    additive list from being mistaken for a correction pair.
    """
    from agents.intake_fallback import _MODULE_CODE_PATTERN

    previous = {code.upper() for code in previous_codes}
    matches = list(_MODULE_CODE_PATTERN.finditer(message or ""))
    for left, right in zip(matches, matches[1:]):
        left_code = f"{left.group(1)}{left.group(2)}".upper()
        right_code = f"{right.group(1)}{right.group(2)}".upper()
        between = (message or "")[left.end() : right.start()]
        if re.search(r"\b(?:to|into|with|for|as)\b", between, re.IGNORECASE):
            old_code, new_code = left_code, right_code
        elif re.search(
            r"\b(?:instead\s+of|rather\s+than|not)\b", between, re.IGNORECASE
        ):
            new_code, old_code = left_code, right_code
        else:
            continue
        if old_code in previous and new_code != old_code:
            return old_code, new_code
    return None


def _apply_module_correction(
    message: str,
    history: list[dict[str, Any]] | None,
    deterministic: Extraction,
) -> Extraction:
    """Turn a correction into a delta against the immediately prior edit.

    This is intentionally based on the prior extraction's operation type, not
    on particular module codes or example wording. It therefore works for any
    module code and for both additive and replacement filter turns.
    """
    from agents.intake_fallback import _module_codes

    current_codes = _module_codes(message)
    previous_message = _last_student_message(history)
    if not current_codes or not previous_message:
        return deterministic

    previous = extract(previous_message)
    previous_codes = previous.add_module_codes or previous.module_codes or previous.remove_module_codes
    if not previous_codes:
        return deterministic

    pair = _correction_pair(message, previous_codes)
    has_correction_language = bool(
        _CORRECTION_PATTERN.search(message or "")
        or _CORRECTION_REPLACEMENT_PATTERN.search(message or "")
        or pair
    )
    # "Actually, include ... too" is an additive edit, not a correction of
    # the preceding edit. Let the deterministic add operation handle it.
    if not has_correction_language or (
        not pair
        and _MODULE_ADDITION_PATTERN.search(message or "")
        and not re.search(r"\b(?:instead\s+of|rather\s+than|not)\b", message or "", re.IGNORECASE)
    ):
        return deterministic

    # Preserve any explicitly stated delta from the current message only when
    # it is itself a removal. A correction of an add/replacement means
    # “remove the old value and add the corrected value”.
    if previous.remove_module_codes:
        deterministic.module_codes = []
        deterministic.add_module_codes = []
        deterministic.remove_module_codes = [pair[1]] if pair else list(dict.fromkeys(current_codes))
    else:
        deterministic.module_codes = []
        if pair:
            old_code, new_code = pair
            deterministic.remove_module_codes = [old_code]
            deterministic.add_module_codes = [new_code]
        else:
            previous_set = {code.upper() for code in previous_codes}
            deterministic.remove_module_codes = list(
                dict.fromkeys(deterministic.remove_module_codes + previous_codes)
            )
            deterministic.add_module_codes = list(
                dict.fromkeys(
                    deterministic.add_module_codes
                    + [code for code in current_codes if code not in previous_set]
                )
            )
    deterministic.found = [
        field
        for field in deterministic.found
        if field not in {"module_codes", "add_module_codes", "remove_module_codes"}
    ]
    deterministic.found.append("module_correction")
    if deterministic.add_module_codes:
        deterministic.found.append("add_module_codes")
    if deterministic.remove_module_codes:
        deterministic.found.append("remove_module_codes")
    return deterministic


def _merge(base: Extraction, overlay: Extraction) -> Extraction:
    """Fill omissions without letting stale conversational context win.

    ``base`` is extracted from the current turn and ``overlay`` is the LLM's
    interpretation of that turn in context. Most fields can safely use the
    overlay only when the deterministic pass found nothing. Destination scope
    is special: country and region are alternatives, and an explicit value in
    the current turn must clear the other alternative. Otherwise an LLM can
    copy the previous region into a follow-up that explicitly names a country.
    """
    # The deterministic parser is deliberately authoritative for explicit
    # scope changes. Keep the more specific current-turn country when a model
    # also echoes an older region; if the current turn states a region, keep
    # the region and discard any stale model country.
    clear_all = base.clear_filters or overlay.clear_filters
    clear_modules = clear_all or base.clear_module_filters or overlay.clear_module_filters
    has_filter_delta = any(
        (
            base.add_module_codes,
            base.remove_module_codes,
            base.add_excluded_module_types,
            base.remove_excluded_module_types,
            base.add_included_module_types,
            base.remove_included_module_types,
            base.show_module_types,
            base.replace_module_type_filters,
        )
    )
    if clear_all:
        destination_pref = base.destination_pref
        destination_region = base.destination_region
        destination_countries = list(base.destination_countries)
    elif base.destination_pref:
        destination_pref = base.destination_pref
        destination_region = None
        destination_countries: list[str] = []
    elif base.destination_region:
        destination_pref = None
        destination_region = base.destination_region
        destination_countries = list(base.destination_countries)
    else:
        destination_pref = overlay.destination_pref
        destination_region = overlay.destination_region
        destination_countries = list(overlay.destination_countries)

    merged = Extraction(
        school_code=base.school_code or overlay.school_code,
        school_name=base.school_name or overlay.school_name,
        preferred_semester=base.preferred_semester or overlay.preferred_semester,
        programme_type=base.programme_type or overlay.programme_type,
        destination_pref=destination_pref,
        named_university=base.named_university or (None if clear_all else overlay.named_university),
        university_candidates=(
            list(base.university_candidates)
            if base.university_candidates
            else ([] if clear_all else list(overlay.university_candidates))
        ),
        cgpa=base.cgpa if base.cgpa is not None else (None if clear_all else overlay.cgpa),
        budget_sgd=(
            base.budget_sgd
            if base.budget_sgd is not None
            else (None if clear_all else overlay.budget_sgd)
        ),
        destination_region=destination_region,
        destination_countries=destination_countries,
        # When the deterministic pass identified an edit, stale arrays from
        # the context-aware model are not allowed to turn an additive edit
        # into a replacement filter.
        module_codes=(
            base.module_codes
            or ([] if clear_modules or has_filter_delta else overlay.module_codes)
        ),
        excluded_module_types=(
            base.excluded_module_types
            or ([] if clear_modules or has_filter_delta else overlay.excluded_module_types)
        ),
        included_module_types=(
            base.included_module_types
            or ([] if clear_modules or has_filter_delta else overlay.included_module_types)
        ),
        restored_module_types=(
            base.restored_module_types
            or ([] if clear_modules or has_filter_delta else overlay.restored_module_types)
        ),
        add_module_codes=list(dict.fromkeys(base.add_module_codes + overlay.add_module_codes)),
        remove_module_codes=list(dict.fromkeys(base.remove_module_codes + overlay.remove_module_codes)),
        add_excluded_module_types=list(
            dict.fromkeys(base.add_excluded_module_types + overlay.add_excluded_module_types)
        ),
        remove_excluded_module_types=list(
            dict.fromkeys(base.remove_excluded_module_types + overlay.remove_excluded_module_types)
        ),
        add_included_module_types=list(
            dict.fromkeys(base.add_included_module_types + overlay.add_included_module_types)
        ),
        remove_included_module_types=list(
            dict.fromkeys(base.remove_included_module_types + overlay.remove_included_module_types)
        ),
        show_module_types=list(dict.fromkeys(base.show_module_types + overlay.show_module_types)),
        replace_module_type_filters=base.replace_module_type_filters
        or overlay.replace_module_type_filters,
        clear_filters=clear_all,
        clear_module_filters=base.clear_module_filters or overlay.clear_module_filters,
    )
    merged.found = sorted(set(base.found) | {f"llm:{f}" for f in overlay.found})
    return merged


def run(
    message: str,
    history: list[dict[str, Any]] | None = None,
    profile: Profile | None = None,
    provider: LLMProvider | None = None,
    named_university: str | None = None,
) -> tuple[Extraction, list[str], dict[str, int]]:
    """Extract a profile. Returns ``(extraction, warnings)``.

    Never raises. With no provider configured, or on any model or parse
    failure, the deterministic extraction is returned unchanged — the turn
    continues on regexes alone.
    """
    warnings: list[str] = []
    counters: dict[str, int] = {}
    deterministic = _apply_module_correction(
        message,
        history,
        extract(message, profile),
    )

    provider = provider or provider_from_env()
    if not provider.available():
        return deterministic, warnings, counters

    prompt = SYSTEM_PROMPT.format(terms=", ".join(f'"{t}"' for t in CANONICAL_TERMS))
    context_parts: list[str] = []
    if named_university:
        context_parts.append(f"university under discussion: {named_university}")
    if profile and profile.preferred_semester:
        context_parts.append(f"exchange term under discussion: {profile.preferred_semester}")
    if context_parts:
        messages: list[tuple[str, str]] = [
            ("system", prompt),
            (
                "system",
                "Structured carried context (use only to resolve clear references): "
                + "; ".join(context_parts),
            ),
        ]
    else:
        messages = [("system", prompt)]
    messages.extend(_bounded_history(history))
    messages.append(("human", (message or "")[:MAX_CHARS_PER_TURN * 2]))

    try:
        response = provider.invoke(
            LLMRequest(messages=tuple(messages), temperature=0.0, json_mode=True)
        )
    except Exception as exc:  # noqa: BLE001 - any model failure degrades the same way
        warnings.append(f"intake_llm_failed: {exc.__class__.__name__}")
        return deterministic, warnings, {"llm_calls": 1}

    counters = usage_counters(response)
    parsed = _parse_json(response.content)
    if not parsed:
        warnings.append("intake_llm_unparseable: falling back to deterministic extraction")
        return deterministic, warnings, counters

    return _merge(deterministic, _validate(parsed)), warnings, counters
