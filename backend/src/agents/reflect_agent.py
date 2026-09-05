"""Bounded self-critique.

Two things make this a real critique rather than a decorative one.

**The checks are deterministic.** They test properties the system claims to
guarantee — every source has a URL, every research finding carries a citation,
no lane cited a source type it is not permitted to emit, an answer that
promises a shortlist actually has one. A model asked "is this good?" reliably
says yes; these questions have answers.

**The loop is bound by a counter held in state**, per the briefing's
requirement that loops be bounded by something the model cannot argue with.
:func:`should_retry` reads ``reflect_iterations`` against
``max_reflect_iterations``. The previous build's ``route_after_reflect``
returned a constant, so its cap never fired at all. Reaching the cap emits a
caveat; it never silently truncates.

**A retry must be able to change the answer.** Re-running a lane that has
already run cannot fix anything: the inputs are identical, so the output is
identical, and the only effect is a second copy of that lane's evidence on an
additive bus. So the loop is allowed to run exactly one thing — the lanes a
corrected route says should have run and did not. Everything else the critique
finds is reported as a warning and, at the cap, as a caveat. A problem being
visible is the point; spending another superstep to reproduce it is not.
"""

from __future__ import annotations

from graph import policy
from graph import router_rules as rules
from graph.domain import Profile
from graph.state import LaneResult

LANE = "reflect"

CAP_CAVEAT = (
    "I reached my internal review limit on this answer. Everything shown is still "
    "sourced, but I stopped short of a further pass."
)


def executed_lanes(state: dict) -> set[str]:
    """Lanes that have already produced a result this turn.

    ``lane_results`` is keyed by lane name and merged across parallel lanes, so
    it is the record of what actually ran — including on a previous pass
    through the loop.
    """
    return {lane for lane in (state.get("lane_results") or {}) if lane in rules.ALL_LANES}


def _corrected_route(state: dict) -> list[str]:
    """Return a corrected lane plan only when it differs from execution."""
    message = str(state.get("message") or "")
    _, corrected = rules.routing_message(message)
    if not corrected:
        return []
    expected = rules.classify(
        message,
        profile=state.get("profile"),
        named_university=state.get("named_university"),
        has_prior_results=bool(state.get("history")),
        profile_from_message=bool(state.get("profile_from_message")),
    )
    planned = set(state.get("lanes") or [])
    return expected.lanes if expected.lanes and not set(expected.lanes).issubset(planned) else []


def pending_lanes(state: dict, candidates: list[str] | None = None) -> list[str]:
    """The corrected-route lanes that have not run yet, in order.

    This is the whole set of work a retry is allowed to do. A lane already in
    ``lane_results`` is excluded: running it again would append a duplicate of
    its evidence and could not change what it found.

    ``candidates`` lets :func:`run` ask about a route it has just computed but
    not yet written to state; the graph edge reads the stored value instead.
    """
    already = executed_lanes(state)
    seen: set[str] = set()
    pending: list[str] = []
    for lane in (candidates if candidates is not None else state.get("replan_lanes") or []):
        if lane in already or lane in seen or lane not in rules.ALL_LANES:
            continue
        seen.add(lane)
        pending.append(lane)
    return pending


def critique(state: dict) -> list[str]:
    """Deterministic checks over the evidence bus. Returns the problems found."""
    problems: list[str] = []

    sources = state.get("sources") or []
    for source in sources:
        if not (getattr(source, "url", "") or "").strip():
            problems.append(f"source_without_url: {getattr(source, 'title', 'untitled')}")
        if not (getattr(source, "excerpt", "") or "").strip():
            problems.append(f"source_without_excerpt: {getattr(source, 'title', 'untitled')}")

    for finding in state.get("research_findings") or []:
        source = getattr(finding, "source", None)
        if source is None or not (getattr(source, "url", "") or "").strip():
            problems.append(f"uncited_finding: {getattr(finding, 'claim', '')[:60]}")

    # Source policy, checked again after the fact. render enforces it; this
    # reports it, so a violation shows up in the metrics rather than only being
    # quietly dropped.
    lanes = state.get("lanes") or []
    permitted = set()
    for lane in lanes:
        permitted |= policy.allowed_types(lane)
    for source in sources:
        source_type = getattr(source, "type", "unknown")
        if lanes and source_type not in permitted:
            problems.append(
                f"policy_violation: '{source_type}' source emitted by lanes {sorted(lanes)}"
            )

    # A correction is a strong signal that the prior interpretation was not
    # the active request. Re-run the deterministic orchestrator against the
    # corrected clause and flag a mismatch for observability. This keeps the
    # reflection pass focused on intent as well as evidence quality.
    corrected_route = _corrected_route(state)
    if corrected_route:
        problems.append(
            "orchestrator_missed_corrected_intent: "
            f"expected {corrected_route}, planned {sorted(set(lanes))}"
        )

    # A shortlist that was planned but never produced, with nothing explaining
    # why, is a silent failure.
    if (
        "course_matching" in lanes
        and not (state.get("universities") or [])
        and not (state.get("caveats") or [])
    ):
        problems.append("empty_shortlist_without_explanation")

    return problems


def should_retry(state: dict, problems: list[str]) -> bool:
    """Whether another pass is permitted.

    Two gates, both outside the model's control: there must be work a retry can
    actually do (a lane the corrected route wants that has not run), and the
    counter in state must still allow a pass.
    """
    if not problems:
        return False
    if not pending_lanes(state):
        return False
    iterations = int(state.get("reflect_iterations") or 0)
    cap = int(state.get("max_reflect_iterations") or 0)
    return iterations < cap


def run(state: dict, profile: Profile | None = None) -> dict:
    """Critique the turn once, and record whether the cap has been reached."""
    problems = critique(state)
    iterations = int(state.get("reflect_iterations") or 0) + 1
    cap = int(state.get("max_reflect_iterations") or 0)

    replan_lanes = _corrected_route(state)
    pending = pending_lanes(state, replan_lanes)

    caveats: list[str] = []
    warnings: list[str] = list(problems)
    # The cap caveat is only true when a retry was available and the counter is
    # what stopped it. Saying "I reached my internal review limit" on a turn
    # that never had a second pass to make would be a false explanation.
    if problems and pending and iterations >= cap:
        caveats.append(CAP_CAVEAT)

    patch: dict = {
        "reflect_iterations": iterations,
        "reflect_notes": problems,
        "replan_lanes": replan_lanes,
        "warnings": warnings,
        "caveats": caveats,
        "counters": {"reflect_iterations": 1, "reflect_problems": len(problems)},
        "lane_results": {
            LANE: LaneResult(
                lane=LANE,
                status="complete",
                note=f"{len(problems)} problem(s) on pass {iterations} of {cap}",
            )
        },
    }
    if pending:
        from agents.decompose_agent import plan_tasks

        # Extend the plan; never replace it. ``lanes`` is what render screens
        # sources against, so overwriting it with only the corrected lanes
        # would drop every citation the lanes that already ran had earned —
        # the shortlist would keep its cards and lose its Coursefinder
        # sources. Same for the task list the UI has already streamed.
        lanes = list(state.get("lanes") or [])
        patch["lanes"] = lanes + [lane for lane in pending if lane not in lanes]
        # plan_tasks() prepends the "profile" task, which the committed plan
        # already carries; take only the lane tasks it added.
        patch["planned_tasks"] = list(state.get("planned_tasks") or []) + plan_tasks(
            pending, state.get("profile") or Profile()
        )[1:]

    return patch
