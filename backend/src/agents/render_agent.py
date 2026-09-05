"""The only node that builds an answer.

Every other node appends evidence; this one reads the bus and turns it into a
:class:`~graph.domain.AnswerEnvelope`. Having exactly one place where evidence
becomes prose is what makes the source policy enforceable at all — there is a
single choke point, and it is here.

Three things happen, in order:

1. **The source policy is applied.** A lane that emitted a source type it is
   not permitted to emit has that source *dropped*, and a warning recorded.
   Dropping silently would hide a defect in the lane.
2. **Lane results become task statuses.** The plan the router committed to is
   updated with what actually happened, so a skipped lane reads "skipped" in
   the UI rather than disappearing.
3. **Prose is composed deterministically.** No LLM call is required to produce
   an answer; the narration is built from the evidence itself, so the product
   works with no API key and says only what it can cite.
"""

from __future__ import annotations

from graph import policy
from graph.domain import (
    AnswerBody,
    AnswerEnvelope,
    Profile,
    SourceEvidence,
)
from graph.state import LaneResult

LANE = "render"


def _screen_sources(
    sources: list[SourceEvidence], lanes: list[str]
) -> tuple[list[SourceEvidence], list[str], list[str]]:
    """Screen every source against what the lanes that actually ran may emit.

    A source type does not belong to one lane. ``gem_explorer`` is legitimately
    produced by both the workload lane and the finance lane, so attributing it
    to a single nominal lane drops a valid citation whenever the *other* lane
    is the one running — which is how Ajou's budget lost its only source.

    The test is therefore: does **any active lane** permit this type? That keeps
    the boundary closed — a forum source in a course-matching turn is still
    dropped, because no active lane permits it — while not punishing a type that
    two lanes share.
    """
    active = [lane for lane in lanes if policy.allowed_types(lane)]
    by_lane: dict[str, list[SourceEvidence]] = {}
    for source in sources:
        owner = next(
            (lane for lane in active if source.type in policy.allowed_types(lane)),
            "__not_permitted__",
        )
        by_lane.setdefault(owner, []).append(source)
    return policy.screen_all(by_lane)


def _unique_sources(sources: list[SourceEvidence]) -> list[SourceEvidence]:
    """Keep one numbered entry per URL, preserving the evidence-bus order."""
    seen: set[str] = set()
    unique: list[SourceEvidence] = []
    for source in sources:
        key = source.url or f"{source.type}:{source.title}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(source)
    return unique


def _apply_statuses(planned_tasks: list, lane_results: dict[str, LaneResult]) -> list:
    """Update the committed plan with what each lane actually did."""
    kind_to_lane = {
        "course_matching": "course_matching",
        "workload": "workload",
        "finance": "finance",
        "research": "research",
        "conversion": "conversion",
        "official_docs": "official_docs",
        "general_questions": "general_questions",
    }
    updated = []
    for task in planned_tasks:
        result = lane_results.get(kind_to_lane.get(task.kind, ""))
        if result is not None:
            task = task.model_copy(
                update={
                    "status": result.status,
                    "warnings": list(task.warnings) + list(result.warnings),
                }
            )
        elif task.status == "pending":
            task = task.model_copy(update={"status": "skipped"})
        updated.append(task)
    return updated


