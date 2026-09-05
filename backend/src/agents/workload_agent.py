"""The workload lane: what a host university requires, in its own units.

This is the lane the product is really about, and the reason a fixed workflow
cannot do this job. The five captured brochures publish course load five
different ways, verbatim:

* Aalborg — ``Minimum | Maximum`` / ``30 ECTS | 30 ECTS``
* Ajou — ``6 credits (usually 2 courses)`` to ``19 credits (usually 6~7 courses)``
* Akita — ``Minimum : 12 credits (required by Japanese immigration law)``
* Queen's — ``15 credits per term (equivalent to 30 ECTS/year)``
* Macalester — nothing at all

Three unit systems, one legal constraint, one stated equivalence, and one
silence. The lane reports each in the university's own words and converts only
where the source itself states a basis. Where it cannot, it says so — and that
honest "conversion not available" is the behaviour worth demonstrating, not the
one to paper over.
"""

from __future__ import annotations

import time

from data import gem_client, gem_parser
from graph.domain import Profile, SourceEvidence, utc_now
from graph.state import LaneResult

LANE = "workload"

# How many universities to fetch brochures for in one turn. Each is a network
# round trip, so an unbounded shortlist would make the lane the slowest thing
# in the product.
MAX_UNIVERSITIES = 3


def _for_university(name: str, country: str | None, cgpa: float | None) -> dict:
    """Fetch and parse one university's brochure. Never raises."""
    result = gem_client.brochure_for(name, country)
    if not result.ok or not result.data:
        return {"warnings": result.warnings, "workload": None, "eligibility": None, "sources": []}

    url = result.program.url if result.program else ""
    workload, warnings = gem_parser.parse_workload(result.data, name, url)
    eligibility = gem_parser.parse_cgpa(result.data, name, url, student_cgpa=cgpa)

    sources: list[SourceEvidence] = []
    if workload is not None:
        sources.append(
            SourceEvidence(
                title=f"GEM Explorer — {name} course load",
                url=url,
                type="gem_explorer",
                excerpt=workload.native_unit_text[:300],
                retrieved_at=utc_now(),
                confidence="high",
            )
        )
    return {
        "warnings": warnings,
        "workload": workload,
        "eligibility": eligibility,
        "sources": sources,
    }


def run(
    profile: Profile,
    named_university: str | None = None,
    universities: list | None = None,
    cgpa: float | None = None,
) -> dict:
    """Attach course-load evidence for the universities in play."""
    started = time.perf_counter()

    targets: list[tuple[str, str | None]] = []
    if named_university:
        targets.append((named_university, profile.destination_pref))
    for card in (universities or [])[:MAX_UNIVERSITIES]:
        name = getattr(card, "name", None)
        country = getattr(card, "country", None)
        if name and name not in {t[0] for t in targets}:
            targets.append((name, country))

    # Lanes run concurrently, so this one cannot see a shortlist that the course
    # matching lane is building in the same superstep. It therefore fetches its
    # own, which is the design rule everywhere: lanes never call each other and
    # each reads its own evidence.
    if not targets and profile.school_code:
        from data import coursefinder_db as cf

        try:
            cards, _, _ = cf.eligible_universities(
                school_code=profile.school_code,
                programme_type=(profile.programme_type or "GEMX").upper(),
                country=profile.destination_pref,
                countries=(profile.destination_countries if not profile.destination_pref else []),
                module_codes=profile.module_codes,
                exclude_module_types=profile.excluded_module_types,
                include_module_types=profile.included_module_types,
                restored_module_types=profile.restored_module_types,
                limit=MAX_UNIVERSITIES,
                preview=1,
            )
            targets = [(card.name, card.country) for card in cards]
        except cf.CoursefinderUnavailable:
            targets = []

    targets = targets[:MAX_UNIVERSITIES]

    if not targets:
        return {
            "lane_results": {
                LANE: LaneResult(
                    lane=LANE, status="skipped", note="no university named or shortlisted"
                )
            }
        }

    workload_evidence = []
    eligibility_evidence = []
    sources: list[SourceEvidence] = []
    caveats: list[str] = []
    warnings: list[str] = []
    calls = failures = 0

    for name, country in targets:
        calls += 1
        outcome = _for_university(name, country, cgpa)
        warnings.extend(outcome["warnings"])
        sources.extend(outcome["sources"])
        # When the planning turn has a CGPA constraint, workload details for
        # universities that did not pass the same published requirement are
        # not relevant to the shortlist the user asked for.
        if cgpa is not None and (
            outcome["eligibility"] is None or outcome["eligibility"].meets is not True
        ):
            caveats.append(
                f"I left out workload details for {name} because its published CGPA requirement "
                "could not be verified as met."
            )
            continue
        if outcome["workload"] is not None:
            workload_evidence.append(outcome["workload"])
        else:
            failures += 1
            caveats.append(
                f"{name} does not publish a course load in its GEM Explorer brochure, so I "
                "have no figure to give you. Ask the host university directly."
            )
        if outcome["eligibility"] is not None:
            eligibility_evidence.append(outcome["eligibility"])

    # The caveat that matters most: a workload we could read but cannot convert.
    unconverted = [w for w in workload_evidence if w.conversion_status == "unsupported"]
    if unconverted:
        names = ", ".join(w.university_name for w in unconverted)
        caveats.append(
            f"Course loads for {names} are shown in that university's own units. Neither NTU "
            "nor the university publishes a conversion to AUs, so I have not invented one — "
            "your school's exchange coordinator confirms the AU award."
        )

    return {
        "workload_evidence": workload_evidence,
        "eligibility_evidence": eligibility_evidence,
        "sources": sources,
        "caveats": caveats,
        "warnings": warnings,
        "counters": {"tool_calls": calls, "tool_failures": failures},
        "lane_results": {
            LANE: LaneResult(
                lane=LANE,
                status="complete" if workload_evidence else "failed",
                note=f"{len(workload_evidence)} of {calls} brochures published a course load",
                duration_ms=int((time.perf_counter() - started) * 1000),
                tool_calls=calls,
                tool_failures=failures,
                warnings=warnings[:5],
            )
        },
    }
