"""The router. Decides the plan *before* any lane does work.

Order matters here in a way it did not in the previous build, which inferred
intent afterwards from whether university cards had come back — so a question
about safety that happened to mention a university was reported as course
planning. Here the plan is a commitment: :class:`PlannedTask` objects are
emitted first, and the lanes then report against them.

The deterministic classifier settles most messages. The LLM is consulted only
when the rules report ``ambiguous``, and its answer is discarded unless it
names lanes that actually exist.
"""

from __future__ import annotations

import json
import re
from typing import Any

from graph import router_rules as rules
from graph.domain import PlannedTask, Profile
from graph.providers import (
    LLMProvider,
    LLMRequest,
    provider_from_env,
    usage_counters,
)

VALID_LANES = frozenset(
    rules.DETERMINISTIC_LANES
    | {rules.RESEARCH, rules.OFFICIAL_DOCS, rules.GENERAL_QUESTIONS}
)

SYSTEM_PROMPT = """You route a student's exchange-planning question to the lanes that can answer it.

The message may contain constraints rather than a direct question: requested
NTU module codes, mapping categories to include or omit, a region, or a maximum
monthly budget. Those are still course-planning work and should be routed to
"course_matching" (and to "finance" when the budget needs checking).

This decomposition step is the orchestrator: it runs before the specialist
agents and chooses the smallest set of lanes that can answer the message.
Route a numeric GPA stated by the student (for example, "my GPA is X") to
"course_matching" as an eligibility filter. Route a question about a host
university's published minimum GPA or course load to "workload". Route NTU-
student process guidance such as financial aid, applications, nominations,
tuition policy, credit transfer, pre-departure preparation, paperwork or what
to prepare before exchange to "general_questions" when it is covered by the
NTU reference corpus.
For a broad briefing about one named host university, include the "finance"
lane so the briefing contains the Wise cost-of-living panel. Do not add the
"finance" lane to a generic shortlist or course-planning request unless the
student asks about cost, budget, rent or affordability.
When the student asks which available partner universities satisfy a
qualitative criterion (for example food, weather, snow, research strength,
community or safety), route both "course_matching" and "research". The
orchestrator will run Coursefinder first and pass only those eligible partner
universities into the web-research step. Never use a generic web result to
invent a new exchange option outside that candidate set.
When the student asks a direct weather question about one named university,
city or region (for example "what is the weather like at UC3M for Semester 1?"),
route only "research". Do not add workload, finance, course matching or a
general university briefing unless the student explicitly asks for those too.

Lanes:
  "course_matching" - which partner universities have approved NTU module mappings
  "workload"        - a host university's own course-load rules
  "finance"         - costs, budget, cost of living
  "research"        - qualitative questions: safety, food, culture, weather, comparisons,
                      or host-university calendar facts such as orientation, arrival,
                      first/last class, exam period and semester dates. For calendar
                      facts, the research lane checks GEM Explorer programme dates first.
  "conversion"      - an explicit unit or currency conversion the student asked for
  "official_docs"   - NTU's own policy: visas, insurance, deadlines, applications
  "general_questions" - NTU-student-specific GEM Explorer and SUSEP guidance from the project-local ChromaDB corpus

Return ONLY a JSON object: {"lanes": ["..."], "reason": "one short sentence"}
Choose the fewest lanes that fully answer the question. Never invent a lane name.
"""


def _parse_lanes(raw: str) -> tuple[list[str], str]:
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return [], ""
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except ValueError:
        return [], ""
    if not isinstance(parsed, dict):
        return [], ""
    lanes = [
        lane for lane in parsed.get("lanes", []) if isinstance(lane, str) and lane in VALID_LANES
    ]
    reason = parsed.get("reason") if isinstance(parsed.get("reason"), str) else ""
    return lanes, (reason or "")[:200]


def _clarification_for(profile: Profile) -> str | None:
    """The one question worth asking. Only programme and term ever block.

    Destination, budget and CGPA are all optional: asking for them would turn a
    planning assistant into a form.
    """
    missing: list[str] = []
    if not profile.school_code:
        missing.append("your degree programme (for example Computer Science, or CSC)")
    if not profile.preferred_semester:
        missing.append("which exchange term you want (Semester 1 or Semester 2)")
    if not missing:
        return None
    if len(missing) == 1:
        return f"Before I can shortlist anything, I need {missing[0]}."
    return f"Before I can shortlist anything, I need two things: {missing[0]}, and {missing[1]}."


def plan_tasks(lanes: list[str], profile: Profile) -> list[PlannedTask]:
    """Turn a lane list into the plan the UI will show and stream."""
    kinds = {
        rules.COURSE_MATCHING: "course_matching",
        rules.WORKLOAD: "workload",
        rules.FINANCE: "finance",
        rules.RESEARCH: "research",
        rules.CONVERSION: "conversion",
        rules.OFFICIAL_DOCS: "official_docs",
        rules.GENERAL_QUESTIONS: "general_questions",
        rules.CLARIFICATION: "clarification",
    }
    tasks = [
        PlannedTask(
            kind="profile",
            status="complete",
            description="Understand the student's programme, term and preferences",
        )
    ]
    for lane in lanes:
        tasks.append(
            PlannedTask(
                kind=kinds.get(lane, "course_matching"),
                status="pending",
                description=rules.describe(lane, profile),
            )
        )
    return tasks