def _shortlist_prose(state: dict, profile: Profile) -> tuple[str, str, list[str]]:
    """Summary, body and recommendations for a shortlist answer."""
    universities = state.get("universities") or []
    total = int(state.get("total_universities") or 0)
    programme = profile.school_name or profile.school_code
    term = profile.preferred_semester
    where = (
        f" in {profile.destination_region}"
        if profile.destination_region
        else (f" in {profile.destination_pref.title()}" if profile.destination_pref else "")
    )

    if not universities:
        named_university = state.get("named_university")
        if named_university:
            message = (
                f"Coursefinder has no previously approved {profile.programme_type} module "
                f"mappings for {programme} at {named_university}."
            )
            return message, "", []
        return (
            f"No partner university{where} has approved mappings for {programme}.",
            "",
            [],
        )

    summary = (
        f"{total} partner universit{'y' if total == 1 else 'ies'}{where} have approved "
        f"{profile.programme_type} module mappings for {programme}"
        + (f", {term}." if term else ".")
    )
    criteria: list[str] = []
    if profile.module_codes:
        criteria.append("including " + ", ".join(profile.module_codes))
    if profile.included_module_types:
        criteria.append("keeping " + ", ".join(profile.included_module_types))
    if profile.restored_module_types:
        alongside = " alongside the requested modules" if profile.module_codes else ""
        criteria.append(
            "including "
            + ", ".join(profile.restored_module_types)
            + " mappings"
            + alongside
        )
    if profile.excluded_module_types:
        criteria.append("excluding " + ", ".join(profile.excluded_module_types))
    if criteria:
        summary += " Filtered to " + "; ".join(criteria) + "."

    lines = [
        (
            f"Showing the {len(universities)} with the most approved mappings. Every count below "
            "is de-duplicated, so it matches what you get when you page through them."
        ),
        "",
    ]
    for card in universities:
        preview = ", ".join(
            f"{m.ntu_module_code} ← {m.host_module_code}" for m in card.mappings_preview[:3]
        )
        lines.append(
            f"- **{card.name}** ({card.country.title()}) — {card.approved_count} approved"
            + (f". For example: {preview}." if preview else ".")
        )

    recommendations = [
        (
            "Open a university card to page through its full mapping history and read the "
            "student submissions attached to each module."
        ),
    ]
    if not profile.destination_pref:
        recommendations.append(
            "Tell me a country if you want the shortlist narrowed — otherwise this is ranked "
            "by how much of your degree is already mapped."
        )
    return summary, "\n".join(lines), recommendations


def _bounds_text(evidence) -> str:
    """One line describing a course load, in the university's own units."""
    unit = evidence.unit_label or "units"
    low, high = evidence.minimum_value, evidence.maximum_value
    if low is not None and high is not None:
        span = f"{low:g} {unit}" if low == high else f"{low:g} to {high:g} {unit}"
    elif high is not None:
        span = f"up to {high:g} {unit}"
    elif low is not None:
        span = f"at least {low:g} {unit}"
    else:
        span = "no numeric bounds published"
    courses = ""
    if evidence.module_count_minimum and evidence.module_count_maximum:
        if evidence.module_count_minimum == evidence.module_count_maximum:
            courses = f", typically {evidence.module_count_minimum} courses"
        else:
            courses = (
                f", roughly {evidence.module_count_minimum}"
                f"-{evidence.module_count_maximum} courses"
            )
    return span + courses


