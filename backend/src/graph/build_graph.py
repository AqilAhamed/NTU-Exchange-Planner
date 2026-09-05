"""LangGraph assembly: a router that fans out, lanes that converge.

    START -> intake -> decompose --+--> course_matching --+
                                   +--> workload         |
                                   +--> finance          +--> reflect -> render -> END
                                   +--> research         |
                                   +--> conversion       |
                                   +--> official_docs   -+
                                   +--> (none) ----------+

``decompose`` is a conditional edge that returns a **list** of node names, which
is how LangGraph fans out: every named lane runs in the same superstep, and
they converge on ``reflect``. The lanes never call each other and never share a
mutable object — each returns a patch, and the additive reducers in
:mod:`graph.state` merge them. That pattern was verified against the installed
LangGraph before any of this was written.

``reflect`` runs once by default and may loop back at most
``max_reflect_iterations`` times, a counter held in state rather than decided by
the model.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from langgraph.graph import END, START, StateGraph

from agents import (
    au_agent,
    cost_of_living_agent,
    currency_agent,
    decompose_agent,
    general_questions_agent,
    intake_agent,
    intake_fallback,
    mapping_agent,
    official_docs_agent,
    reflect_agent,
    render_agent,
    research_agent,
    workload_agent,
)
from graph import router_rules as rules
from graph.domain import Profile
from graph.state import ExchangeState, LaneResult, initial_state

# Lane name -> graph node name. Kept explicit so a typo is a KeyError at build
# time rather than a lane that silently never runs.
LANE_NODES: dict[str, str] = {
    rules.COURSE_MATCHING: "course_matching",
    rules.WORKLOAD: "workload",
    rules.FINANCE: "finance",
    rules.RESEARCH: "research",
    rules.CONVERSION: "conversion",
    rules.OFFICIAL_DOCS: "official_docs",
    rules.GENERAL_QUESTIONS: "general_questions",
}


# --- nodes ----------------------------------------------------------------


def node_intake(state: ExchangeState) -> dict[str, Any]:
    """Deterministic extraction, with the LLM layered over it when available."""
    extraction, warnings, counters = intake_agent.run(
        message=state.get("message", ""),
        history=state.get("history"),
        profile=state.get("profile"),
        named_university=state.get("named_university"),
    )
    profile = extraction.as_profile(state.get("profile"))
    # Older persisted chats may carry CGPA in turn context before it became a
    # first-class profile field. Promote it so later pages keep the filter.
    if profile.cgpa is None and state.get("cgpa") is not None:
        profile = profile.model_copy(update={"cgpa": state.get("cgpa")})
    # The LLM is allowed to resolve a referent when no structured context
    # exists, but it must not replace an already-known university with a guess
    # on a follow-up such as "what is the weather like there?". Re-run the
    # deterministic pass to distinguish a university explicitly named in this
    # message from one the model inferred from conversation.
    explicit_extraction = intake_fallback.extract(state.get("message", ""))
    carried_university = state.get("named_university")
    carried_options = list(state.get("university_candidates") or [])
    selected_option = None
    if not explicit_extraction.named_university and not explicit_extraction.university_candidates:
        selected_option = intake_fallback.select_university_option(
            state.get("message", ""), carried_options, carried_university
        )
    if explicit_extraction.named_university:
        active_university = explicit_extraction.named_university
        option_list = []
    elif explicit_extraction.university_candidates:
        active_university = None
        option_list = list(explicit_extraction.university_candidates)
    elif selected_option:
        active_university = selected_option
        option_list = carried_options
    elif intake_fallback.replaces_university_scope(
        state.get("message", ""), explicit_extraction
    ):
        # A newly stated country/region or explicit broadening wording is a
        # scope change. The old university remains useful for referents such
        # as "there", but must not override the new shortlist request.
        active_university = None
        option_list = []
    elif carried_university:
        # Structured context wins over an LLM's uncertain interpretation of a
        # pronoun. This is what keeps "there" tied to the university the
        # student just asked about.
        active_university = carried_university
        option_list = carried_options
    else:
        active_university = extraction.named_university
        option_list = carried_options

    # A named university is carried separately from the destination profile.
    # Enforce the same programme/location invariant for that state too: local
    # exchange can only target Singapore partners, while GEMX targets overseas
    # partners. If the student changes exchange type, release an incompatible
    # old university before mapping runs.
    if not intake_fallback.university_matches_programme(
        active_university, profile.programme_type
    ):
        active_university = None
    return {
        "profile": profile,
        "named_university": active_university,
        "university_candidates": option_list,
        "university_selection": bool(selected_option),
        "cgpa": extraction.cgpa if extraction.cgpa is not None else state.get("cgpa"),
        "budget_sgd": (
            extraction.budget_sgd if extraction.budget_sgd is not None else state.get("budget_sgd")
        ),
        "profile_complete": bool(profile.school_code and profile.preferred_semester),
        "profile_from_message": bool(
            {"programme", "term"} & {f.removeprefix("llm:") for f in extraction.found}
        ),
        "warnings": warnings,
        "counters": counters,
    }


def node_decompose(state: ExchangeState) -> dict[str, Any]:
    """Commit to a plan before any lane does work."""
    candidates = list(state.get("university_candidates") or [])
    if len(candidates) > 1 and rules.is_cost_request(state.get("message", "")):
        choices = "\n".join(
            f"{index}. {name}" for index, name in enumerate(candidates, start=1)
        )
        return decompose_agent._ask(
            "I found more than one partner university matching that name. "
            "Please reply with the number of the one you want:\n\n" + choices,
            state.get("profile") or Profile(),
            [],
            {},
        )
    if state.get("university_selection") and state.get("named_university"):
        profile = state.get("profile") or Profile()
        return {
            "intent": rules.CORE_PLANNING,
            "lanes": [rules.FINANCE],
            "planned_tasks": decompose_agent.plan_tasks([rules.FINANCE], profile),
            "clarification": None,
            "warnings": [],
            "counters": {},
        }
    return decompose_agent.run(
        message=state.get("message", ""),
        profile=state.get("profile") or Profile(),
        named_university=state.get("named_university"),
        has_prior_results=bool(state.get("history")),
        profile_from_message=bool(state.get("profile_from_message")),
    )


def node_course_matching(state: ExchangeState) -> dict[str, Any]:
    return mapping_agent.run(
        profile=state.get("profile") or Profile(),
        named_university=state.get("named_university"),
        cgpa=state.get("cgpa"),
    )


def node_workload(state: ExchangeState) -> dict[str, Any]:
    return workload_agent.run(
        profile=state.get("profile") or Profile(),
        named_university=state.get("named_university"),
        universities=state.get("universities"),
        cgpa=state.get("cgpa"),
    )


def node_finance(state: ExchangeState) -> dict[str, Any]:
    # The router owns the decision to schedule finance. In particular, a
    # named-university briefing deliberately includes the Wise panel even if
    # the student says only “tell me more about ...”. Keeping a second,
    # narrower gate here discarded that scheduled work and left the briefing
    # without its cost-of-living evidence.
    return cost_of_living_agent.run(
        profile=state.get("profile") or Profile(),
        named_university=state.get("named_university"),
        universities=state.get("universities"),
        university_candidates=state.get("university_candidates"),
    )


def node_research(state: ExchangeState) -> dict[str, Any]:
    profile = state.get("profile") or Profile()
    kwargs: dict[str, Any] = {
        "message": state.get("message", ""),
        "named_university": state.get("named_university"),
        "term": profile.preferred_semester,
    }
    if rules.needs_candidate_research(
        state.get("message", ""), state.get("lanes") or [], state.get("named_university")
    ):
        # Course matching is deliberately completed before this call for a
        # shortlist-comparison question, so research can inspect only partners
        # NTU actually offers for this student's programme and term.
        kwargs["universities"] = state.get("universities") or []
        kwargs["candidate_scoped"] = True
    return research_agent.run(
        **kwargs,
    )


def node_conversion(state: ExchangeState) -> dict[str, Any]:
    """Unit and currency conversion share a lane; both may apply to one message."""
    message = state.get("message", "")
    patch = au_agent.run(message)
    if currency_agent.parse_request(message) is not None:
        currency_patch = currency_agent.run(message)
        for key in ("conversion_results", "caveats", "sources"):
            if key in currency_patch:
                patch[key] = list(patch.get(key) or []) + list(currency_patch[key])
        # Both halves report through the same lane; keep whichever did work.
        if not patch.get("conversion_results"):
            patch["lane_results"] = currency_patch.get("lane_results", patch.get("lane_results", {}))
    return patch


def node_official_docs(state: ExchangeState) -> dict[str, Any]:
    return official_docs_agent.run(state.get("message", ""))


def node_general_questions(state: ExchangeState) -> dict[str, Any]:
    return general_questions_agent.run(
        message=state.get("message", ""),
        history=state.get("history"),
    )


def node_reflect(state: ExchangeState) -> dict[str, Any]:
    return reflect_agent.run(dict(state), profile=state.get("profile"))


def node_render(state: ExchangeState) -> dict[str, Any]:
    return render_agent.run(dict(state))


# --- edges ----------------------------------------------------------------


def route_lanes(state: ExchangeState) -> list[str]:
    """The fan-out edge. Returning a list is what makes LangGraph run lanes in parallel."""
    lanes = [LANE_NODES[lane] for lane in (state.get("lanes") or []) if lane in LANE_NODES]
    if rules.needs_candidate_research(
        state.get("message", ""), state.get("lanes") or [], state.get("named_university")
    ):
        # Research needs the cards emitted by Coursefinder. Its dependent edge
        # below starts it after mapping has converged.
        return ["course_matching"]
    # No lane is a legitimate outcome — a clarification, or a greeting. The
    # turn still needs an answer, so go straight to reflect.
    return lanes or ["reflect"]


def route_after_course_matching(state: ExchangeState) -> str | list[str]:
    """Release dependent research/finance work after candidate cards exist."""
    if not rules.needs_candidate_research(
        state.get("message", ""), state.get("lanes") or [], state.get("named_university")
    ):
        return "reflect"
    remaining = [
        LANE_NODES[lane]
        for lane in (state.get("lanes") or [])
        if lane != rules.COURSE_MATCHING and lane in LANE_NODES
    ]
    return remaining or "reflect"


def route_after_reflect(state: ExchangeState) -> str | list[str]:
    """Run the corrected lanes that have not run yet, or finish.

    The previous build returned a constant here, so its iteration cap could
    never fire. The cap fires now — and so does a second rule: a lane already
    in ``lane_results`` is never re-entered. Re-running one with identical
    inputs cannot change what it found, and every lane appends to an additive
    bus, so the only thing a re-run produces is a duplicate of its own
    evidence. :func:`reflect_agent.pending_lanes` is the whole of what a retry
    may do.
    """
    problems = list(state.get("reflect_notes") or [])
    snapshot = dict(state)
    if reflect_agent.should_retry(snapshot, problems):
        pending = reflect_agent.pending_lanes(snapshot)
        nodes = [LANE_NODES[lane] for lane in pending if lane in LANE_NODES]
        # A dependent research retry still needs the candidate cards, but
        # course_matching has already produced them and they are on the bus,
        # so the lane reads them from state rather than being run again.
        return nodes or "render"
    return "render"


@lru_cache(maxsize=1)
def build_graph():
    """Compile the graph once and reuse it. Compilation is not free."""
    builder = StateGraph(ExchangeState)

    builder.add_node("intake", node_intake)
    builder.add_node("decompose", node_decompose)
    builder.add_node("course_matching", node_course_matching)
    builder.add_node("workload", node_workload)
    builder.add_node("finance", node_finance)
    builder.add_node("research", node_research)
    builder.add_node("conversion", node_conversion)
    builder.add_node("official_docs", node_official_docs)
    builder.add_node("general_questions", node_general_questions)
    builder.add_node("reflect", node_reflect)
    builder.add_node("render", node_render)

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "decompose")
    builder.add_conditional_edges(
        "decompose",
        route_lanes,
        [
            "course_matching", "workload", "finance", "research", "conversion",
            "official_docs", "general_questions", "reflect",
        ],
    )
    for node in LANE_NODES.values():
        if node != "course_matching":
            builder.add_edge(node, "reflect")
    builder.add_conditional_edges(
        "course_matching", route_after_course_matching, ["research", "finance", "reflect"]
    )
    builder.add_conditional_edges(
        "reflect", route_after_reflect, [*LANE_NODES.values(), "render"]
    )
    builder.add_edge("render", END)

    return builder.compile()


def run_turn(
    message: str,
    session_id: str,
    history: list[dict[str, Any]] | None = None,
    profile: Profile | None = None,
    context: dict[str, Any] | None = None,
) -> ExchangeState:
    """Run one full turn and return the final state, envelope included."""
    graph = build_graph()
    state = initial_state(
        message, session_id, history=history, profile=profile, context=context
    )
    return graph.invoke(state)


async def arun_turn(
    message: str,
    session_id: str,
    history: list[dict[str, Any]] | None = None,
    profile: Profile | None = None,
    context: dict[str, Any] | None = None,
) -> ExchangeState:
    """``run_turn`` for an async caller.

    The nodes are synchronous and LangGraph runs them on a threadpool either
    way; awaiting rather than blocking is what lets the caller put a timeout
    around a turn, which the non-streaming endpoint previously had no way to
    do.
    """
    graph = build_graph()
    state = initial_state(
        message, session_id, history=history, profile=profile, context=context
    )
    return await graph.ainvoke(state)


__all__ = [
    "LANE_NODES",
    "LaneResult",
    "arun_turn",
    "build_graph",
    "route_after_reflect",
    "route_after_course_matching",
    "route_lanes",
    "run_turn",
]