def _apply_lane_guardrails(
    decision: rules.RouteDecision,
    message: str,
    profile: Profile,
    named_university: str | None = None,
) -> rules.RouteDecision:
    """Apply invariants after both deterministic and model-based routing.

    The deterministic rules normally settle this, but an ambiguous request can
    be handed to the router model. These constraints are therefore applied a
    second time at the orchestration boundary so a model cannot reintroduce a
    Singapore budget or an otherwise incompatible lane.
    """
    lanes = list(decision.lanes)
    from agents.intake_fallback import country_for_university

    named_country = country_for_university(named_university)
    is_singapore_scope = profile.programme_type == "SUSEP" or (
        named_country or ""
    ).strip().upper() == "SINGAPORE"
    if is_singapore_scope and not rules.FINANCE_PATTERNS.search(
        rules.routing_message(message)[0]
    ):
        lanes = [lane for lane in lanes if lane != rules.FINANCE]
    if lanes == decision.lanes:
        return decision
    return rules.RouteDecision(
        intent=rules._intent_for(lanes),
        lanes=lanes,
        ambiguous=False,
        matched=decision.matched,
        reason=f"{decision.reason}:policy_guardrail",
    )


def _ask(
    clarification: str,
    profile: Profile,
    warnings: list[str],
    counters: dict[str, int],
) -> dict[str, Any]:
    """The turn ends in a question. One shape, used everywhere it happens."""
    return {
        "intent": rules.UNKNOWN,
        "lanes": [],
        "planned_tasks": [
            PlannedTask(
                kind="clarification",
                status="complete",
                description=rules.describe(rules.CLARIFICATION, profile),
            )
        ],
        "clarification": clarification,
        "warnings": warnings,
        "counters": counters,
    }


def run(
    message: str,
    profile: Profile,
    named_university: str | None = None,
    has_prior_results: bool = False,
    profile_from_message: bool = False,
    provider: LLMProvider | None = None,
) -> dict[str, Any]:
    """Route one turn. Returns the state patch the graph node applies."""
    warnings: list[str] = []
    counters: dict[str, int] = {}
    _, corrected = rules.routing_message(message)
    if corrected:
        warnings.append("orchestrator_replanned_after_user_correction")
    decision = rules.classify(
        message,
        profile=profile,
        named_university=named_university,
        has_prior_results=has_prior_results,
        profile_from_message=profile_from_message,
    )
    clarification = _clarification_for(profile)

    # A message the rules could not place, from a student who has not yet said
    # what they study, has an obvious right answer: ask. Consulting the model
    # here spends a call to be told something worse - it routed a bare
    # "Semester 1 (Fall)" to the disabled official-documents lane, which
    # answers a student's half-finished sentence with a refusal.
    if decision.ambiguous and not decision.lanes and clarification:
        return _ask(clarification, profile, warnings, counters)

    # The LLM is a tie-breaker, never the first resort.
    if decision.ambiguous:
        provider = provider or provider_from_env()
        if provider.available():
            try:
                response = provider.invoke(
                    LLMRequest(
                        messages=(
                            ("system", SYSTEM_PROMPT),
                            ("human", (message or "")[:800]),
                        ),
                        temperature=0.0,
                        json_mode=True,
                    )
                )
                counters = usage_counters(response)
                lanes, reason = _parse_lanes(response.content)
                if lanes:
                    decision = rules.RouteDecision(
                        intent=rules._intent_for(lanes),
                        lanes=lanes,
                        ambiguous=False,
                        matched=decision.matched,
                        reason=f"llm: {reason}" if reason else "llm",
                    )
                else:
                    warnings.append("router_llm_unparseable: kept the deterministic route")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"router_llm_failed: {exc.__class__.__name__}")
                counters = {"llm_calls": 1}

    decision = _apply_lane_guardrails(decision, message, profile, named_university)

    # Some questions have a known, bounded answer and need no lane at all.
    if decision.reason == "gem_or_susep":
        return {
            "intent": rules.CORE_PLANNING,
            "lanes": [],
            "planned_tasks": plan_tasks([], profile),
            "direct_answer": rules.GEM_OR_SUSEP_ANSWER,
            "clarification": None,
            "warnings": warnings,
            "counters": counters,
        }

    # Only a lane that needs a shortlist is blocked by a missing programme or
    # term. Everything else — a named university, a conversion, a visa question
    # — can answer without them, so the gate drops *those lanes* rather than the
    # whole turn. "How much does Ajou University cost per month?" needs neither
    # a degree nor a semester, and demanding them before answering would be a
    # form pretending to be an assistant.
    needs_profile = {rules.COURSE_MATCHING}
    lanes = list(decision.lanes)
    if clarification:
        runnable = [lane for lane in lanes if lane not in needs_profile]
        if runnable:
            return {
                "intent": rules._intent_for(runnable),
                "lanes": runnable,
                "planned_tasks": plan_tasks(runnable, profile),
                "clarification": None,
                "warnings": warnings,
                "counters": counters,
            }
        return _ask(clarification, profile, warnings, counters)

    if decision.is_greeting:
        return {
            "intent": rules.UNKNOWN,
            "lanes": [],
            "planned_tasks": plan_tasks([], profile),
            "clarification": clarification
            or "What would you like to plan? Tell me your programme and which term.",
            "warnings": warnings,
            "counters": counters,
        }

    return {
        "intent": decision.intent,
        "lanes": lanes,
        "planned_tasks": plan_tasks(lanes, profile),
        "clarification": None,
        "warnings": warnings,
        "counters": counters,
    }
