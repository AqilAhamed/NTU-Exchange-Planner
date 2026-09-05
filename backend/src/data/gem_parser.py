"""Reading workload, eligibility and cost evidence out of a GEM brochure.

Pure and network-free: every function here takes brochure JSON and returns
evidence, so the whole thing is testable against captured fixtures with no
portal, no token and no clock.

**Why this is not a fixed parser.** The five captured brochures publish course
load five different ways, verbatim:

* Aalborg — a table: ``Minimum | Maximum`` / ``30 ECTS | 30 ECTS``
* Ajou — a table whose cells carry two units at once:
  ``6 credits (usually 2 courses)`` to ``19 credits (usually 6~7 courses)``
* Akita — prose lines, and the floor is a legal constraint, not an academic one:
  ``Minimum : 12 credits (required by Japanese immigration law)``
* Queen's — a stated equivalence: ``15 credits per term (equivalent to 30
  ECTS/year)``
* McGill — courses first, credits second: ``4 courses (12 credits)``

No single regular expression covers that set, and no averaged "credits" number
would be true of any of them. So the extraction keeps the university's own
words and only ever *adds* structure it can defend.

The rule that matters: :attr:`conversion_status` is ``supported`` **only** when
the source text itself states an equivalence. Otherwise it is ``unsupported``,
and the system says so rather than inventing a factor.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass
from typing import Any, Iterable

from graph.domain import (
    ConversionBasis,
    EligibilityEvidence,
    FinanceEvidence,
    SourceEvidence,
    WorkloadEvidence,
    utc_now,
)

COURSEWORK_SECTIONS = ("coursework", "academics", "courses")
FINANCIAL_SECTIONS = ("financial", "finances", "cost")

# Units seen across the portal, mapped to the label the university used.
_UNIT_WORDS = r"(ects|credits?|credit hours?|units?|courses?|modules?|mcs?|papers?)"

_COURSE_LOAD_HEADINGS = re.compile(
    r"(course\s*load|study\s*load|full[- ]?time\s*study\s*load|academic\s*load|course\s*work\s*load)",
    re.IGNORECASE,
)

# "Minimum : 12 credits", "Recommended minimum: 15 credits per term"
_LABELLED_VALUE = re.compile(
    rf"\b(minimum|maximum|min|max)\b[^\n:|]{{0,24}}?[:|]?\s*"
    rf"(\d+(?:\.\d+)?)\s*{_UNIT_WORDS}",
    re.IGNORECASE,
)

# "6 credits (usually 2 courses)", "4 courses (12 credits)"
_VALUE_WITH_UNIT = re.compile(rf"(\d+(?:\.\d+)?)\s*{_UNIT_WORDS}", re.IGNORECASE)

# "can take up to 30 Queen's units per year" states a ceiling without the word.
_UP_TO = re.compile(rf"\bup to\s+(\d+(?:\.\d+)?)\s*[\w'’]*\s*{_UNIT_WORDS}", re.IGNORECASE)

# The only thing that licenses a conversion: the source saying so itself.
# "15 credits per term (equivalent to 30 ECTS/year)", "2 ECTS = 1 AU"
_CONVERSION_BASIS = re.compile(
    rf"(\d+(?:\.\d+)?)\s*{_UNIT_WORDS}[^.()\n]{{0,40}}"
    rf"(?:\(?\s*(?:is\s+)?(?:equivalent\s+to|equals?|=)\s*)"
    rf"(\d+(?:\.\d+)?)\s*{_UNIT_WORDS}",
    re.IGNORECASE,
)

_CGPA_PARAMETER = re.compile(r"c?gpa", re.IGNORECASE)
_CGPA_VALUE = re.compile(r"(\d(?:\.\d+)?)\s*(?:/\s*(\d(?:\.\d+)?))?")

_MONEY = re.compile(
    r"(?:(?P<code>[A-Z]{3})\s*|(?P<symbol>[$€£¥₩₹])\s*)?"
    r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?P<trailing>[A-Z]{3})?"
)
_SYMBOL_TO_CODE = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₩": "KRW", "₹": "INR"}

# ISO-4217 codes for the 34 countries that actually have partner universities.
# An allow-list, because a bare three-letter-uppercase match reads "CPR" (the
# Danish national ID mentioned in Aalborg's housing text) as a currency.
CURRENCY_CODES = frozenset(
    {
        "AUD", "BND", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR", "GBP", "HKD",
        "HUF", "IDR", "INR", "JPY", "KRW", "MOP", "MYR", "NOK", "NZD", "PLN",
        "RMB", "SEK", "SGD", "THB", "TRY", "TWD", "USD", "VND",
    }
)


# --- html -----------------------------------------------------------------


def to_text(raw_html: str | None) -> str:
    """Flatten brochure HTML to readable text, keeping table structure.

    Table cells become ``|`` separated, because the Minimum/Maximum pairing in
    a two-column table is the meaning, not decoration.
    """
    if not raw_html:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
    # Collapse the source's own newlines *first*. The portal's markup wraps
    # freely inside table cells, so leaving them in scatters one table row
    # across five lines and destroys the Minimum/Maximum pairing that carries
    # the meaning.
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</tr>|</table>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</t[dh]>", " | ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    # Zero-width and non-breaking spaces are everywhere in this content and
    # break otherwise-correct patterns.
    text = text.replace("​", "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def sections(brochure: dict[str, Any]) -> dict[str, str]:
    """Every brochure section as flattened text, keyed by its display name."""
    current = (brochure or {}).get("current") or {}
    out: dict[str, str] = {}
    for section in current.get("sections") or []:
        name = (section.get("sectionDisplayName") or "").strip()
        chunks = [
            to_text(widget.get("contentHTML"))
            for widget in section.get("sectionWidgets") or []
            if widget.get("contentHTML")
        ]
        body = "\n".join(chunk for chunk in chunks if chunk)
        if name:
            out[name] = (out.get(name, "") + "\n" + body).strip() if name in out else body
    return out


def _section_matching(section_map: dict[str, str], keywords: Iterable[str]) -> tuple[str, str]:
    for name, body in section_map.items():
        lowered = name.lower()
        if any(word in lowered for word in keywords):
            return name, body
    return "", ""


def information_parameters(brochure: dict[str, Any]) -> list[dict[str, Any]]:
    """The brochure's structured parameter list, where the CGPA lives."""
    current = (brochure or {}).get("current") or {}
    found: list[dict[str, Any]] = []
    for section in current.get("sections") or []:
        for widget in section.get("sectionWidgets") or []:
            sheet = widget.get("contentInformationSheet")
            if isinstance(sheet, dict):
                found.extend(p for p in sheet.get("parameters") or [] if isinstance(p, dict))
    return found


