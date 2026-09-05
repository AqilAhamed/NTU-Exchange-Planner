"""Deterministic profile extraction. Runs on every turn, with or without an LLM.

This is the floor the system stands on. It always runs first; the LLM intake
agent then merges *over* it, so a missing API key costs polish, not function.
Every field it produces is one a regular expression can defend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from data.coursefinder_db import all_university_names, match_universities
from data.destinations import (
    is_recognized_country_name,
    resolve_country,
    resolve_region,
)
from data.programmes import by_code, resolve_programme
from graph.domain import Profile
from graph.terms import resolve_term

GEMX = "GEMX"
SUSEP = "SUSEP"

# A student does GEM or SUSEP, never both, so these are mutually exclusive.
_SUSEP_PATTERN = re.compile(r"\bsusep\b|\blocal exchange\b|\bwithin singapore\b", re.IGNORECASE)
_GEMX_PATTERN = re.compile(r"\bgem\b|\bgem explorer\b|\bgemx\b|\boverseas exchange\b", re.IGNORECASE)

# "CGPA 4.2", "my gpa is 3.85", "4.1/5.0 cgpa"
_CGPA_PATTERN = re.compile(
    r"\b(?:c?gpa)\b[^0-9]{0,12}(\d(?:\.\d{1,2})?)|(\d(?:\.\d{1,2})?)\s*(?:/\s*\d(?:\.\d)?)?\s*\bc?gpa\b",
    re.IGNORECASE,
)

# "budget of 2k a month", "SGD 1,500", "$2000 per month", "under 1.5k"
_BUDGET_PATTERN = re.compile(
    r"(?:sgd|s\$|\$|budget(?:\s+of)?|under|around|about|max(?:imum)?)\s*"
    r"(\d[\d,]*(?:\.\d+)?)\s*(k|m)?\b",
    re.IGNORECASE,
)
_BUDGET_CONTEXT_PATTERN = re.compile(
    r"(?:monthly|per\s+month|a\s+month|month(?:ly)?\s+budget|spend(?:ing)?\s+limit)"
    r"[^\d$]{0,16}(?:sgd|s\$|\$)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|m)?\b",
    re.IGNORECASE,
)

# Module identifiers are data-shaped rather than question-shaped. Requiring
# digits avoids mistaking a degree code such as CSC or a word such as BDE for
# an NTU module, while allowing common spacing such as "SC 4000".
_MODULE_CODE_PATTERN = re.compile(r"\b([A-Za-z]{2,5})\s*[- ]?\s*(\d{3,5}[A-Za-z]?)\b")
_MODULE_PREFIX_STOPWORDS = frozenset(
    {"about", "after", "from", "have", "into", "is", "just", "like", "make", "only", "per", "show", "that", "the", "this", "with"}
)

_TYPE_ALIASES = {
    "bde": "BDE",
    "bdes": "BDE",
    "broadening elective": "BDE",
    "broadening electives": "BDE",
    "broadening education": "BDE",
    "ue": "UE",
    "ues": "UE",
    "university elective": "UE",
    "university electives": "UE",
    "core": "Core",
    "core module": "Core",
    "core modules": "Core",
    "major pe": "Major-PE",
    "major-pe": "Major-PE",
    "major professional elective": "Major-PE",
    "major professional electives": "Major-PE",
    "2nd spec pe": "2nd Spec-PE",
    "second spec pe": "2nd Spec-PE",
}
_TYPE_PHRASES = tuple(sorted(_TYPE_ALIASES, key=len, reverse=True))
_NEGATION_PATTERN = re.compile(
    r"(?:ignore|exclude|excluding|omit|omitting|remove|removing|without|"
    r"leave\s+out|drop|don't\s+(?:show|want|need|include|display|return)|"
    r"do\s+not\s+(?:show|want|need|include|display|return)|not|no)"
    r"\s+(?:all\s+)?(?:the\s+)?(?P<types>[^.;,!?]+)",
    re.IGNORECASE,
)
_ONLY_TYPE_PATTERN = re.compile(
    r"(?:only|just|limited\s+to|restrict(?:ed)?\s+to)\s+"
    r"(?P<types>[^.;,!?]*?)(?=\s+(?:without|except|but|and\s+not)\b|[.;,!?]|$)",
    re.IGNORECASE,
)

# Reset commands are state operations, not missing values. Without explicit
# markers, a normal merge cannot distinguish "the student did not mention a
# filter" from "the student asked to remove that filter".
_CLEAR_ALL_FILTERS_PATTERN = re.compile(
    r"\b(?:remove|clear|reset|drop|discard|forget)\s+(?:all\s+)?"
    r"(?:the\s+|my\s+|current\s+|active\s+)?filters?\b"
    r"|\b(?:without|with\s+no)\s+(?:any\s+)?filters?\b",
    re.IGNORECASE,
)
_CLEAR_MODULE_FILTER_PATTERN = re.compile(
    r"\b(?:any|all|whatever)\s+(?:mods?|modules?|courses?)\b"
    r"|\b(?:remove|clear|reset|drop|discard|forget)\s+(?:the\s+|my\s+)?"
    r"(?:module|mod|course)(?:s)?(?:\s+(?:filter|filters|restriction|restrictions))?\b"
    r"|\bwithout\s+(?:a\s+)?(?:module|mod|course)\s+(?:filter|restriction)s?\b",
    re.IGNORECASE,
)
_ADD_MODULE_CODE_PATTERN = re.compile(
    r"\b(?:add|also|plus|keep|include|including|append|retain|"
    r"alongside|along\s+with|in\s+addition(?:\s+to)?|as\s+well|too)\b",
    re.IGNORECASE,
)
_REMOVE_MODULE_CODE_PATTERN = re.compile(
    r"\b(?:remove|delete|drop|exclude|omit|without)\b"
    r"|\b(?:don't|do\s+not)\s+(?:show|include|want|need)\b",
    re.IGNORECASE,
)
_SHOW_MODULE_TYPE_PATTERN = re.compile(
    r"\b(?:show|restore|bring\s+back|allow|keep|include)\b\s+"
    r"(?!(?:only|just|limited\s+to|restrict(?:ed)?\s+to)\b)"
    r"(?:the\s+|my\s+|all\s+)?(?P<types>[^.;,!?]+)",
    re.IGNORECASE,
)
_RESTORE_TYPE_FILTER_PATTERN = re.compile(
    r"\b(?:remove|clear|reset|drop)\b\s+(?:the\s+|my\s+)?"
    r"(?P<types>[^.;,!?]+?)\s+(?:filter|filters|restriction|restrictions)\b",
    re.IGNORECASE,
)

# "know more about Aalborg", "tell me about Ajou University", "at Akita"
_NAMED_UNIVERSITY_PATTERN = re.compile(
    r"\b(?:know more about|tell me about|more about|what about|how about|details on|"
    r"info(?:rmation)? on|at|in|to|for)\s+"
    r"([A-Z][\w&.'-]*(?:\s+(?:of|the|de|and|&|[A-Z][\w&.'-]*))*)",
)

# After an explicit "tell me about ...", the thing named is a university even
# when the student did not capitalise it. "tell me more uc3m" names one.
_ABOUT_PATTERN = re.compile(
    r"\b(?:know more about|tell me more about|tell me about|tell me more|more about|"
    r"more on|details on|info(?:rmation)? on)\s+"
    r"([\w&.'-]+(?:\s+[\w&.'-]+){0,4})",
    re.IGNORECASE,
)


_STOP_WORDS = frozenset(
    {
        "i", "me", "my", "the", "a", "an", "it", "that", "this", "there", "them",
        "semester", "sem", "fall", "spring", "summer", "gem", "susep", "explorer",
        "singapore", "ntu", "exchange", "europe", "asia", "anywhere",
    }
)

# Words that describe a shortlist request rather than a destination. This is
# used only when a student supplies a country that is not in Coursefinder. It
# lets us preserve an explicit unsupported location as a hard zero-result
# filter instead of silently turning it into an unrestricted shortlist.
_DESTINATION_CONTEXT_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "at", "about", "abroad", "all", "am", "any", "can", "clear",
        "could", "country", "destination", "do", "exchange", "find", "filter",
        "filters", "for", "from", "get", "go", "have", "how", "i", "i'm", "i've", "in", "is",
        "it", "like", "list", "local", "m", "me", "mod", "mods", "module",
        "modules", "my", "of", "on", "only", "option", "options", "partner",
        "partners", "place", "places", "please", "recommend", "remove", "reset",
        "same", "school", "schools", "semester", "show", "somewhere", "student",
        "students", "study", "suggest", "term", "the", "there", "to", "university",
        "universities", "uni", "unis", "want", "what", "where", "which", "with",
        "within", "would", "year", "years",
    }
)
_DESTINATION_TERM_WORDS = frozenset(
    {"semester", "sem", "fall", "autumn", "spring", "summer", "intake", "term", "full", "year", "special"}
)
_DESTINATION_OPERATION_WORDS = frozenset(
    {
        "add", "addition", "additionally", "also", "allow", "append", "bring", "back", "change", "correct",
        "delete", "drop", "exclude", "excluding", "include", "including", "keep",
        "omit", "omitting", "plus", "remove", "replace", "restore", "retain", "show",
        "swap", "update",
    }
)

# A carried university is useful for referents such as "there" and "that
# place", but it must be released when the student broadens the scope. This
# is intentionally a scope detector, not a list of complete user questions.
_BROAD_UNIVERSITY_SCOPE = re.compile(
    r"\b(?:any|all|other|different|more)\s+(?:un(?:i|iversit)|partner|school|option)"
    r"|\b(?:anywhere|everywhere)\b.{0,36}\b(?:university|partner|school|option)"
    r"|\b(?:not|rather than)\s+(?:just|only)\b.{0,24}\b(?:university|partner|school)\b",
    re.IGNORECASE,
)


@dataclass
class Extraction:
    """What the deterministic pass could establish, and what it could not."""

    school_code: str = ""
    school_name: str = ""
    preferred_semester: str = ""
    programme_type: str = ""
    destination_pref: str | None = None
    named_university: str | None = None
    university_candidates: list[str] = field(default_factory=list)
    cgpa: float | None = None
    budget_sgd: float | None = None
    destination_region: str | None = None
    destination_countries: list[str] = field(default_factory=list)
    module_codes: list[str] = field(default_factory=list)
    excluded_module_types: list[str] = field(default_factory=list)
    included_module_types: list[str] = field(default_factory=list)
    restored_module_types: list[str] = field(default_factory=list)
    replace_module_type_filters: bool = False
    clear_filters: bool = False
    clear_module_filters: bool = False
    add_module_codes: list[str] = field(default_factory=list)
    remove_module_codes: list[str] = field(default_factory=list)
    add_excluded_module_types: list[str] = field(default_factory=list)
    remove_excluded_module_types: list[str] = field(default_factory=list)
    add_included_module_types: list[str] = field(default_factory=list)
    remove_included_module_types: list[str] = field(default_factory=list)
    show_module_types: list[str] = field(default_factory=list)
    found: list[str] = field(default_factory=list)

    def as_profile(self, base: Profile | None = None) -> Profile:
        """Merge over an existing profile without erasing what it already knows."""
        base = base or Profile()
        clear_all = self.clear_filters
        clear_modules = clear_all or self.clear_module_filters
        # A newly stated country and a newly stated region are alternatives.
        # Do not leave the old location constraint active when a student
        # refines "Europe" to "Germany" (or the reverse).
        stated_country = self.destination_pref is not None
        stated_region = self.destination_region is not None
        programme_type = self.programme_type or base.programme_type or GEMX
        destination_pref = (
            self.destination_pref
            if stated_country
            else (None if stated_region or clear_all else base.destination_pref)
        )
        destination_region = (
            self.destination_region
            if stated_region
            else (None if stated_country or clear_all else base.destination_region)
        )
        destination_countries = (
            list(self.destination_countries)
            if stated_region
            else ([] if stated_country or clear_all else list(base.destination_countries))
        )

        # GEMX is overseas and SUSEP is local-only. If the student switches
        # programmes without restating a destination, release an incompatible
        # carried country/region instead of issuing a contradictory query.
        # Explicit destination text remains authoritative; the parser assigns
        # the programme from that location before this compatibility guard.
        if not stated_country and not stated_region:
            if programme_type == SUSEP and (
                destination_pref != "SINGAPORE" or destination_region is not None
            ):
                destination_pref = None
                destination_region = None
                destination_countries = []
            elif programme_type == GEMX and destination_pref == "SINGAPORE":
                destination_pref = None
                destination_region = None
                destination_countries = []

        def normalise_code(value: str) -> str:
            return re.sub(r"\s+", "", str(value or "")).upper()

        def add_unique(values: list[str], additions: list[str]) -> list[str]:
            existing = {normalise_code(value) for value in values}
            for value in additions:
                clean = str(value).strip()
                key = normalise_code(clean)
                if key and key not in existing:
                    values.append(clean.upper())
                    existing.add(key)
            return values

        def remove_values(values: list[str], removals: list[str]) -> list[str]:
            targets = {normalise_code(value) for value in removals}
            return [value for value in values if normalise_code(value) not in targets]

        module_codes = (
            list(self.module_codes)
            if self.module_codes
            else ([] if clear_modules else list(base.module_codes))
        )
        module_codes = remove_values(module_codes, self.remove_module_codes)
        module_codes = add_unique(module_codes, self.add_module_codes)

        excluded_types = (
            list(self.excluded_module_types)
            if self.excluded_module_types
            else ([] if clear_modules else list(base.excluded_module_types))
        )
        included_types = (
            list(self.included_module_types)
            if self.included_module_types
            else ([] if clear_modules else list(base.included_module_types))
        )
        restored_types = (
            []
            if clear_modules or self.replace_module_type_filters
            else list(base.restored_module_types)
        )
        excluded_types = remove_values(
            excluded_types,
            self.remove_excluded_module_types + self.show_module_types,
        )
        excluded_types = add_unique(excluded_types, self.add_excluded_module_types)
        included_types = remove_values(included_types, self.remove_included_module_types)
        included_types = add_unique(included_types, self.add_included_module_types)
        restored_types = remove_values(
            restored_types,
            self.remove_excluded_module_types
            + self.add_excluded_module_types
            + self.remove_included_module_types,
        )
        restored_types = add_unique(restored_types, self.show_module_types)
        # "Show BDEs" restores them from an exclusion. If a whitelist is
        # already active, add them to it; otherwise removing the exclusion is
        # sufficient and must not accidentally turn the query into BDE-only.
        if included_types:
            included_types = add_unique(included_types, self.show_module_types)
        included_types = remove_values(included_types, excluded_types)
        restored_types = remove_values(restored_types, excluded_types)

        return Profile(
            school_code=self.school_code or base.school_code,
            school_name=self.school_name or base.school_name,
            preferred_semester=self.preferred_semester or base.preferred_semester,
            programme_type=programme_type,
            destination_pref=destination_pref,
            destination_region=destination_region,
            destination_countries=destination_countries,
            module_codes=module_codes,
            excluded_module_types=excluded_types,
            included_module_types=included_types,
            restored_module_types=restored_types,
            cgpa=self.cgpa if self.cgpa is not None else (None if clear_all else base.cgpa),
            max_monthly_budget_sgd=(
                self.budget_sgd
                if self.budget_sgd is not None
                else (None if clear_all else base.max_monthly_budget_sgd)
            ),
        )


def broadens_university_scope(text: str) -> bool:
    """Whether a turn asks to leave a previously named university."""
    return bool(_BROAD_UNIVERSITY_SCOPE.search(text or ""))


def replaces_university_scope(text: str, extraction: Extraction) -> bool:
    """Whether a turn replaces a carried university with a new shortlist scope.

    A new explicit country or region is itself a scope change when the turn did
    not name another university. This covers natural follow-ups such as
    "what about Asia instead" as well as broader wording without relying on a
    fixed question template.
    """
    if extraction.named_university:
        return False
    if extraction.clear_filters:
        return True
    return broadens_university_scope(text) or bool(
        extraction.destination_pref or extraction.destination_region
    )


def _cgpa(text: str) -> float | None:
    match = _CGPA_PATTERN.search(text)
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # NTU grades on a 5.0 scale; anything outside 0-5 is some other number that
    # happened to sit next to the word "GPA".
    return value if 0.0 < value <= 5.0 else None


def _budget(text: str) -> float | None:
    matches = list(_BUDGET_PATTERN.finditer(text)) + list(_BUDGET_CONTEXT_PATTERN.finditer(text))
    for match in matches:
        try:
            value = float(match.group(1).replace(",", ""))
        except (TypeError, ValueError):
            continue
        suffix = (match.group(2) or "").lower()
        if suffix == "k":
            value *= 1_000
        elif suffix == "m":
            value *= 1_000_000
        # A budget below 50 is almost certainly a CGPA, a semester number or a
        # module count that matched the same shape.
        if value >= 50:
            return value
    return None


def _module_codes(text: str) -> list[str]:
    found: list[str] = []
    for match in _MODULE_CODE_PATTERN.finditer(text or ""):
        if match.group(1).lower() in _MODULE_PREFIX_STOPWORDS:
            continue
        code = f"{match.group(1)}{match.group(2)}".upper()
        if code not in found:
            found.append(code)
    return found


def _types_in_phrase(phrase: str) -> list[str]:
    lowered = re.sub(r"\s+", " ", (phrase or "").lower()).strip()
    found: list[str] = []
    for raw in _TYPE_PHRASES:
        if re.search(rf"\b{re.escape(raw)}\b", lowered):
            canonical = _TYPE_ALIASES[raw]
            if canonical not in found:
                found.append(canonical)
    return found


def _module_type_constraints(text: str) -> tuple[list[str], list[str]]:
    excluded: list[str] = []
    included: list[str] = []
    for match in _NEGATION_PATTERN.finditer(text or ""):
        for value in _types_in_phrase(match.group("types")):
            if value not in excluded:
                excluded.append(value)
    for match in _ONLY_TYPE_PATTERN.finditer(text or ""):
        for value in _types_in_phrase(match.group("types")):
            if value not in included:
                included.append(value)
    return excluded, included


def _module_type_deltas(text: str) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """Extract add/remove/restore operations for mapping-type filters.

    A filter follow-up is an edit to the active filter state, not a new
    replacement filter.  The wording is deliberately interpreted from the
    operation around the category, so ``show BDEs`` restores BDE without
    accidentally changing an unrestricted query into a BDE-only query.
    """
    add_excluded: list[str] = []
    remove_excluded: list[str] = []
    add_included: list[str] = []
    remove_included: list[str] = []
    show: list[str] = []

    def extend_unique(target: list[str], values: list[str]) -> None:
        for value in values:
            if value not in target:
                target.append(value)

    for match in _RESTORE_TYPE_FILTER_PATTERN.finditer(text or ""):
        extend_unique(show, _types_in_phrase(match.group("types")))

    for match in _SHOW_MODULE_TYPE_PATTERN.finditer(text or ""):
        # Do not reinterpret the positive verb inside "don't show ..." or
        # "do not include ..." as a restore operation.
        prefix = (text or "")[max(0, match.start() - 12) : match.start()]
        if re.search(r"(?:don't|do\s+not)\s*$", prefix, re.IGNORECASE):
            continue
        extend_unique(show, _types_in_phrase(match.group("types")))

    # ``show``/restore wins over the generic negation matcher. This covers
    # both "show BDEs" and "remove the BDE filter".
    excluded, included = _module_type_constraints(text)
    for value in excluded:
        if value not in show:
            extend_unique(remove_included, [value])
            extend_unique(add_excluded, [value])
    for value in included:
        if value not in show:
            extend_unique(add_included, [value])

    # ``include``/``show`` is a restore operation. Other additive verbs can
    # still introduce a type into an explicit whitelist. Exclusion wins when
    # both verbs occur in the same sentence (for example, "also exclude BDE").
    if _ADD_MODULE_CODE_PATTERN.search(text or "") and not show:
        for value in _types_in_phrase(text):
            if value not in excluded:
                extend_unique(add_included, [value])

    if show:
        for value in show:
            extend_unique(remove_excluded, [value])

    # An explicit "remove/clear X filter" is a restore operation, not a new
    # exclusion. Keep the generic lists clean for the profile merge.
    for value in show:
        if value in add_excluded:
            add_excluded.remove(value)
        if value in remove_included:
            remove_included.remove(value)
    return add_excluded, remove_excluded, add_included, remove_included, show


def _country_for_university(name: str | None) -> str | None:
    """Return the stored country for a university the resolver identified."""
    if not name:
        return None
    wanted = name.casefold()
    return next(
        (country for _, university, country in all_university_names() if university.casefold() == wanted),
        None,
    )


def university_matches_programme(name: str | None, programme_type: str | None) -> bool:
    """Whether a carried university is valid for the selected exchange type.

    Coursefinder stores SUSEP and GEMX mappings separately, but the
    conversational state also carries a named university outside ``Profile``.
    Checking the university's stored country here prevents a local programme
    from being queried against an overseas university (and vice versa).
    Unknown names are left alone so this guard never guesses or blocks a
    university that the database cannot identify.
    """
    country = _country_for_university(name)
    kind = (programme_type or "").strip().upper()
    if not country or kind not in {GEMX, SUSEP}:
        return True
    is_singapore = country.strip().upper() == "SINGAPORE"
    return is_singapore if kind == SUSEP else not is_singapore


def country_for_university(name: str | None) -> str | None:
    """Expose the validated Coursefinder country for orchestration guardrails."""
    return _country_for_university(name)


def _programme_type(
    text: str,
    *,
    country: str | None = None,
    region: tuple[str, list[str]] | None = None,
    university_country: str | None = None,
) -> str:
    """Infer the mutually exclusive exchange programme from the turn.

    Location is a stronger signal than a stale programme from an earlier
    turn: SUSEP is local-only, while a non-Singapore country, region, or
    university belongs to GEM Explorer. Explicit programme wording still
    handles messages that do not name a destination.
    """
    if country:
        return SUSEP if country == "SINGAPORE" else GEMX
    if region or university_country:
        return SUSEP if university_country == "SINGAPORE" else GEMX
    if _SUSEP_PATTERN.search(text):
        return SUSEP
    if _GEMX_PATTERN.search(text):
        return GEMX
    return ""


@lru_cache(maxsize=1)
def _structural_name_words() -> frozenset[str]:
    """Words so common across the 558 partner names that they identify nobody.

    Derived from the data rather than listed by hand, so it cannot drift: if a
    word appears in more than about 2% of university names it is structural
    ("University", "School", "Technology") and matching on it alone is noise.
    """
    counts: dict[str, int] = {}
    universities = all_university_names()
    for _, name, _ in universities:
        for word in set(re.findall(r"[a-z]{3,}", name.lower())):
            counts[word] = counts.get(word, 0) + 1
    floor = max(3, int(len(universities) * 0.02))
    return frozenset(word for word, count in counts.items() if count >= floor)


# Qualifiers that appear in parentheses but name a programme scope, not a
# university: "(Coe Only)", "(Except Nbs)", "(Mikkeli Campus)".
_NOT_AN_ACRONYM = re.compile(r"only|except|campus|all\b", re.IGNORECASE)


@lru_cache(maxsize=1)
def _acronym_index() -> dict[str, str]:
    """Short forms a student actually types, mapped to the full name.

    Coursefinder writes the acronym in brackets - "Universidad Carlos Iii De
    Madrid (Uc3m)" - and a student writes only "uc3m". Without this, the token
    has no letters-only run of three characters, so the distinctive-word
    matcher reduces it to an empty string and nothing matches at all.

    A short form claimed by two different universities is dropped: "(Aun)"
    belongs to both Mahidol and Gadjah Mada, and guessing between them would
    answer a question about one with the other's rules.
    """
    claimed: dict[str, set[str]] = {}
    for _, name, _ in all_university_names():
        for bracketed in re.findall(r"\(([^)]{2,14})\)", name):
            if _NOT_AN_ACRONYM.search(bracketed):
                continue
            key = re.sub(r"[^a-z0-9]", "", bracketed.lower())
            if len(key) >= 3:
                claimed.setdefault(key, set()).add(name)
    return {key: next(iter(names)) for key, names in claimed.items() if len(names) == 1}


@lru_cache(maxsize=1)
def _ambiguous_acronyms() -> frozenset[str]:
    """Short forms more than one university claims.

    These must be refused outright, not fuzzy-matched. "(Aun)" belongs to both
    Mahidol and Universitas Gadjah Mada; left to the similarity score, "aun"
    resolves to whichever name carries fewer other words, which is a guess
    dressed as a match.
    """
    claimed: dict[str, set[str]] = {}
    for _, name, _ in all_university_names():
        for bracketed in re.findall(r"\(([^)]{2,14})\)", name):
            if _NOT_AN_ACRONYM.search(bracketed):
                continue
            key = re.sub(r"[^a-z0-9]", "", bracketed.lower())
            if len(key) >= 3:
                claimed.setdefault(key, set()).add(name)
    return frozenset(key for key, names in claimed.items() if len(names) > 1)


def _distinctive(phrase: str) -> str:
    """The words in a name that actually identify a university.

    "Queen's University - School Of Business" reduces to "queen": everything
    else is shared with hundreds of other names and only adds noise to a
    similarity score.
    """
    structural = _structural_name_words()
    words = [w for w in re.findall(r"[a-z]{3,}", (phrase or "").lower()) if w not in structural]
    return " ".join(words)


def _is_plausible_candidate(phrase: str) -> bool:
    """Whether a phrase could name a university rather than something else.

    Two rejections matter in practice:

    * **A degree programme is not a university.** "Computer Science" contains
      the word "Science", which appears inside dozens of university names, so
      a substring-based score matches it at 100 against "Aalto University -
      School Of Science & Technology". Checking the programme resolver first
      removes the whole class of error.
    * **A phrase made only of structural words** ("School of Technology")
      identifies no particular university.
    """
    cleaned = phrase.strip()
    if len(cleaned) <= 2 or cleaned.lower() in _STOP_WORDS:
        return False
    if re.sub(r"[^a-z0-9]", "", cleaned.lower()) in _ambiguous_acronyms():
        return False
    if resolve_programme(cleaned) is not None:
        return False
    words = re.findall(r"[a-z]{3,}", cleaned.lower())
    if words and all(word in _structural_name_words() for word in words):
        return False
    return True


def _unresolved_destination(
    text: str,
    *,
    school_code: str = "",
    school_name: str = "",
    term: str = "",
) -> str | None:
    """Extract an unsupported destination as a hard filter."""
    if is_recognized_country_name(text) and not resolve_country(text):
        return " ".join(text.split()).upper()

    # A real country mentioned in a natural sentence is still an explicit
    # scope, even when the student has not supplied the programme yet. This is
    # what lets a first turn such as "what about Israel?" survive until the
    # follow-up supplies "Business, semester 1".
    candidate_text = (text or "").lower()
    country_tokens = re.findall(r"[a-z][a-z'-]*", candidate_text)
    for width in (3, 2, 1):
        for start in range(len(country_tokens) - width + 1):
            phrase = " ".join(country_tokens[start : start + width])
            if is_recognized_country_name(phrase) and not resolve_country(phrase):
                return phrase.upper()

    if not (school_code or term):
        return None

    def candidate_from(segment: str) -> str | None:
        # Module-code payloads are not destinations. Truncate at the first
        # code so its alphabetic prefix (for example, "IE" in IE3017) cannot
        # become a fake destination in a correction or filter command.
        module_match = _MODULE_CODE_PATTERN.search(segment)
        if module_match:
            segment = segment[: module_match.start()]
        tokens = re.findall(r"[a-z][a-z'-]*", segment.lower())
        kept: list[str] = []
        for token in tokens:
            # A marker such as "to" also introduces filter operations ("to
            # include only ..."). Those words are a hard boundary for this
            # destination-only fallback, not a candidate to be uppercased.
            # Stop before the filter payload so module codes cannot become a
            # fake country either.
            if token in _DESTINATION_OPERATION_WORDS:
                return " ".join(kept).strip(" '-").upper() or None
            if (
                token in _DESTINATION_CONTEXT_STOP_WORDS
                or token in _DESTINATION_TERM_WORDS
                or token.isdigit()
            ):
                if kept:
                    break
                continue
            kept.append(token)
        if not kept or len(kept) > 3:
            return None
        candidate = " ".join(kept).strip(" '-").upper()
        if (
            not candidate
            or resolve_region(candidate)
            or resolve_country(candidate)
            or resolve_programme(candidate) is not None
            or named_university(candidate) is not None
        ):
            return None
        return candidate

    # Natural location phrases are the strongest evidence. The candidate
    # extractor stops at the next planning/term word, so "in Israel for CSC"
    # yields only ISRAEL.
    marker = re.compile(
        r"\b(?:in|from|within|around|country(?:\s+of)?|destination|to)\s+"
        r"[a-z][a-z0-9'-]*(?:\s+[a-z][a-z0-9'-]*){0,3}",
        re.IGNORECASE,
    )
    for match in marker.finditer(candidate_text):
        candidate = candidate_from(match.group(0).split(None, 1)[1])
        if candidate:
            return candidate

    # Compact searches such as "israel csc sem 1" have no preposition. Look
    # immediately before/after the programme anchor instead of treating every
    # leftover word in an ordinary sentence as a country.
    for value in (school_name, school_code):
        if not value:
            continue
        match = re.search(rf"\b{re.escape(value.lower())}\b", candidate_text)
        if not match:
            continue
        for segment in (candidate_text[: match.start()], candidate_text[match.end() :]):
            candidate = candidate_from(segment)
            if candidate:
                return candidate
    return None


def _fuzzy_named_university(text: str, threshold: int = 88) -> str | None:
    """The partner university this message names, matched against the database.

    Fuzzy, because a student writes "Aalborg" for "Aalborg University" and
    "NTNU" for its spelled-out name. Anything below the threshold returns
    ``None`` rather than the closest of 558 wrong answers.
    """
    if not (text or "").strip():
        return None
    try:
        from rapidfuzz import fuzz, process
    except ImportError:  # pragma: no cover - rapidfuzz is a hard requirement
        return None

    universities = all_university_names()
    names = [name for _, name, _ in universities]
    lowered = text.lower()

    # A full name quoted in the message wins outright.
    direct = [name for name in names if len(name) > 6 and name.lower() in lowered]
    if direct:
        return max(direct, key=len)

    # Then an unambiguous short form: "uc3m", "epfl", "nus". Checked before the
    # fuzzy path because it is exact, and case-insensitively because a student
    # types it lowercase.
    acronyms = _acronym_index()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9]{2,13}", text):
        hit = acronyms.get(token.lower())
        if hit:
            return hit

    # Otherwise score the capitalised phrases the message actually contains,
    # rather than the whole sentence, which would drown the signal.
    candidates: list[str] = []
    for match in _NAMED_UNIVERSITY_PATTERN.finditer(text):
        candidates.append(match.group(1).strip())
    for match in _ABOUT_PATTERN.finditer(text):
        candidates.append(match.group(1).strip())
    candidates.extend(re.findall(r"\b[A-Z][\w&.'-]{3,}\b", text))

    # Score on the *distinctive* words only. Comparing whole names lets the
    # shared word "University" carry the match: a substring scorer ranks "Ie
    # University" at 92 for "Queens University", and "Queensland University Of
    # Technology" above the Queen's entries.
    distinctive = [(_distinctive(name), name) for name in names]

    scored: list[tuple[float, float, str]] = []
    for candidate in candidates:
        if not _is_plausible_candidate(candidate):
            continue
        key = _distinctive(candidate)
        if not key:
            continue
        for name_key, name in distinctive:
            if not name_key:
                continue
            scored.append(
                (float(fuzz.token_set_ratio(key, name_key)), float(fuzz.ratio(key, name_key)), name)
            )
    if not scored:
        return None

    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    best = scored[0]
    if best[0] < threshold:
        return None

    rivals = [row[2] for row in scored if best[0] - row[0] < 4 and best[1] - row[1] < 4]
    keys = {_distinctive(name) for name in rivals}
    if len(keys) > 1:
        # Different universities scoring alike is real ambiguity — "Queens
        # University" against "Queensland University Of Technology" — and
        # guessing answers a student's question with another place's rules.
        return None
    # One university listed several times, once per faculty ("University Of
    # Waterloo", "- Ccds", "- Coe & Cos"). They are the same institution, so
    # the shortest name is the base entry.
    return min(rivals, key=len)


def university_candidates(text: str, threshold: int = 88) -> list[str]:
    """Resolve a natural-language university mention to canonical names.

    Coursefinder's token matcher handles lowercase fragments and ``uni``
    shorthand. The older fuzzy matcher remains as a compatibility fallback for
    typos and unusual punctuation, but it is only allowed to contribute one
    result because fuzzy similarity cannot safely enumerate ambiguity.
    """
    matches = match_universities(text)
    if matches:
        return [str(match["name"]) for match in matches]
    fuzzy = _fuzzy_named_university(text, threshold=threshold)
    return [fuzzy] if fuzzy else []


def named_university(text: str, threshold: int = 88) -> str | None:
    """Return a university only when the current message names one uniquely."""
    candidates = university_candidates(text, threshold=threshold)
    return candidates[0] if len(candidates) == 1 else None


def select_university_option(
    text: str, options: list[str], current: str | None = None
) -> str | None:
    """Resolve a numbered choice or ``another`` against a carried cost list."""
    if not options:
        return None
    value = (text or "").strip()
    number = re.fullmatch(r"(?:option\s*|number\s*)?(\d+)", value, re.IGNORECASE)
    if number:
        index = int(number.group(1)) - 1
        return options[index] if 0 <= index < len(options) else None
    if re.search(r"\b(?:another|different|next)\b", value, re.IGNORECASE):
        if current in options:
            return options[(options.index(current) + 1) % len(options)]
        return options[0]
    return None


def extract(message: str, base: Profile | None = None) -> Extraction:
    """Read everything a regular expression can defend out of one message."""
    text = (message or "").strip()
    result = Extraction()
    if not text:
        return result

    if _CLEAR_ALL_FILTERS_PATTERN.search(text):
        result.clear_filters = True
        result.found.append("clear_filters")
    elif _CLEAR_MODULE_FILTER_PATTERN.search(text):
        result.clear_module_filters = True
        result.found.append("clear_module_filters")

    term = resolve_term(text)
    if term:
        result.preferred_semester = term
        result.found.append("term")

    programme = resolve_programme(text)
    # Ambiguous programme codes (notably BUS) are intentionally case-sensitive
    # in ``resolve_programme`` so ordinary words do not become filters. Once a
    # term is explicit, however, a lower-case code is a clear exchange query
    # (e.g. "israel bus sem 1"). Resolve it deterministically without letting
    # the destination scope get lost between conversational turns.
    if programme is None and term:
        for token in re.findall(r"\b[A-Za-z]{2,6}\b", text):
            candidate = by_code(token)
            if candidate is not None:
                programme = candidate
                break
    if programme is not None:
        result.school_code = programme.code
        result.school_name = programme.name
        result.found.append("programme")

    country = resolve_country(text)
    if not country:
        country = _unresolved_destination(
            text,
            school_code=result.school_code or (base.school_code if base else ""),
            school_name=result.school_name or (base.school_name if base else ""),
            term=result.preferred_semester or (base.preferred_semester if base else ""),
        )
    if country:
        result.destination_pref = country
        result.found.append("destination")

    universities = university_candidates(text)
    if len(universities) == 1:
        result.named_university = universities[0]
        result.found.append("named_university")
    elif len(universities) > 1:
        result.university_candidates = universities
        result.found.append("university_candidates")

    cgpa = _cgpa(text)
    if cgpa is not None:
        result.cgpa = cgpa
        result.found.append("cgpa")

    budget = _budget(text)
    if budget is not None:
        result.budget_sgd = budget
        result.found.append("budget")

    region = resolve_region(text)
    if region:
        result.destination_region, result.destination_countries = region
        result.found.append("destination_region")

    kind = _programme_type(
        text,
        country=country,
        region=region,
        university_country=_country_for_university(result.named_university),
    )
    if kind:
        result.programme_type = kind
        result.found.append("programme_type")

    module_codes = _module_codes(text)
    if module_codes:
        # A list introduced by "only" is a replacement constraint. Natural
        # follow-ups such as "include IE3102 too" and "remove IE3014" are
        # deltas against the active filter state.
        only_module_list = bool(
            re.search(
                r"\b(?:only|just|limited\s+to|restrict(?:ed)?\s+to)\b",
                text,
                re.IGNORECASE,
            )
        )
        if not only_module_list and _REMOVE_MODULE_CODE_PATTERN.search(text):
            result.remove_module_codes = module_codes
            result.found.append("remove_module_codes")
        elif not only_module_list and _ADD_MODULE_CODE_PATTERN.search(text):
            result.add_module_codes = module_codes
            result.found.append("add_module_codes")
        else:
            result.module_codes = module_codes
            result.found.append("module_codes")

    (
        result.add_excluded_module_types,
        result.remove_excluded_module_types,
        result.add_included_module_types,
        result.remove_included_module_types,
        result.show_module_types,
    ) = _module_type_deltas(text)

    excluded, included = _module_type_constraints(text)
    excluded_for_replacement = [value for value in excluded if value not in result.show_module_types]
    if excluded_for_replacement:
        prior_type_filter = bool(
            base
            and (base.excluded_module_types or base.included_module_types)
        )
        if (_ADD_MODULE_CODE_PATTERN.search(text) or prior_type_filter) and not result.clear_module_filters:
            result.add_excluded_module_types = excluded_for_replacement
            result.excluded_module_types = []
            result.found.append("add_excluded_module_types")
        else:
            result.excluded_module_types = excluded_for_replacement
            result.replace_module_type_filters = True
            result.found.append("excluded_module_types")
    if included:
        result.included_module_types = included
        result.replace_module_type_filters = True
        result.found.append("included_module_types")

    for field_name in (
        "add_excluded_module_types",
        "remove_excluded_module_types",
        "add_included_module_types",
        "remove_included_module_types",
        "show_module_types",
    ):
        if getattr(result, field_name):
            result.found.append(field_name)

    # SUSEP is the Singapore programme; naming Singapore as a destination is a
    # strong signal, but the explicit word always wins.
    if not result.programme_type and result.destination_pref == "SINGAPORE":
        result.programme_type = SUSEP
        result.found.append("programme_type")

    return result
