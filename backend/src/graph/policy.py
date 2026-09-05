"""The source policy: which lane may cite which kind of source.

The hackathon briefing is explicit that ``allowed_tools`` is *"an allow-list and
a security boundary"*. This module is that boundary, expressed in code rather
than in a prompt, because a prompt is a request and code is a rule.

What it buys, concretely: a student asking "which modules map at Aalborg?" gets
an answer built only from the Coursefinder database and the official GEM
brochure. No forum post, no blog, no cost-of-living scrape can influence which
modules are shown to have been approved.

Keep this table honest in both directions. A lane granted a source type it
never emits is a permission nobody is enforcing, and it reads as though the
lane still consults that source when it no longer does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from graph.domain import SourceEvidence

# Lane -> the source types that lane is permitted to emit.
LANE_SOURCES: dict[str, frozenset[str]] = {
    # Deterministic planning. Never reads the open web.
    "course_matching": frozenset({"coursefinder", "gem_explorer"}),
    "workload": frozenset({"gem_explorer", "coursefinder"}),
    # Wise's public cost-of-living pages plus local arithmetic. The city/state
    # comes from Coursefinder's enriched universities table; local is only the
    # fixed GBP->SGD conversion and percentage multiplication.
    "finance": frozenset({"wise", "local", "coursefinder"}),
    # Qualitative only. May never touch Coursefinder, so it cannot contradict
    # or "improve on" an eligibility or mapping fact. GEM is included for
    # official programme-calendar facts from the host brochure.
    "research": frozenset({"gem_explorer", "official", "web", "reddit"}),
    # NTU-student-only GEM Explorer and SUSEP guidance. This is a local,
    # versioned intranet dataset, not an open-web source.
    "general_questions": frozenset({"ntu_intranet"}),
    # Arithmetic the student explicitly asked for.
    "conversion": frozenset({"local"}),
    # Disabled. Emits nothing at all.
    "official_docs": frozenset(),
    "clarification": frozenset(),
    "profile": frozenset(),
}

# Source types that must always carry a caveat, whatever lane produced them.
ALWAYS_CAVEATED: frozenset[str] = frozenset({"reddit"})

CAVEAT_TEXT: dict[str, str] = {
    "reddit": "Forum posts are individual experiences, not official policy. "
    "Verify anything decision-critical with the university.",
    "wise": "Cost-of-living figures are Wise estimates, converted from GBP to SGD using a fixed planning rate. "
    "They are a guide and may not match your personal spending.",
}


@dataclass(frozen=True)
class PolicyDecision:
    """The result of screening one lane's sources."""

    allowed: list[SourceEvidence]
    dropped: list[SourceEvidence]
    warnings: list[str]
    caveats: list[str]


def allowed_types(lane: str) -> frozenset[str]:
    """Source types this lane may emit. An unknown lane may emit nothing."""
    return LANE_SOURCES.get(lane, frozenset())


def permits(lane: str, source_type: str) -> bool:
    return source_type in allowed_types(lane)


def screen(lane: str, sources: Iterable[SourceEvidence]) -> PolicyDecision:
    """Drop any source the lane is not permitted to emit, and say so.

    A violation is never silently discarded: it produces a warning that is
    counted, because a lane reaching for a source it should not have is a
    defect in the lane, not noise to be hidden.
    """
    allowed: list[SourceEvidence] = []
    dropped: list[SourceEvidence] = []
    warnings: list[str] = []
    caveats: list[str] = []

    for source in sources:
        if not permits(lane, source.type):
            dropped.append(source)
            warnings.append(
                f"policy_violation: lane '{lane}' may not cite a '{source.type}' source "
                f"({source.title or source.url or 'untitled'})"
            )
            continue
        if not (source.url or "").strip():
            dropped.append(source)
            warnings.append(
                f"unusable_source: '{source.title or 'untitled'}' has no URL, so the "
                "student cannot verify it"
            )
            continue
        allowed.append(source)
        caveat = CAVEAT_TEXT.get(source.type)
        if caveat and caveat not in caveats:
            caveats.append(caveat)

    return PolicyDecision(allowed=allowed, dropped=dropped, warnings=warnings, caveats=caveats)


def screen_all(
    by_lane: dict[str, list[SourceEvidence]]
) -> tuple[list[SourceEvidence], list[str], list[str]]:
    """Screen every lane's sources at once, de-duplicating by URL.

    Returns ``(sources, warnings, caveats)``.
    """
    sources: list[SourceEvidence] = []
    warnings: list[str] = []
    caveats: list[str] = []
    seen: set[str] = set()

    for lane, lane_sources in by_lane.items():
        decision = screen(lane, lane_sources)
        warnings.extend(decision.warnings)
        for caveat in decision.caveats:
            if caveat not in caveats:
                caveats.append(caveat)
        for source in decision.allowed:
            if source.url in seen:
                continue
            seen.add(source.url)
            sources.append(source)

    return sources, warnings, caveats
