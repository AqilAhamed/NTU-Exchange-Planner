"""Explicit unit conversion, and nothing else.

This lane runs only when a student asked for a conversion in so many words.
It is the sole caller of :mod:`data.au_units`, and every number it produces
carries that module's permanent rule-of-thumb caveat.

What it must never be used for: describing a host university's workload. That
comes from the university's own published wording, via the GEM parser.
"""

from __future__ import annotations

import re

from data.au_units import RULE_OF_THUMB_CAVEAT, convert, normalise_unit
from graph.domain import Conversion, SourceEvidence, utc_now
from graph.state import LaneResult

LANE = "conversion"

# "30 ECTS to AU", "convert 4 AU into ECTS", "how many AU is 30 ECTS"
_EXPLICIT = re.compile(
    r"(\d+(?:\.\d+)?)\s*([A-Za-z][A-Za-z ]{0,14}?)\s*(?:to|into|in|=|equals?|as)\s+"
    r"([A-Za-z][A-Za-z ]{0,14}?)\b",
    re.IGNORECASE,
)
_REVERSED = re.compile(
    r"how many\s+([A-Za-z][A-Za-z ]{0,14}?)\s+(?:is|are|in)\s+(\d+(?:\.\d+)?)\s*"
    r"([A-Za-z][A-Za-z ]{0,14}?)\b",
    re.IGNORECASE,
)


def parse_request(message: str) -> tuple[float, str, str] | None:
    """Read ``(value, from_unit, to_unit)`` out of the student's own words."""
    text = (message or "").strip()

    match = _REVERSED.search(text)
    if match:
        target, value, source = match.group(1), match.group(2), match.group(3)
        try:
            return float(value), normalise_unit(source), normalise_unit(target)
        except ValueError:
            return None

    match = _EXPLICIT.search(text)
    if match:
        value, source, target = match.group(1), match.group(2), match.group(3)
        try:
            return float(value), normalise_unit(source), normalise_unit(target)
        except ValueError:
            return None
    return None


def run(message: str) -> dict:
    """Perform the conversion the student asked for, or say why it cannot be done."""
    request = parse_request(message)
    if request is None:
        return {
            "caveats": [
                "I could not tell which units you wanted converted. Try, for example, "
                "'convert 30 ECTS to AU'."
            ],
            "lane_results": {
                LANE: LaneResult(lane=LANE, status="skipped", note="no parseable conversion request")
            },
        }

    value, source, target = request
    result = convert(value, source, target)

    conversion = Conversion(
        kind="au",
        summary=result.summary,
        details={
            "input_value": result.input_value,
            "from_unit": result.from_unit,
            "to_unit": result.to_unit,
            "converted_value": result.value,
            "supported": result.supported,
            "basis": result.basis,
        },
    )

    sources: list[SourceEvidence] = []
    if result.supported and result.basis == "rule_of_thumb":
        sources.append(
            SourceEvidence(
                title="Rule-of-thumb academic unit conversion (not authoritative)",
                url="/backend/api/health",
                type="local",
                excerpt=RULE_OF_THUMB_CAVEAT,
                retrieved_at=utc_now(),
                confidence="low",
            )
        )

    return {
        "conversion_results": [conversion],
        "sources": sources,
        "caveats": list(result.caveats),
        "lane_results": {
            LANE: LaneResult(
                lane=LANE,
                status="complete" if result.supported else "complete",
                note=f"{result.from_unit} to {result.to_unit}"
                + ("" if result.supported else " (not supported)"),
            )
        },
    }
