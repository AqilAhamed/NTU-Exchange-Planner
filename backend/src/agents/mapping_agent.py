"""The core planning lane: which partner universities actually work.

Everything this lane emits comes from ``coursefinder.db`` through the
parameterized queries in :mod:`data.coursefinder_db`. It never reads the open
web, and the source policy enforces that rather than trusting it.

Citations point at the API query that reproduces the claim. That is a
deliberate choice: the underlying records are NTU's internal Coursefinder
submissions and have no public per-mapping URL, so the honest citation is the
exact, re-runnable query behind the number — not a plausible-looking link to a
page that does not contain it. The one thing it must never be is blank; the
previous build shipped an empty ``url`` and every citation in the product was
unverifiable.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from data import coursefinder_db as cf
from data import gem_client, gem_parser
from graph.domain import EligibilityEvidence, Profile, SourceEvidence, utc_now
from graph.state import LaneResult

LANE = "course_matching"
# Each candidate costs one brochure read, so the pool a CGPA filter examines is
# bounded. When the bound bites, the shortfall is stated rather than hidden.
MAX_CGPA_CHECKS = 50


def _check_cgpa(card, student_cgpa: float) -> tuple[object, EligibilityEvidence | None, str | None]:
    """Read one partner's published CGPA requirement for shortlist filtering."""
    result = gem_client.brochure_for(card.name, card.country)
    if not result.ok or not result.data:
        return card, None, f"Could not verify the published CGPA requirement for {card.name}."
    url = result.program.url if result.program else ""
    evidence = gem_parser.parse_cgpa(
        result.data, card.name, url, student_cgpa=student_cgpa
    )
    if evidence is None:
        return card, None, f"{card.name} publishes no readable minimum CGPA requirement."
    return card, evidence, None


def filter_by_cgpa(cards: list, student_cgpa: float) -> tuple[list, list, list[str]]:
    """Keep only universities whose published 5-point minimum the student meets.

    Unknown requirements are excluded from a CGPA-filtered shortlist. Showing
    them would contradict a request for universities at or below the student's
    threshold; the reason is returned as a caveat so the omission is visible.
    """
    if not cards:
        return [], [], []
    with ThreadPoolExecutor(max_workers=min(8, len(cards))) as pool:
        outcomes = list(pool.map(lambda card: _check_cgpa(card, student_cgpa), cards))

    kept: list = []
    evidence: list[EligibilityEvidence] = []
    caveats: list[str] = []
    for card, item, issue in outcomes:
        if item is not None and item.required_value is not None and item.meets is True:
            kept.append(card)
            evidence.append(item)
        elif item is not None and item.required_value is not None:
            caveats.append(
                f"{card.name} was left out because its published minimum CGPA is "
                f"{item.required_value:g}, above your {student_cgpa:g}."
            )
        elif issue:
            caveats.append(f"{issue} It was left out of the CGPA-filtered results.")
    return kept, evidence, caveats


@dataclass(frozen=True)
class CgpaPage:
    """One page of a CGPA-filtered shortlist, and what it does not cover."""

    cards: list = field(default_factory=list)
    total: int = 0
    has_more: bool = False
    evidence: list = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    examined: int = 0
    pool_total: int = 0

    @property
    def truncated(self) -> bool:
        """True when partners existed that no brochure check was spent on."""
        return self.pool_total > self.examined


def apply_cgpa_filter(
    pool: list,
    pool_total: int,
    student_cgpa: float,
    offset: int = 0,
    limit: int = cf.DEFAULT_PAGE,
) -> CgpaPage:
    """Filter a candidate pool by published minimum CGPA and page the result.

    ``pool`` is the candidates actually fetched, capped at
    :data:`MAX_CGPA_CHECKS`; ``pool_total`` is how many partners matched the
    query before that cap. The two differ whenever the cap bites, and the
    difference is stated as a caveat rather than folded away — ``total`` here
    means "eligible among those checked", never "eligible overall", because
    the second number is not something this pass measured.

    Shared by the graph lane and ``GET /api/universities`` so a page fetched by
    "show more" is filtered by exactly the same rule as the first one.
    """
    kept, evidence, caveats = filter_by_cgpa(pool, student_cgpa)
    page = kept[offset : offset + limit] if limit > 0 else []
    result = CgpaPage(
        cards=page,
        total=len(kept),
        has_more=offset + len(page) < len(kept),
        evidence=evidence,
        caveats=caveats,
        examined=len(pool),
        pool_total=max(pool_total, len(pool)),
    )
    if result.truncated:
        caveats.append(
            f"I checked the {result.examined} partner universities with the most approved "
            f"mappings against their published minimum CGPA, not all {result.pool_total}. "
            "Narrow by country or module to bring the rest into range."
        )
    return result


