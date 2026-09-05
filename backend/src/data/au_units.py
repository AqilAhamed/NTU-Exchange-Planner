"""Academic-unit arithmetic, quarantined behind an explicit request.

⚠️ **This module has one legitimate caller: :mod:`agents.au_agent`, and only
when a student has explicitly asked for a conversion.** It must not be imported
by the mapping lane or the finance lane.

The reason is a real defect in the previous build. It carried a hardcoded
``2 ECTS = 1 AU`` table and applied it *by default* to every host university,
printing confident AU figures for universities that publish their workload in
credits, modules, or nothing at all. Aalborg publishes "min 30 / max 30 ECTS";
Ajou "6 credits (usually 2 courses)" through "19 credits (usually 6~7
courses)"; Akita "minimum 12 credits (required by Japanese immigration law),
maximum 18… typically 5 courses". No single factor is true across that set, and
inventing one is worse than saying so.

So: every result this module produces carries :data:`RULE_OF_THUMB_CAVEAT`,
permanently and un-removably. A conversion with a *cited* basis is a different
thing entirely and lives in :class:`graph.domain.ConversionBasis`, produced by
the GEM parser from the university's own words.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Widely repeated guidance, not an authority. Named as a rule of thumb in the
# constant itself so no call site can mistake it for a published factor.
RULE_OF_THUMB_FACTORS: dict[tuple[str, str], float] = {
    ("ECTS", "AU"): 0.5,
    ("AU", "ECTS"): 2.0,
}

RULE_OF_THUMB_CAVEAT = (
    "This uses the widely quoted rule of thumb that 2 ECTS is roughly 1 NTU AU. "
    "It is NOT an official conversion: NTU publishes no universal factor, and each "
    "host university sets its own credit weighting. Your school's exchange "
    "coordinator must confirm the actual AU award before you rely on this."
)

KNOWN_UNITS = frozenset({"ECTS", "AU", "CREDITS", "MC", "MODULES"})

# Units we deliberately refuse to convert, because no general factor exists.
UNCONVERTIBLE_NOTE = (
    "'{unit}' is not a unit with a general conversion to NTU AUs. Host universities "
    "define their own credits, so this can only be answered from that university's "
    "own published statement."
)


@dataclass
class ConversionResult:
    """The outcome of an explicitly requested unit conversion."""

    supported: bool
    value: float | None = None
    from_unit: str = ""
    to_unit: str = ""
    input_value: float | None = None
    summary: str = ""
    caveats: list[str] = field(default_factory=list)
    basis: str = ""


def normalise_unit(raw: str) -> str:
    """Map what a student writes onto a canonical unit label."""
    text = (raw or "").strip().upper().rstrip(".")
    aliases = {
        "ECT": "ECTS",
        "ECTS": "ECTS",
        "AU": "AU",
        "AUS": "AU",
        "ACADEMIC UNIT": "AU",
        "ACADEMIC UNITS": "AU",
        "CREDIT": "CREDITS",
        "CREDITS": "CREDITS",
        "CREDIT HOURS": "CREDITS",
        "MC": "MC",
        "MCS": "MC",
        "MODULE": "MODULES",
        "MODULES": "MODULES",
    }
    return aliases.get(text, text)


def convert(value: float, from_unit: str, to_unit: str) -> ConversionResult:
    """Convert between academic units, or explain why that cannot be done.

    Returns ``supported=False`` rather than a number whenever no defensible
    factor exists. "Conversion not available" is a correct answer here, and the
    one the system is designed to be comfortable giving.
    """
    source = normalise_unit(from_unit)
    target = normalise_unit(to_unit)

    if source == target and source:
        return ConversionResult(
            supported=True,
            value=value,
            from_unit=source,
            to_unit=target,
            input_value=value,
            summary=f"{value:g} {source} is {value:g} {target}.",
            basis="identity",
        )

    factor = RULE_OF_THUMB_FACTORS.get((source, target))
    if factor is None:
        unit = source if source not in {"AU"} else target
        return ConversionResult(
            supported=False,
            from_unit=source,
            to_unit=target,
            input_value=value,
            summary=UNCONVERTIBLE_NOTE.format(unit=unit or from_unit),
            caveats=[
                "No conversion was applied. Ask the host university, or check its "
                "GEM Explorer brochure, for its own statement of workload."
            ],
        )

    converted = value * factor
    return ConversionResult(
        supported=True,
        value=converted,
        from_unit=source,
        to_unit=target,
        input_value=value,
        summary=f"{value:g} {source} is approximately {converted:g} {target}.",
        caveats=[RULE_OF_THUMB_CAVEAT],
        basis="rule_of_thumb",
    )