# --- programme dates ------------------------------------------------------


@dataclass(frozen=True)
class ProgrammeDates:
    """Dates published for one exchange term in a GEM brochure."""

    term: str
    section_name: str
    items: tuple[tuple[str, str], ...]
    source_excerpt: str


_PROGRAMME_DATE_HEADING = re.compile(
    r"programme\s+dates?\s*\(\s*(fall|spring|autumn)\s*\)",
    re.IGNORECASE,
)

# Brochures sometimes qualify their native unit with the institution's name:
# "1 Hanyang credit equals 2 ECTS". The qualifier is descriptive and is not
# treated as a new unit; the captured unit remains CREDIT/ECTS.
_QUALIFIED_CONVERSION_BASIS = re.compile(
    rf"(?P<from_value>\d+(?:\.\d+)?)\s+"
    rf"(?:[A-Za-z][\w/&-]*\s+)*(?P<from_unit>{_UNIT_WORDS.replace('(', '(?:')})\s*"
    rf"(?:is\s+)?(?:equivalent\s+to|equals?|=)\s*"
    rf"(?P<to_value>\d+(?:\.\d+)?)\s*(?P<to_unit>{_UNIT_WORDS.replace('(', '(?:')})",
    re.IGNORECASE,
)

# "ranges from 15 to 18 credits", "15-18 ECTS", and typographic dash
# variants all carry a useful bound even when no Minimum/Maximum table exists.
_RANGE_VALUE = re.compile(
    rf"\b(\d+(?:\.\d+)?)\s*(?:to|[-–—])\s*(\d+(?:\.\d+)?)\s*{_UNIT_WORDS}",
    re.IGNORECASE,
)