def _compose(
    state: dict, profile: Profile, sources: list[SourceEvidence] | None = None
) -> AnswerBody:
    """Build the prose answer from evidence alone."""
    clarification = state.get("clarification")
    if clarification:
        return AnswerBody(
            summary=clarification,
            body="",
            recommendations=[],
            next_questions=[
                "I'm in Computer Science, Semester 1 (Fall)",
                "EEE, Semester 2, somewhere in Japan",
            ],
        )

    direct = state.get("direct_answer")
    if direct:
        return AnswerBody(
            summary=direct,
            body="",
            recommendations=[],
            next_questions=[
                "I'm in Computer Science, Semester 1 (Fall)",
                "Which SUSEP partners take Business students?",
            ],
        )

    lanes = state.get("lanes") or []
    parts: list[str] = []
    summary = ""
    recommendations: list[str] = []
    source_numbers = {
        source.url: index for index, source in enumerate(sources or [], start=1) if source.url
    }

    def citation(url: str) -> str:
        number = source_numbers.get(url)
        if number is None:
            return "(source)"
        if len(source_numbers) == 1:
            return ""
        return f"([{number}]({url}))"

    if "course_matching" in lanes:
        summary, body, recs = _shortlist_prose(state, profile)
        if body:
            parts.append(body)
        recommendations.extend(recs)

    # Workload is rendered by its own panel, which can show the native units,
    # the conversion status and the brochure link properly. Repeating it here
    # would print every course load twice.
    workload = state.get("workload_evidence") or []
    if workload and not summary:
        names = ", ".join(w.university_name for w in workload)
        summary = f"Course load for {names}, in each university's own units."

    budget = state.get("budget")
    budgets = list(state.get("budgets") or [])
    if budgets and not summary:
        if len(budgets) > 1:
            summary = f"Monthly cost-of-living estimates for {len(budgets)} matching universities, excluding rent where Wise publishes a Property allocation."
            parts.extend(
                f"- **{item.university_name}**: about {item.monthly_total:,.0f} {item.monthly_total_currency} a month"
                + (" excluding rent." if item.rent_included is False else ".")
                if item.monthly_total is not None
                else f"- **{item.university_name}**: no usable Wise monthly figure."
                for item in budgets
            )
        elif budgets[0] is not None:
            budget = budgets[0]
    if budget is not None and not summary:
        if budget.monthly_total is not None:
            where = f" at {budget.university_name}" if budget.university_name else ""
            # The finance lane is Wise-based, so every budget it produces is an
            # estimate. ``fallback_used`` stays on the model for the API
            # contract, but the prose does not branch on a flag that is always
            # true — that reads as though a "published figures" path exists.
            rent_note = " excluding rent" if budget.rent_included is False else ""
            summary = (
                f"About {budget.monthly_total:,.0f} {budget.monthly_total_currency} a month"
                f"{rent_note}{where}, from estimated figures ({budget.source_label})."
            )
        else:
            summary = (
                f"I could not put a monthly figure on {budget.university_name or 'that university'}."
            )

    conversion_lines = [c.summary for c in (state.get("conversion_results") or [])]
    if conversion_lines and not summary:
        # The first conversion becomes the headline; repeating it in the body
        # would print the same sentence twice.
        summary, conversion_lines = conversion_lines[0], conversion_lines[1:]
    parts.extend(conversion_lines)

    findings = state.get("research_findings") or []
    calendar_sections = state.get("calendar_sections") or []
    if calendar_sections:
        parts.append("### Programme dates")
        calendar_urls = set()
        for section in calendar_sections:
            term = str(section.get("term") or "Exchange term")
            source_url = str(section.get("source_url") or "")
            if source_url:
                calendar_urls.add(source_url)
            parts.append(f"#### {term}")
            parts.append("| Information | Date |")
            parts.append("| --- | --- |")
            for item in section.get("items") or []:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "").strip()
                value = str(item.get("value") or "").strip()
                if label and value:
                    parts.append(f"| {label} | {value} |")
        if not summary:
            first_term = str(calendar_sections[0].get("term") or "the exchange term")
            summary = f"Programme dates for {first_term}, taken from GEM Explorer."

        additional_findings = [
            finding
            for finding in findings
            if not (finding.source.type == "gem_explorer" and finding.source.url in calendar_urls)
        ]
        if additional_findings:
            parts.append("### Additional research")
            for finding in additional_findings:
                parts.append(f"- {finding.claim} {citation(finding.source.url)}")
    elif findings:
        parts.append("")
        if "course_matching" in lanes:
            parts.append("### Research across eligible partner universities")
        if "general_questions" in lanes:
            parts.append("### NTU student reference")
        for index, finding in enumerate(findings):
            if index:
                parts.append("")
            if "general_questions" in lanes:
                # Source-derived PDF excerpts can contain table rows. Keep
                # those labels and paragraphs intact instead of putting a
                # whole extracted page inside one giant bullet.
                claim_lines = finding.claim.splitlines()
                contains_table = any(
                    line.strip().startswith("|") and line.strip().endswith("|")
                    for line in claim_lines
                )
                if contains_table:
                    # Appending the citation to the final Markdown row makes
                    # that row cease to be a table row. Put the citation on a
                    # separate line so the UI parser can close the table and
                    # render the citation as ordinary text beneath it.
                    parts.append(finding.claim)
                    source_citation = citation(finding.source.url)
                    if source_citation:
                        parts.append("")
                        parts.append(source_citation)
                else:
                    parts.append(
                        f"{finding.claim} {citation(finding.source.url)}".rstrip()
                    )
            else:
                parts.append(f"- {finding.claim} {citation(finding.source.url)}")
        if not summary:
            summary = (
                state.get("general_summary")
                if "general_questions" in lanes and state.get("general_summary")
                else (
                    "Here is the relevant guidance from NTU's GEM Explorer and SUSEP materials."
                    if "general_questions" in lanes
                    else f"{len(findings)} cited finding(s) for your question."
                )
            )

    calendar_notes = state.get("calendar_notes") or []
    if calendar_notes:
        parts.append("")
        parts.extend(calendar_notes)
        if not summary:
            summary = calendar_notes[0]

    # Lanes that could not run explain themselves through caveats, which the
    # envelope carries separately; the summary should still say something true.
    if not summary:
        caveats = state.get("caveats") or []
        summary = caveats[0] if caveats else "I don't have enough to answer that yet."

    next_questions: list[str] = []
    if state.get("universities"):
        first = state["universities"][0].name
        next_questions = [
            f"What's the course load at {first}?",
            f"How much does {first} cost per month?",
        ]

    return AnswerBody(
        summary=summary,
        body="\n".join(parts).strip(),
        recommendations=recommendations,
        next_questions=next_questions,
    )


