"""The four exchange terms, and how a student might refer to one.

⚠️ **Assumption, flagged deliberately.** The exact term labels GEM Explorer
uses in its Terra Dotta search (``pt=<term>``) do not appear in any surviving
document. The canonical strings below follow the phrasing the frontend already
ships (``HomeHero.tsx:17`` — "Semester 1 (Fall)") and the conventional NTU
vocabulary. Phase 3 queries Terra Dotta directly and will replace
:data:`CANONICAL_TERMS` with whatever the portal actually returns. That is why
every label lives in this one constant rather than being spelled out at each
call site.

Alias matching is deliberately generous, because a student writes "sem 1",
"fall", "first semester" and "aug intake" for the same thing.
"""

from __future__ import annotations

import re

SEMESTER_1 = "Semester 1 (Fall)"
SEMESTER_2 = "Semester 2 (Spring)"
FULL_YEAR = "Full Year"
SPECIAL_TERM = "Special Term (Summer)"

CANONICAL_TERMS: tuple[str, ...] = (SEMESTER_1, SEMESTER_2, FULL_YEAR, SPECIAL_TERM)

# Ordered longest-intent-first: a full-year phrase must win over the "sem 1"
# hiding inside "semester 1 and 2".
_ALIAS_PATTERNS: tuple[tuple[str, str], ...] = (
    (FULL_YEAR, r"\b(full[ -]?year|whole year|both semesters?|academic year|two semesters?|"
                r"semesters? 1 and 2|year[ -]?long)\b"),
    (SPECIAL_TERM, r"\b(special term|summer|summer term|summer school|winter term|short term)\b"),
    (SEMESTER_1, r"\b(sem(?:ester)?\s*(?:one|1)|first sem(?:ester)?|fall|autumn|aug(?:ust)? intake)\b"),
    (SEMESTER_2, r"\b(sem(?:ester)?\s*(?:two|2)|second sem(?:ester)?|spring|jan(?:uary)? intake)\b"),
)


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def resolve_term(text: str) -> str | None:
    """The exchange term a phrase names, or ``None``.

    ``None`` is the honest answer for "next year" or "sometime soon"; the
    clarification gate then asks, which beats picking a term for the student.
    """
    cleaned = _normalise(text)
    if not cleaned:
        return None
    for canonical, pattern in _ALIAS_PATTERNS:
        if re.search(pattern, cleaned):
            return canonical
    return None


def is_term(text: str) -> bool:
    return (text or "").strip() in CANONICAL_TERMS