_WORD_COUNT = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
_WORD_COURSE_COUNT = re.compile(
    r"\b(?:(at\s+least|minimum\s+of|up\s+to|maximum\s+of)\s+)?"
    r"(one|two|three|four|five|six|seven)\s+(?:regular\s+)?"
    r"(courses?|modules?|papers?)\b",
    re.IGNORECASE,
)
_UNDERGRAD_COURSE_COUNT = re.compile(
    r"\b(\d+(?:\.\d+)?)\s+(?:undergraduate|undergrad|bachelor(?:'s)?)\s+"
    r"(?:courses?|modules?|papers?)\b",
    re.IGNORECASE,
)
_DATE_ITEM_LABELS = {
    "arrival": "Arrival",
    "orientation": "Orientation",
    "first class": "First Class",
    "last class": "Last Class",
    "exam period": "Exam Period",
    "exam": "Exam Period",
}
_PLACEHOLDER_DATES = re.compile(
    r"^(?:tba|tbc|xxx|to\s+be\s+announced|not\s+available|n/?a|-)\.?$",
    re.IGNORECASE,
)
_TERM_ROW = re.compile(r"^semester\s*[12]\b[^|]*", re.IGNORECASE)


def _date_value(raw: str) -> str:
    value = re.sub(r"\s+", " ", (raw or "").replace("\xa0", " ")).strip(" |\t")
    if _PLACEHOLDER_DATES.fullmatch(value):
        return "To be announced (TBA)"
    return value


def _requested_term(term: str | None) -> str | None:
    value = (term or "").lower()
    if re.search(r"\b(?:semester\s*1|sem\s*1|fall|autumn)\b", value):
        return "fall"
    if re.search(r"\b(?:semester\s*2|sem\s*2|spring)\b", value):
        return "spring"
    return None


def _parse_date_blocks(text: str, section_name: str) -> list[ProgrammeDates]:
    matches = list(_PROGRAMME_DATE_HEADING.finditer(text or ""))
    parsed: list[ProgrammeDates] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end() : end].strip()
        items: list[tuple[str, str]] = []
        for line in block.splitlines():
            cells = [re.sub(r"\s+", " ", cell).strip() for cell in line.split("|")]
            if len(cells) < 2:
                continue
            label = _DATE_ITEM_LABELS.get(cells[0].lower().rstrip(":"))
            if not label:
                continue
            value = _date_value(" | ".join(cell for cell in cells[1:] if cell))
            if not value:
                continue
            items.append((label, value))
        if items:
            parsed.append(
                ProgrammeDates(
                    term=match.group(1).title() if match.group(1).lower() != "autumn" else "Fall",
                    section_name=section_name,
                    items=tuple(items),
                    source_excerpt=re.sub(r"\s+", " ", text[match.start() : end]).strip()[:1200],
                )
            )
    return parsed


def _parse_overview_rows(text: str, section_name: str) -> list[ProgrammeDates]:
    """Read the overview table only as a fallback for missing date blocks."""
    parsed: list[ProgrammeDates] = []
    for line in (text or "").splitlines():
        cells = [re.sub(r"\s+", " ", cell).strip() for cell in line.split("|")]
        while cells and not cells[-1]:
            cells.pop()
        if len(cells) < 2 or not _TERM_ROW.match(cells[0]):
            continue
        term_match = re.search(r"\b(fall|spring|autumn)\b", cells[0], re.IGNORECASE)
        if not term_match:
            continue
        # GEM's overview table places Start Date and End Date in the final
        # two columns. Keep that structure instead of guessing from nearby
        # numbers or from a current calendar year.
        values = [_date_value(cell) for cell in cells[-2:]]
        items = []
        if values[0]:
            items.append(("Start Date", values[0]))
        if values[1]:
            items.append(("End Date", values[1]))
        if items:
            parsed.append(
                ProgrammeDates(
                    term=term_match.group(1).title() if term_match.group(1).lower() != "autumn" else "Fall",
                    section_name=section_name,
                    items=tuple(items),
                    source_excerpt=line[:1200],
                )
            )
    return parsed


def parse_programme_dates(
    brochure: dict[str, Any], term: str | None = None
) -> list[ProgrammeDates]:
    """Extract exchange dates, prioritising ``Others & Contact``.

    The portal has no stable field schema for these dates: some brochures use
    a programme-date table, some publish only a start/end row in Programme
    Overview, and some explicitly say TBA. This parser preserves the page's
    labels and values and never derives dates from today's date.
    """
    section_map = sections(brochure)
    requested = _requested_term(term)

    others_name = next((name for name in section_map if "other" in name.lower()), "")
    if not others_name:
        others_name = next((name for name in section_map if "contact" in name.lower()), "")
    others = _parse_date_blocks(section_map.get(others_name, ""), others_name)
    if requested:
        others = [item for item in others if item.term.lower() == requested]
    if others:
        return others

    overview_name = next(
        (name for name in section_map if "programme" in name.lower() and "overview" in name.lower()),
        "",
    )
    overview = _parse_overview_rows(section_map.get(overview_name, ""), overview_name)
    if requested:
        overview = [item for item in overview if item.term.lower() == requested]
    return overview