def _confidence(state: dict, sources: list[SourceEvidence]) -> str:
    """How much the answer should be trusted, from what backs it."""
    if state.get("clarification"):
        return "high"  # asking a clear question is not a low-confidence act
    if not sources:
        return "low"
    failed = [r for r in (state.get("lane_results") or {}).values() if r.status == "failed"]
    skipped = [r for r in (state.get("lane_results") or {}).values() if r.status == "skipped"]
    if failed:
        return "low"
    if skipped:
        return "medium"
    return "high" if all(s.confidence == "high" for s in sources) else "medium"


def run(state: dict) -> dict:
    """Build the envelope. The single place evidence becomes an answer."""
    profile: Profile = state.get("profile") or Profile()
    lanes = list(state.get("lanes") or [])

    sources, policy_warnings, policy_caveats = _screen_sources(
        list(state.get("sources") or []), lanes
    )
    sources = _unique_sources(sources)

    caveats = list(state.get("caveats") or [])
    for caveat in policy_caveats:
        if caveat not in caveats:
            caveats.append(caveat)

    planned_tasks = _apply_statuses(
        list(state.get("planned_tasks") or []), state.get("lane_results") or {}
    )

    envelope = AnswerEnvelope(
        answer=_compose(state, profile, sources),
        sources=sources,
        confidence=_confidence(state, sources),
        caveats=caveats,
        intent=state.get("intent") or "unknown",
        profile=profile,
        planned_tasks=planned_tasks,
        universities=[card.model_dump() for card in (state.get("universities") or [])],
        workload_evidence=list(state.get("workload_evidence") or []),
        finance_evidence=list(state.get("finance_evidence") or []),
        eligibility_evidence=list(state.get("eligibility_evidence") or []),
        budget=state.get("budget"),
        budgets=list(state.get("budgets") or []),
        research_findings=list(state.get("research_findings") or []),
        conversion_results=[c.model_dump() for c in (state.get("conversion_results") or [])],
        clarification=state.get("clarification"),
        errors=list(state.get("errors") or []),
    )

    return {
        "envelope": envelope,
        "warnings": policy_warnings,
        "counters": {"sources_dropped_by_policy": len(policy_warnings)},
        "lane_results": {
            LANE: LaneResult(
                lane=LANE,
                status="complete",
                note=f"{len(sources)} source(s), {len(policy_warnings)} dropped",
            )
        },
    }
