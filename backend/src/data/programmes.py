"""Resolving what a student types into an NTU programme code.

The 68 programmes are read from the database, not hardcoded, so the resolver
cannot drift from the data it queries. Only the handful of colloquialisms that
genuinely do not appear in any official name are listed here, and every one of
them is validated against the database at load time — an alias pointing at a
code that no longer exists is dropped rather than silently mis-resolving.

The trap this module exists to avoid: ``CS`` is **Communication Studies**.
"Computer Science" is ``CSC``. Getting that backwards hands a communications
student a shortlist of algorithms courses.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from data.coursefinder_db import all_programmes
from graph.domain import Programme

# Colloquial phrasings with no official counterpart. Values are programme codes
# and are checked against the database before use.
_PHRASE_ALIASES: dict[str, str] = {
    "comp sci": "CSC",
    "compsci": "CSC",
    "computing": "CSC",
    "cs degree": "CSC",
    "computer science": "CSC",
    "comms": "CS",
    "communications": "CS",
    "comm studies": "CS",
    "data science": "DSAI",
    "data science and ai": "DSAI",
    "ai": "DSAI",
    "biz": "BUS",
    "business school": "BUS",
    "nbs": "BUS",
    "accounting": "ACC",
    "mech eng": "ME",
    "mechanical": "ME",
    "electrical": "EEE",
    "civil": "CEE",
    "chemical": "CBE",
    "aerospace": "AERO",
    "material science": "MAT",
    "materials": "MAT",
    "maths": "MATH",
    "math": "MATH",
    "econs": "ECON",
    "psych": "PSY",
    "sociology": "SOC",
    "renaissance": "REP",
}

# Short codes that are also ordinary English words. They resolve only when the
# student wrote them in capitals, which is how a programme code is written.
_AMBIGUOUS_CODES = {"ME", "BS", "MS", "CS", "CE", "SOC", "REP", "ENG", "MAT", "BUS", "PHY", "AISC"}


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


@lru_cache(maxsize=4)
def _index(db_path: str | None = None) -> tuple[
    tuple[Programme, ...], dict[str, Programme], dict[str, Programme]
]:
    programmes = tuple(all_programmes(Path(db_path) if db_path else None))
    by_code = {p.code.upper(): p for p in programmes}
    by_name = {_normalise(p.name): p for p in programmes}
    return programmes, by_code, by_name


def programmes(db_path: str | None = None) -> list[Programme]:
    return list(_index(db_path)[0])


def by_code(code: str, db_path: str | None = None) -> Programme | None:
    return _index(db_path)[1].get((code or "").strip().upper())


def _valid_aliases(db_path: str | None = None) -> dict[str, Programme]:
    _, codes, _ = _index(db_path)
    return {phrase: codes[code] for phrase, code in _PHRASE_ALIASES.items() if code in codes}


def _code_from_capitals(raw_text: str, db_path: str | None = None) -> Programme | None:
    """Match a programme code the student typed as a code.

    ``I'm in CSC`` resolves; ``tell me about Aalborg`` must not resolve ``ME``,
    which is why ambiguous codes require the original capitalisation.
    """
    _, codes, _ = _index(db_path)
    for token in re.findall(r"\b[A-Za-z]{2,6}\b", raw_text or ""):
        upper = token.upper()
        if upper not in codes:
            continue
        if upper in _AMBIGUOUS_CODES and token != upper:
            continue
        return codes[upper]
    return None


def resolve_programme(text: str, db_path: str | None = None) -> Programme | None:
    """Best programme for a free-text phrase, or ``None`` when nothing is close.

    Returning ``None`` is a valid, useful answer: the clarification gate then
    asks the student directly, which is far better than guessing a degree.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    programme_list, _, names = _index(db_path)
    cleaned = _normalise(raw)

    # 1. The exact official name, e.g. "Computer Science".
    if cleaned in names:
        return names[cleaned]

    # 2. A known colloquialism, longest phrase first so "comp sci" beats "cs".
    aliases = _valid_aliases(db_path)
    for phrase in sorted(aliases, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", cleaned):
            return aliases[phrase]

    # 3. A programme code written as a code.
    code_hit = _code_from_capitals(raw, db_path)
    if code_hit is not None:
        return code_hit

    # 4. An official name quoted inside a longer sentence. Longest wins, so
    #    "Computer Science and Economics" beats "Computer Science".
    contained = [
        p
        for p in programme_list
        if re.search(rf"\b{re.escape(_normalise(p.name))}\b", cleaned)
    ]
    if contained:
        return max(contained, key=lambda p: len(p.name))

    # 5. Fuzzy, as a last resort and only when the match is strong.
    return _fuzzy(cleaned, programme_list)


def _fuzzy(cleaned: str, programme_list: tuple[Programme, ...]) -> Programme | None:
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover - rapidfuzz is a hard requirement
        return None

    wants_combined = any(word in cleaned for word in ("double", "and", "&"))
    best: tuple[float, int, Programme] | None = None
    for programme in programme_list:
        name = _normalise(programme.name)
        score = float(fuzz.token_set_ratio(cleaned, name))
        if score < 82:
            continue
        # A double degree is a different degree, not a longer spelling of one.
        # Without this, "computer science" drifts to "Business and Computer
        # Science (Double Degree)" whenever token overlap happens to tie.
        if not wants_combined and ("double" in name or " and " in f" {name} "):
            score -= 12
        # Prefer the candidate closest in length to what was actually typed.
        distance = abs(len(name) - len(cleaned))
        candidate = (score, -distance, programme)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None or best[0] < 82:
        return None
    return best[2]