# --- workload -------------------------------------------------------------


def _excerpt_around(text: str, start: int, end: int, width: int = 220) -> str:
    left = max(0, start - width // 3)
    right = min(len(text), end + width)
    return re.sub(r"\s+", " ", text[left:right]).strip()


def _course_load_block(text: str) -> tuple[str, int]:
    """The passage that states the course load, and where it starts.

    Anchored on the heading, so a credit figure mentioned elsewhere in the
    section (a tuition line, a prerequisite) is not read as a workload rule.
    """
    match = _COURSE_LOAD_HEADINGS.search(text)
    if not match:
        return text, 0
    start = match.start()
    # Stop at the next heading-like line so we do not run into "Course Offering".
    rest = text[match.end() :]
    stop = re.search(
        r"\n\s*(course offering|programs? available|restricted|language of|"
        r"course registration|academic calendar|grading)",
        rest,
        re.IGNORECASE,
    )
    end = match.end() + (stop.start() if stop else min(len(rest), 900))
    return text[start:end], start


def _unit_label(raw: str) -> str:
    """Canonical display form for a unit word.

    ECTS is already the acronym's own spelling; naively stripping a trailing
    "s" would print "ECT", which is not a unit anyone recognises.
    """
    word = (raw or "").strip().upper()
    if word in {"ECTS", "MC", "MCS", "AU", "AUS"}:
        return "MC" if word.startswith("MC") else ("AU" if word.startswith("AU") else "ECTS")
    return word[:-1] if word.endswith("S") and len(word) > 3 else word


def _find_conversion_basis(text: str, url: str) -> ConversionBasis | None:
    """A conversion is permitted only when the source states the equivalence."""
    match = _CONVERSION_BASIS.search(text)
    if match:
        try:
            from_value = float(match.group(1))
            to_value = float(match.group(3))
        except (TypeError, ValueError):
            return None
        from_unit = _unit_label(match.group(2))
        to_unit = _unit_label(match.group(4))
    else:
        qualified = _QUALIFIED_CONVERSION_BASIS.search(text)
        if not qualified:
            return None
        match = qualified
        try:
            from_value = float(qualified.group("from_value"))
            to_value = float(qualified.group("to_value"))
        except (TypeError, ValueError):
            return None
        from_unit = _unit_label(qualified.group("from_unit"))
        to_unit = _unit_label(qualified.group("to_unit"))
    if from_value <= 0:
        return None
    return ConversionBasis(
        from_unit=from_unit,
        to_unit=to_unit,
        factor=round(to_value / from_value, 6),
        source_excerpt=_excerpt_around(text, match.start(), match.end()),
        source_url=url or None,
    )


def _labelled_bounds(block: str) -> dict[str, tuple[float, str]]:
    """Minimum/maximum stated in prose: ``Minimum : 12 credits``."""
    bounds: dict[str, tuple[float, str]] = {}
    for match in _LABELLED_VALUE.finditer(block):
        key = "minimum" if match.group(1).lower().startswith("min") else "maximum"
        if key in bounds:
            continue
        try:
            bounds[key] = (float(match.group(2)), match.group(3).lower())
        except ValueError:
            continue
    if "maximum" not in bounds:
        ceiling = _UP_TO.search(block)
        if ceiling:
            try:
                bounds["maximum"] = (float(ceiling.group(1)), ceiling.group(2).lower())
            except ValueError:
                pass
    return bounds


def _table_bounds(block: str) -> list[tuple[float, str]]:
    """Minimum/maximum stated as two table cells under a Minimum|Maximum header.

    The header itself is matched loosely because the portal's own markup splits
    words ("Mi nimum") when an editor has pasted styled text.
    """
    # No digits may sit between the two words: "Minimum | Maximum" is a header,
    # while "Minimum : 12 credits ... Maximum : 18" is two prose lines.
    header = re.search(
        r"mi\s*nimum[^0-9\n]{0,60}?max\s*imum", block, re.IGNORECASE
    )
    if not header:
        return []
    after = block[header.end() :]
    values: list[tuple[float, str]] = []
    for match in _VALUE_WITH_UNIT.finditer(after[:400]):
        try:
            values.append((float(match.group(1)), match.group(2).lower()))
        except ValueError:
            continue
    if not values:
        return []
    # A cell can carry two units at once - Ajou publishes "6 credits (usually 2
    # courses)". Pairing 6 credits with 2 courses would read as a minimum above
    # its own maximum, so keep only the figures sharing the leading unit.
    leading = values[0][1]
    same_unit = [v for v in values if v[1] == leading]
    return same_unit if len(same_unit) >= 2 else values


def _range_bounds(block: str) -> tuple[float, float, str] | None:
    """Read a prose range such as ``15 to 18 credits``."""
    match = _RANGE_VALUE.search(block)
    if not match:
        return None
    try:
        low = float(match.group(1))
        high = float(match.group(2))
    except ValueError:
        return None
    if low > high:
        low, high = high, low
    return low, high, match.group(3).lower()


def _workload_summary(
    block: str,
    unit_label: str | None,
    minimum: float | None,
    maximum: float | None,
    module_min: int | None,
    module_max: int | None,
    basis: ConversionBasis | None,
) -> str:
    """Create a compact, readable rule while retaining the full source excerpt."""
    raw_unit = (unit_label or "units").lower()
    unit = "ECTS" if raw_unit.startswith("ects") else ("AU" if raw_unit.startswith("au") else raw_unit)
    range_values = _range_bounds(block)
    if range_values and unit_label and range_values[2] != unit_label.lower():
        range_values = None
    parts: list[str] = []
    if range_values:
        low, high, range_unit = range_values
        parts.append(f"Typical range: {low:g}–{high:g} {range_unit}")
        if maximum is not None and maximum != high:
            parts.append(f"maximum: {maximum:g} {unit}")
    elif minimum is not None and maximum is not None:
        parts.append(
            f"{minimum:g} {unit}" if minimum == maximum else f"{minimum:g}–{maximum:g} {unit}"
        )
    elif maximum is not None:
        parts.append(f"up to {maximum:g} {unit}")
    elif minimum is not None:
        parts.append(f"at least {minimum:g} {unit}")

    course_label = (
        "undergraduate courses"
        if _UNDERGRAD_COURSE_COUNT.search(block)
        else "courses"
    )
    if module_min is not None and module_max is not None:
        parts.append(
            f"typically {module_min} {course_label[:-1] if module_min == 1 else course_label}"
            if module_min == module_max
            else f"typically {module_min}–{module_max} {course_label}"
        )
    elif module_min is not None:
        singular = course_label[:-1] if module_min == 1 else course_label
        parts.append(f"at least {module_min} {singular}")
    elif module_max is not None:
        parts.append(f"up to {module_max} {course_label}")
    if basis is not None:
        parts.append(
            f"published conversion: 1 {basis.from_unit} = {basis.factor:g} {basis.to_unit}"
        )
    if not parts:
        # This should only be reached for an unusual prose-only rule. Keep a
        # bounded passage for display instead of dumping the whole section.
        return re.sub(r"\s+", " ", block).strip()[:360]
    return "; ".join(parts)


def _planning_reference(
    unit_label: str | None,
    minimum: float | None,
    maximum: float | None,
    basis: ConversionBasis | None,
) -> str | None:
    """Give an ECTS-only planning aid, clearly separate from official evidence."""
    uses_ects = (unit_label or "").lower().startswith("ects") or (
        basis is not None and basis.to_unit.upper() == "ECTS"
    )
    if not uses_ects:
        return None

    def display_number(value: float) -> str:
        return str(int(round(value))) if value.is_integer() else f"{value:.1f}"

    # If GEM publishes a university-specific conversion into ECTS, use that
    # stated basis before applying the separate NTU planning reference. Native
    # credits must never be labelled ECTS just because both appear in a rule.
    ects_minimum = minimum
    ects_maximum = maximum
    if (
        basis is not None
        and basis.to_unit.upper() == "ECTS"
        and (unit_label or "").lower()[:6] != "ects"
    ):
        if ects_minimum is not None:
            ects_minimum = round(ects_minimum * basis.factor, 2)
        if ects_maximum is not None:
            ects_maximum = round(ects_maximum * basis.factor, 2)

    amounts: list[str] = []
    if ects_minimum is not None:
        amounts.append(f"{ects_minimum:g}")
    if ects_maximum is not None and ects_maximum != ects_minimum:
        amounts.append(f"{ects_maximum:g}")
    if len(amounts) == 2:
        ects = f"{amounts[0]}–{amounts[1]} ECTS"
        au = f"{display_number(ects_minimum / 2)}–{display_number(ects_maximum / 2)} AUs" if ects_minimum is not None and ects_maximum is not None else "an indicative AU range"
        modules = f" ({display_number(ects_minimum / 6)}–{display_number(ects_maximum / 6)} NTU modules)" if ects_minimum is not None and ects_maximum is not None else ""
    elif amounts:
        value = ects_minimum if ects_minimum is not None else ects_maximum
        ects = f"{amounts[0]} ECTS"
        au = f"about {display_number(value / 2)} AUs" if value is not None else "an indicative AU figure"
        modules = f" (about {display_number(value / 6)} NTU modules)" if value is not None else ""
    else:
        ects, au, modules = "the published ECTS load", "an indicative AU figure", ""
    return (
        f"Rough planning reference only: 6 ECTS is often treated as about 1 NTU module "
        f"(3 AUs). For {ects}, that is approximately {au}{modules}. "
        "This is not an official conversion; confirm the AU award with your school."
    )


def _module_counts(block: str) -> tuple[int | None, int | None]:
    """Course counts, whether stated as the unit or in a parenthetical aside."""
    undergraduate = [
        int(float(match.group(1)))
        for match in _UNDERGRAD_COURSE_COUNT.finditer(block)
    ]
    if undergraduate:
        return min(undergraduate), max(undergraduate)

    def graduate_only_context(start: int, end: int) -> bool:
        """Do not let a graduate-only sentence supply an undergraduate count."""
        left = max(
            block.rfind(".", 0, start),
            block.rfind(";", 0, start),
            block.rfind("|", 0, start),
            block.rfind("\n", 0, start),
        ) + 1
        right_candidates = [
            position for marker in (".", ";", "|", "\n")
            if (position := block.find(marker, end)) >= 0
        ]
        right = min(right_candidates) if right_candidates else len(block)
        sentence = block[left:right]
        return bool(
            re.search(r"\bgraduate|postgraduate|master(?:'s)?|doctoral|ph\.?d\b", sentence, re.IGNORECASE)
        ) and not bool(
            re.search(r"\bundergraduate|undergrad|bachelor(?:'s)?\b", sentence, re.IGNORECASE)
        )

    counts = [
        (float(m.group(1)), m.group(2).lower())
        for m in _VALUE_WITH_UNIT.finditer(block)
        if m.group(2).lower().startswith(("course", "module", "paper"))
        and not graduate_only_context(m.start(), m.end())
    ]
    for match in _RANGE_VALUE.finditer(block):
        if match.group(3).lower().startswith(("course", "module", "paper")):
            if not graduate_only_context(match.start(), match.end()):
                counts.extend(
                    [
                        (float(match.group(1)), match.group(3).lower()),
                        (float(match.group(2)), match.group(3).lower()),
                    ]
                )
    minimum: int | None = None
    maximum: int | None = None
    for match in _WORD_COURSE_COUNT.finditer(block):
        if graduate_only_context(match.start(), match.end()):
            continue
        value = _WORD_COUNT[match.group(2).lower()]
        qualifier = (match.group(1) or "").lower()
        if qualifier in {"at least", "minimum of"}:
            minimum = max(minimum or 0, value)
        elif qualifier in {"up to", "maximum of"}:
            maximum = max(maximum or 0, value)
        else:
            minimum = value if minimum is None else min(minimum, value)
            maximum = value if maximum is None else max(maximum, value)
    # "6~7 courses" leaves a bare 6 in front; keep the numbers we can defend.
    numbers = sorted({int(value) for value, _ in counts if 0 < value < 30})
    if numbers:
        minimum = numbers[0] if minimum is None else min(minimum, numbers[0])
        maximum = numbers[-1] if maximum is None else max(maximum, numbers[-1])
    return minimum, maximum


def parse_workload(
    brochure: dict[str, Any], university_name: str, url: str = ""
) -> tuple[WorkloadEvidence | None, list[str]]:
    """Extract the host's course-load rule in its own units.

    Returns ``(evidence, warnings)``. ``None`` means the brochure does not
    publish a course load — a normal outcome that must be reported as such
    rather than filled in.
    """
    warnings: list[str] = []
    section_map = sections(brochure)
    section_name, body = _section_matching(section_map, COURSEWORK_SECTIONS)
    if not body:
        return None, ["gem_no_coursework_section"]

    block, _ = _course_load_block(body)
    if not _COURSE_LOAD_HEADINGS.search(body):
        warnings.append("gem_no_course_load_heading: searched the whole coursework section")

    unit_label: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    prose_range = _range_bounds(block)

    # The table layout is tried first and deliberately. Under a
    # ``Minimum | Maximum`` header the first figure is the minimum by position,
    # but a label-driven regex reads "Maximum | 6 credits" and binds Ajou's
    # *minimum* of 6 to the maximum. Position beats proximity here.
    table = _table_bounds(block)
    if len(table) >= 2:
        minimum, unit_label = table[0]
        maximum = table[1][0]
        if table[1][1] != unit_label:
            warnings.append(
                f"gem_mixed_units: minimum in '{unit_label}', maximum in '{table[1][1]}'"
            )
    else:
        labelled = _labelled_bounds(block)
        if "minimum" in labelled:
            minimum, unit_label = labelled["minimum"]
        if "maximum" in labelled:
            maximum = labelled["maximum"][0]
            unit_label = unit_label or labelled["maximum"][1]
        if len(table) == 1 and minimum is None and maximum is None:
            minimum, unit_label = table[0]
            warnings.append("gem_single_bound: only one course-load figure was published")
        if prose_range and (
            unit_label is None or prose_range[2] == unit_label.lower()
        ):
            range_min, range_max, range_unit = prose_range
            minimum = range_min
            unit_label = unit_label or range_unit
            if maximum is None:
                maximum = range_max

    if minimum is not None and maximum is not None and minimum > maximum:
        warnings.append(
            f"gem_bounds_swapped: read a minimum of {minimum:g} above a maximum of {maximum:g}; "
            "reporting the published wording rather than the numbers"
        )
        minimum, maximum = None, None

    labelled_units = _labelled_bounds(block)
    if (
        "minimum" in labelled_units
        and "maximum" in labelled_units
        and labelled_units["minimum"][1] != labelled_units["maximum"][1]
    ):
        warnings.append(
            f"gem_mixed_units: minimum stated in '{labelled_units['minimum'][1]}', "
            f"maximum in '{labelled_units['maximum'][1]}'"
        )
    periods = set(re.findall(r"per (term|semester|year|session)", block, re.IGNORECASE))
    if len({p.lower() for p in periods}) > 1:
        warnings.append(
            "gem_mixed_periods: the published figures cover different periods "
            f"({', '.join(sorted(p.lower() for p in periods))}); compare them with care"
        )

    module_min, module_max = _module_counts(block)

    if minimum is None and maximum is None and module_min is None:
        return None, [*warnings, "gem_no_course_load_found"]

    # A generic "credits" is preserved as the university's own wording. It is
    # never relabelled ECTS or AU: those are different systems, and the whole
    # point of this module is that no factor relates them universally.
    if unit_label and unit_label.startswith("credit"):
        warnings.append(
            "Generic credits preserved as native wording; no ECTS/AU inference made."
        )

    basis = _find_conversion_basis(block, url)
    if basis is not None:
        status = "supported"
    elif unit_label is None:
        status = "not_applicable"
    else:
        status = "unsupported"

    native = _workload_summary(
        block, unit_label, minimum, maximum, module_min, module_max, basis
    )
    planning_reference = _planning_reference(unit_label, minimum, maximum, basis)

    evidence = WorkloadEvidence(
        university_name=university_name,
        native_unit_text=native[:400],
        unit_label=unit_label,
        minimum_value=minimum,
        maximum_value=maximum,
        module_count_minimum=module_min,
        module_count_maximum=module_max,
        raw_source_excerpt=block[:1200].strip(),
        source_url=url,
        retrieved_at=utc_now(),
        conversion_status=status,
        conversion_basis=basis,
        planning_reference=planning_reference,
        parse_warnings=warnings,
    )
    return evidence, warnings


# --- eligibility ----------------------------------------------------------


def parse_cgpa(
    brochure: dict[str, Any], university_name: str, url: str = "", student_cgpa: float | None = None
) -> EligibilityEvidence | None:
    """The published minimum CGPA, checked against the student's own.

    ``meets`` is deliberately tri-state. When a brochure publishes no CGPA the
    answer is ``None`` — "not published" — and the university stays on the
    shortlist. Dropping it silently would hide an option the student is very
    possibly eligible for.
    """
    for parameter in information_parameters(brochure):
        name = str(parameter.get("parameterName") or "")
        if not _CGPA_PARAMETER.search(name):
            continue
        values = [str(v) for v in parameter.get("assignedValues") or [] if str(v).strip()]
        if not values:
            continue
        raw = values[0].strip()
        match = _CGPA_VALUE.search(raw)
        required: float | None = None
        notes: list[str] = []
        if match:
            try:
                required = float(match.group(1))
            except ValueError:
                required = None
            scale = match.group(2)
            if scale and abs(float(scale) - 5.0) > 0.01:
                notes.append(
                    f"Published on a {scale} scale, while NTU grades on 5.0. Compare with care."
                )
                required = None
        if required is None and not notes:
            notes.append(f"Could not read a number from the published requirement '{raw}'.")

        meets: bool | None = None
        if required is not None and student_cgpa is not None:
            meets = student_cgpa >= required

        return EligibilityEvidence(
            university_name=university_name,
            requirement=f"{name}: {raw}",
            required_value=required,
            student_value=student_cgpa,
            meets=meets,
            source=SourceEvidence(
                title=f"GEM Explorer — {university_name} entry requirements",
                url=url,
                type="gem_explorer",
                excerpt=f"{name}: {raw}",
                retrieved_at=utc_now(),
                confidence="high",
            )
            if url
            else None,
            notes=notes,
        )
    return None


# --- finance --------------------------------------------------------------


def _currency_of(text: str) -> str | None:
    """The currency a passage is denominated in, or ``None``.

    Only real ISO codes count. Accepting any three capitals reads "CPR" — the
    Danish national ID number, which Aalborg's brochure mentions — as a
    currency, and then silently mislabels every figure beneath it.
    """
    for candidate in re.findall(r"\b([A-Z]{3})\b", text or ""):
        if candidate in CURRENCY_CODES:
            return "CNY" if candidate == "RMB" else candidate
    for symbol, code in _SYMBOL_TO_CODE.items():
        if symbol in (text or ""):
            return code
    return None


def parse_finance(
    brochure: dict[str, Any], university_name: str, url: str = ""
) -> tuple[list[FinanceEvidence], list[str]]:
    """Published cost figures, each kept in its own currency and period.

    Figures are never merged or converted here. A brochure that publishes
    nothing usable returns an empty list, which is what lets the finance lane
    decide honestly whether it has enough to answer.
    """
    warnings: list[str] = []
    section_map = sections(brochure)
    section_name, body = _section_matching(section_map, FINANCIAL_SECTIONS)
    if not body.strip():
        return [], ["gem_no_financial_section"]

    currency = _currency_of(body)
    period = "monthly" if re.search(r"per month|/month|monthly", body, re.I) else None
    if period is None and re.search(r"per semester|/semester", body, re.I):
        period = "semester"

    evidence: list[FinanceEvidence] = []
    for line in body.split("\n"):
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if len(cells) < 2:
            continue
        label = cells[0]
        if not re.search(r"[A-Za-z]", label) or len(label) > 60:
            continue
        # The currency must come from the same cell as the amount. Ajou's table
        # has a KRW column beside a USD-equivalent column; reading the row as a
        # whole labels 150,000 KRW as USD, a thousand-fold error.
        value_cell = ""
        amount = None
        for cell in cells[1:]:
            found = re.findall(r"\d[\d,]*(?:\.\d+)?", cell)
            if not found:
                continue
            try:
                amount = float(found[0].replace(",", ""))
            except ValueError:
                continue
            value_cell = cell
            break
        if amount is None or amount < 1:
            continue
        evidence.append(
            FinanceEvidence(
                university_name=university_name,
                label=label[:60],
                amount=amount,
                currency=_currency_of(value_cell) or currency,
                period=period,
                includes_rent=None,
                included_items=[],
                excluded_items=[],
                raw_source_excerpt=re.sub(r"\s+", " ", line).strip()[:300],
                source=SourceEvidence(
                    title=f"GEM Explorer — {university_name} financial information",
                    url=url,
                    type="gem_explorer",
                    excerpt=re.sub(r"\s+", " ", line).strip()[:300],
                    retrieved_at=utc_now(),
                    confidence="high",
                )
                if url
                else None,
                parse_warnings=[],
            )
        )

    if not evidence:
        warnings.append("gem_no_financial_figures: the section publishes no usable amounts")
    if currency is None and evidence:
        warnings.append("gem_currency_unknown: amounts are unlabelled")
    return evidence, warnings


def sufficient(evidence: list[FinanceEvidence]) -> bool:
    """Whether the published figures can carry a budget on their own.

    A pure predicate, so the decision to fall back to a cost-of-living estimate
    is testable and always documented rather than ad hoc.
    """
    usable = [e for e in evidence if e.amount and e.currency]
    currencies = {e.currency for e in usable}
    if len(currencies) == 1 and any("total" in (e.label or "").lower() for e in usable):
        return True
    if len(usable) < 2:
        return False
    return len(currencies) == 1
