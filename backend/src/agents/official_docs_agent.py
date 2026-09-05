"""The official-documents lane, which is deliberately disabled.

Visa rules, insurance requirements, application deadlines and NTU's own
exchange policy are consequential enough that a wrong answer costs a student a
semester. This system has no licensed, versioned copy of those documents, so
the lane returns a structured ``unavailable`` and points at the office that
does — rather than paraphrasing policy from memory.

The lane still exists as a node because routing to it is the correct behaviour:
a visa question must be *recognised* as a visa question and declined clearly,
not quietly answered with a module shortlist.
"""

from __future__ import annotations

from graph.state import LaneResult

LANE = "official_docs"

REFERRAL = (
    "That's an NTU policy question — visas, insurance, deadlines and application rules. "
    "I don't carry NTU's official documents, and policy is exactly the wrong thing to "
    "answer from memory, so please check with NTU Global Education & Mobility or the "
    "GEM Explorer portal directly. I can still help with module mappings, workload and "
    "shortlisting."
)


def run(message: str = "") -> dict:
    """Decline clearly and say who can answer."""
    return {
        "caveats": [REFERRAL],
        "lane_results": {
            LANE: LaneResult(
                lane=LANE,
                status="skipped",
                note="official_docs lane is disabled by design",
                warnings=["official_docs_unavailable"],
            )
        },
    }