def _query_url(
    school_code: str,
    programme_type: str,
    country: str | None,
    university_id: int | None = None,
    module_codes: list[str] | None = None,
    exclude_module_types: list[str] | None = None,
    include_module_types: list[str] | None = None,
    restored_module_types: list[str] | None = None,
    countries: list[str] | None = None,
) -> str:
    """The API call that reproduces a claim, so a reader can re-run it."""
    if university_id is not None:
        query = (
            f"/backend/api/universities/{university_id}/mappings"
            f"?school={school_code}&programme_type={programme_type}&offset=0&limit=50"
        )
    else:
        query = f"/backend/api/universities?school={school_code}&programme_type={programme_type}&offset=0"
    if country:
        query += f"&country={country}"
    for code in module_codes or []:
        query += f"&module_codes={code}"
    for value in exclude_module_types or []:
        query += f"&exclude_module_types={value}"
    for value in include_module_types or []:
        query += f"&include_module_types={value}"
    for value in restored_module_types or []:
        query += f"&restore_module_types={value}"
    for value in countries or []:
        query += f"&countries={value}"
    return query


def _single_university_card(
    name: str,
    school_code: str,
    programme_type: str,
    profile: Profile,
) -> tuple[list, int, bool]:
    """Return only the named university's mappings when a follow-up refers to one place."""
    match = next(
        (
            (university_id, uni_name, country)
            for university_id, uni_name, country in cf.all_university_names()
            if uni_name.lower() == name.lower()
        ),
        None,
    )
    if match is None:
        return [], 0, False

    university_id, uni_name, country = match
    count = cf.approved_mapping_count(
        university_id,
        school_code,
        programme_type,
        module_codes=profile.module_codes,
        exclude_module_types=profile.excluded_module_types,
        include_module_types=profile.included_module_types,
        restored_module_types=profile.restored_module_types,
    )
    if count <= 0:
        return [], 0, False
    preview = cf.approved_mappings(
        university_id=university_id,
        school_code=school_code,
        programme_type=programme_type,
        offset=0,
        limit=cf.DEFAULT_PREVIEW,
        module_codes=profile.module_codes,
        exclude_module_types=profile.excluded_module_types,
        include_module_types=profile.included_module_types,
        restored_module_types=profile.restored_module_types,
    )
    from graph.domain import UniversityCard

    return [
        UniversityCard(
            university_id=university_id,
            name=uni_name,
            country=country,
            approved_count=count,
            programme_type=programme_type,
            mappings_preview=preview,
            preview_shown=len(preview),
        )
    ], count, False


