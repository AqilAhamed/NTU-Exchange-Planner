"""The graph's shared state, and the reducers that let lanes run in parallel.

The router fans out to several lanes at once. Without reducers, two lanes
returning ``{"sources": [...]}`` in the same superstep would clobber each
other and one lane's citations would vanish. Every field a lane writes to is
therefore additive: lanes **append**, ``render`` **reads**, nothing overwrites.

One field deliberately has no reducer. ``planned_tasks`` is written once, by
the router, before any work happens; lanes report progress through
``lane_results`` instead. That keeps a single writer for the plan and makes
duplicated tasks structurally impossible.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from graph.domain import (
    AnswerEnvelope,
    AnswerError,
    BudgetEstimate,
    Conversion,
    EligibilityEvidence,
    FinanceEvidence,
    PlannedTask,
    Profile,
    ResearchFinding,
    SourceEvidence,
    UniversityCard,
    WorkloadEvidence,
)

# The reflect loop's hard ceiling. Held in state and enforced by the graph, not
# by the model's judgement: the briefing requires every loop to be bound by a
# counter the model cannot argue with.
MAX_REFLECT_ITERATIONS = 2


@dataclass
class LaneResult:
    """How one lane finished. ``render`` turns these into task statuses."""

    lane: str
    status: str = "complete"  # pending | running | complete | skipped | failed
    warnings: list[str] = field(default_factory=list)
    note: str = ""
    duration_ms: int = 0
    tool_calls: int = 0
    tool_failures: int = 0


def merge_lane_results(
    left: dict[str, LaneResult] | None, right: dict[str, LaneResult] | None
) -> dict[str, LaneResult]:
    """Merge two lanes' result maps. Each lane owns its own key, so a plain
    dict update is safe and no lane can overwrite another's status."""
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def merge_counters(left: dict[str, int] | None, right: dict[str, int] | None) -> dict[str, int]:
    """Sum per-key counters written from parallel lanes."""
    merged = dict(left or {})
    for key, value in (right or {}).items():
        merged[key] = merged.get(key, 0) + value
    return merged


def merge_universities(
    left: list[UniversityCard] | None, right: list[UniversityCard] | None
) -> list[UniversityCard]:
    """Append cards, but never let the same university appear twice.

    A shortlist cannot legitimately contain one university twice, so this is a
    real invariant rather than defensive tidying. It is enforced here because
    the evidence bus is additive: anything that runs ``course_matching`` a
    second time in one turn would otherwise append a duplicate of every card,
    and the rendered prose would count them ("showing the 12") as if they were
    distinct places. The graph is built so a lane runs at most once per turn;
    this reducer is what makes that a guarantee instead of a convention.
    """
    merged = list(left or [])
    seen = {(card.university_id, card.programme_type) for card in merged}
    for card in right or []:
        key = (card.university_id, card.programme_type)
        if key in seen:
            continue
        seen.add(key)
        merged.append(card)
    return merged


class ExchangeState(TypedDict, total=False):
    """Everything one turn of the conversation carries.

    ``total=False`` because the graph builds this up node by node; a node reads
    what it needs with ``state.get(...)`` and returns only what it changed.
    """

    # --- input ---
    session_id: str
    message: str
    history: list[dict[str, Any]]

    # --- what intake worked out ---
    profile: Profile
    named_university: str | None
    university_candidates: list[str]
    university_selection: bool
    cgpa: float | None
    budget_sgd: float | None
    profile_complete: bool
    profile_from_message: bool
    clarification: str | None
    direct_answer: str | None
    general_summary: str | None

    # --- what the router decided, before any work is done ---
    intent: str
    lanes: list[str]
    planned_tasks: list[PlannedTask]

    # --- the evidence bus: lanes append, render reads ---
    sources: Annotated[list[SourceEvidence], operator.add]
    universities: Annotated[list[UniversityCard], merge_universities]
    workload_evidence: Annotated[list[WorkloadEvidence], operator.add]
    finance_evidence: Annotated[list[FinanceEvidence], operator.add]
    eligibility_evidence: Annotated[list[EligibilityEvidence], operator.add]
    research_findings: Annotated[list[ResearchFinding], operator.add]
    calendar_sections: Annotated[list[dict[str, Any]], operator.add]
    calendar_notes: Annotated[list[str], operator.add]
    conversion_results: Annotated[list[Conversion], operator.add]
    caveats: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]
    errors: Annotated[list[AnswerError], operator.add]
    lane_results: Annotated[dict[str, LaneResult], merge_lane_results]
    counters: Annotated[dict[str, int], merge_counters]

    # --- pagination, carried through to the legacy payload ---
    budget: BudgetEstimate | None
    budgets: list[BudgetEstimate]
    total_universities: int
    has_more: bool

    # --- the bounded reflect loop ---
    reflect_iterations: int
    max_reflect_iterations: int
    reflect_notes: list[str]
    replan_lanes: list[str]

    # --- output ---
    envelope: AnswerEnvelope


def initial_state(
    message: str,
    session_id: str,
    history: list[dict[str, Any]] | None = None,
    profile: Profile | None = None,
    context: dict[str, Any] | None = None,
) -> ExchangeState:
    """A fresh state for one turn, seeded with what the session already knows.

    ``context`` carries the things a follow-up depends on but that are not part
    of the profile: the university under discussion, the active exchange term,
    the student's CGPA and their budget. Keeping the term here as well as in
    the profile makes conversational research deterministic for older or
    partially persisted sessions: "what is the weather there?" can use both
    the place and the period from the preceding turn.
    """
    carried = context or {}
    seeded_profile = profile or Profile()
    carried_term = str(carried.get("preferred_semester") or "").strip()
    if not seeded_profile.preferred_semester and carried_term:
        seeded_profile = seeded_profile.model_copy(
            update={"preferred_semester": carried_term}
        )
    return ExchangeState(
        session_id=session_id,
        message=message or "",
        history=list(history or []),
        profile=seeded_profile,
        named_university=carried.get("named_university"),
        university_candidates=list(carried.get("university_candidates") or []),
        university_selection=False,
        cgpa=carried.get("cgpa"),
        budget_sgd=carried.get("budget_sgd"),
        profile_complete=False,
        profile_from_message=False,
        clarification=None,
        direct_answer=None,
        general_summary=None,
        intent="unknown",
        lanes=[],
        planned_tasks=[],
        sources=[],
        universities=[],
        workload_evidence=[],
        finance_evidence=[],
        eligibility_evidence=[],
        research_findings=[],
        calendar_sections=[],
        calendar_notes=[],
        conversion_results=[],
        caveats=[],
        warnings=[],
        errors=[],
        lane_results={},
        counters={},
        budget=None,
        budgets=[],
        total_universities=0,
        has_more=False,
        reflect_iterations=0,
        max_reflect_iterations=MAX_REFLECT_ITERATIONS,
        reflect_notes=[],
        replan_lanes=[],
    )