def run(
    profile: Profile,
    offset: int = 0,
    limit: int = cf.DEFAULT_PAGE,
    named_university: str | None = None,
    cgpa: float | None = None,
) -> dict:
    """Build the shortlist. Returns the state patch the graph node applies."""
    started = time.perf_counter()
    warnings: list[str] = []

    if not profile.school_code:
        return {
            "lane_results": {
                LANE: LaneResult(
                    lane=LANE, status="skipped", note="no programme code on the profile"
                )
            }
        }

    programme_type = (profile.programme_type or "GEMX").upper()
    country = profile.destination_pref

    try:
        if named_university:
            cards, total, has_more = _single_university_card(
                named_university, profile.school_code, programme_type, profile
            )
        else:
            cards, total, has_more = cf.eligible_universities(
                school_code=profile.school_code,
                programme_type=programme_type,
                country=country,
                countries=(profile.destination_countries if not country else []),
                # A CGPA filter is applied after the external brochure check,
                # so pagination must start from the same candidate pool and
                # be applied only after ineligible universities are removed.
                offset=(0 if cgpa is not None else offset),
                # Eligibility filtering needs a candidate pool larger than the
                # visible page; the final page is rebuilt after verification.
                limit=(MAX_CGPA_CHECKS if cgpa is not None else limit),
                module_codes=profile.module_codes,
                exclude_module_types=profile.excluded_module_types,
                include_module_types=profile.included_module_types,
                restored_module_types=profile.restored_module_types,
            )
    except cf.CoursefinderUnavailable as exc:
        return {
            "warnings": [f"coursefinder_unavailable: {exc}"],
            "counters": {"tool_calls": 1, "tool_failures": 1},
            "lane_results": {
                LANE: LaneResult(lane=LANE, status="failed", note=str(exc), tool_calls=1, tool_failures=1)
            },
        }

    caveats: list[str] = []
    eligibility_evidence: list[EligibilityEvidence] = []
    if cgpa is not None:
        page = apply_cgpa_filter(cards, total, cgpa, offset=offset, limit=limit)
        cards = page.cards
        eligibility_evidence = page.evidence
        caveats.extend(page.caveats)
        total = page.total
        has_more = page.has_more
        if not cards:
            caveats.append(
                f"I could not find a university with a published minimum CGPA at or below "
                f"your {cgpa:g}."
            )
    if named_university and not cards:
        caveats.append(
            f"I found {named_university}, but Coursefinder has no approved {programme_type} "
            f"mappings for {profile.school_name or profile.school_code} there."
        )
    elif country and not cards:
        caveats.append(
            f"No partner university in {country.title()} has an approved mapping for "
            f"{profile.school_name or profile.school_code}. Try removing the country filter."
        )
    elif not country and not cards:
        caveats.append(
            f"Coursefinder holds no approved {programme_type} mappings for "
            f"{profile.school_name or profile.school_code}."
        )

    sources: list[SourceEvidence] = []
    if cards:
        scope = f" in {country.title()}" if country else ""
        sources.append(
            SourceEvidence(
                title=(
                    f"NTU Coursefinder — approved {programme_type} mappings for "
                    f"{profile.school_name or profile.school_code}"
                ),
                url=_query_url(
                    profile.school_code,
                    programme_type,
                    country,
                    module_codes=profile.module_codes,
                    exclude_module_types=profile.excluded_module_types,
                    include_module_types=profile.included_module_types,
                    restored_module_types=profile.restored_module_types,
                    countries=(profile.destination_countries if not country else []),
                ),
                type="coursefinder",
                excerpt=(
                    f"{total} partner universit{'y' if total == 1 else 'ies'}{scope} hold at least "
                    f"one approved module mapping. Showing {len(cards)}, led by {cards[0].name} "
                    f"with {cards[0].approved_count} approved mappings."
                ),
                retrieved_at=utc_now(),
                confidence="high",
            )
        )
        # One citation per card, so every number on screen is traceable.
        for card in cards:
            sources.append(
                SourceEvidence(
                    title=f"NTU Coursefinder — {card.name}",
                    url=_query_url(
                        profile.school_code,
                        programme_type,
                        None,
                        university_id=card.university_id,
                        module_codes=profile.module_codes,
                        exclude_module_types=profile.excluded_module_types,
                        include_module_types=profile.included_module_types,
                        restored_module_types=profile.restored_module_types,
                        countries=(profile.destination_countries if not country else []),
                    ),
                    type="coursefinder",
                    excerpt=(
                        f"{card.approved_count} approved mappings for "
                        f"{profile.school_name or profile.school_code} at {card.name}, "
                        f"{card.country.title()}."
                    ),
                    retrieved_at=utc_now(),
                    confidence="high",
                )
            )

    elapsed = int((time.perf_counter() - started) * 1000)
    return {
        "universities": cards,
        "eligibility_evidence": eligibility_evidence,
        "sources": sources,
        "caveats": caveats,
        "warnings": warnings,
        "total_universities": total,
        "has_more": has_more,
        "counters": {"tool_calls": 1},
        "lane_results": {
            LANE: LaneResult(
                lane=LANE,
                status="complete" if cards else "complete",
                note=f"{len(cards)} of {total} universities",
                duration_ms=elapsed,
                tool_calls=1,
            )
        },
    }
