"""The finance lane: Wise cost-of-living estimates for an exchange destination.

The location is read from ``universities.city_state`` first. Wise city pages
are attempted before the country aggregate; a limited-data city page and an
unsearchable city both use the country page, exactly as the Wise UI suggests.
"""

from __future__ import annotations

import time

from graph.domain import (
    BudgetComponent,
    BudgetEstimate,
    Profile,
    SourceEvidence,
    utc_now,
)
from graph.state import LaneResult

from data import coursefinder_db as cf
from data import host_city, wise

LANE = "finance"


def _budget_from_wise(cost: wise.CostOfLiving, university: str, location: str, budget_limit: float | None = None) -> BudgetEstimate:
    property_expense = next(
        (expense for expense in cost.expenses if expense.label.casefold() == "property"),
        None,
    )
    excludes_rent = property_expense is not None
    counted_expenses = [expense for expense in cost.expenses if expense is not property_expense]
    components = [
        BudgetComponent(
            label=f"{expense.label} ({expense.percentage}%)",
            amount=expense.amount_sgd,
            currency="SGD",
            period="monthly",
            source_title="Wise — Distribution of Expenses",
            included=not (expense is property_expense),
        )
        for expense in cost.expenses
    ]
    monthly_total = (
        round(sum(expense.amount_sgd for expense in counted_expenses), 2)
        if excludes_rent
        else cost.monthly_sgd
    )
    caveats = [
        "Cost-of-living figures are Wise estimates, not the university's own published costs.",
        f"Wise location used: {location} ({cost.level}-level data).",
        f"GBP was converted to SGD at the fixed planning rate of S${wise.GBP_TO_SGD:.2f} per £1.",
    ]
    if excludes_rent and cost.monthly_sgd is not None:
        caveats.append(
            f"Monthly total excludes Wise's Property allocation ({property_expense.percentage}%), "
            f"which is about S${property_expense.amount_sgd:,.0f}; Wise's full headline is "
            f"S${cost.monthly_sgd:,.0f}."
        )
    elif cost.expenses:
        caveats.append(
            "Wise published a distribution without a Property line, so the published total is shown; "
            "rent could not be separated reliably."
        )
    if cost.level == "country":
        caveats.append("The city/state page was incomplete or unavailable, so Wise's aggregate country data was used.")
    if budget_limit is not None and monthly_total is not None:
        comparison = "within" if monthly_total <= budget_limit else "above"
        caveats.append(
            f"This rent-excluded estimate is {comparison} your stated maximum of "
            f"S${budget_limit:,.0f} per month."
            if excludes_rent
            else f"This estimate is {comparison} your stated maximum of S${budget_limit:,.0f} per month."
        )
    return BudgetEstimate(
        university_name=university,
        components=components,
        monthly_total=monthly_total,
        monthly_total_currency="SGD",
        rent_included=False if excludes_rent else None,
        fallback_used=True,
        source_label=(
            f"Wise ({cost.level}: {location}, excluding rent)"
            if excludes_rent
            else f"Wise ({cost.level}: {location})"
        ),
        caveats=caveats,
    )


def _location_from_db(university: str, country: str | None, universities: list | None, db_path=None) -> tuple[str | None, str | None]:
    for card in universities or []:
        if getattr(card, "name", "").strip().casefold() == university.strip().casefold():
            return getattr(card, "city_state", None), getattr(card, "country", country)
    record = cf.university_by_name(university, country, db_path=db_path)
    if record:
        return record.get("city_state") or None, record.get("country") or country
    return None, country


def _wise_candidates(location: str | None, country: str | None, db_path=None) -> list[tuple[str, wise.CostOfLiving]]:
    candidates: list[tuple[str, wise.CostOfLiving]] = []
    if location:
        candidates.append(("city", wise.fetch(location, country, db_path=db_path)))
    if not candidates or not candidates[-1][1].usable:
        candidates.append(("country", wise.fetch_country(country, db_path=db_path)))
    return candidates


def _estimate(university: str, country: str | None, universities: list | None, calls: int, failures: int, started: float, budget_limit: float | None = None, db_path=None) -> dict:
    location, resolved_country = _location_from_db(university, country, universities, db_path=db_path)
    # Compatibility fallback for an older database that has not been enriched.
    if not location:
        resolved = host_city.resolve(university, resolved_country, db_path=db_path)
        if resolved:
            location = resolved.name

    candidates = _wise_candidates(location, resolved_country, db_path=db_path)
    calls += len(candidates)
    usable = next(((level, cost) for level, cost in candidates if cost.usable), None)
    if usable is None:
        failures += 1
        final = candidates[-1][1] if candidates else wise.fetch_country(resolved_country, db_path=db_path)
        return {
            "budget": BudgetEstimate(
                university_name=university,
                components=[],
                fallback_used=True,
                source_label="Wise",
                caveats=[final.error or "Wise has no usable cost-of-living data for this destination."],
            ),
            "warnings": ["wise_unavailable"],
            "counters": {"tool_calls": calls, "tool_failures": failures},
            "lane_results": {LANE: LaneResult(lane=LANE, status="failed", note="no Wise cost-of-living data", tool_calls=calls, tool_failures=failures)},
        }

    level, cost = usable
    label = location or resolved_country or "the destination"
    budget = _budget_from_wise(cost, university, label, budget_limit=budget_limit)
    return {
        "budget": budget,
        "sources": [
            SourceEvidence(
                title=f"Wise — cost of living in {label}",
                url=cost.url,
                type="wise",
                excerpt=(f"Average cost per month: £{cost.monthly_gbp:,.0f}; "
                         f"Distribution of Expenses: {', '.join(f'{e.label} {e.percentage}%' for e in cost.expenses)}"),
                retrieved_at=utc_now(),
                confidence="low",
            )
        ],
        "caveats": budget.caveats,
        "warnings": [],
        "counters": {"tool_calls": calls, "tool_failures": failures},
        "lane_results": {LANE: LaneResult(lane=LANE, status="complete", note=f"Wise {level} cost of living for {label}", duration_ms=int((time.perf_counter() - started) * 1000), tool_calls=calls, tool_failures=failures)},
    }


def run(
    profile: Profile,
    named_university: str | None = None,
    universities: list | None = None,
    university_candidates: list[str] | None = None,
    db_path=None,
) -> dict:
    """Produce a Wise estimate for the named university or first shortlist card."""
    started = time.perf_counter()

    # A partial name can legitimately identify more than one partner. Cost is
    # the one lane where that ambiguity should not block the student: return a
    # separate budget for every matching institution.
    if university_candidates and not named_university:
        budgets: list[BudgetEstimate] = []
        sources: list[SourceEvidence] = []
        caveats: list[str] = []
        warnings: list[str] = []
        total_calls = 0
        total_failures = 0
        completed = 0
        for candidate in university_candidates:
            record = cf.university_by_name(candidate, db_path=db_path)
            candidate_country = (record or {}).get("country") or profile.destination_pref
            result = _estimate(
                candidate,
                candidate_country,
                None,
                0,
                0,
                started,
                profile.max_monthly_budget_sgd,
                db_path=db_path,
            )
            budget = result.get("budget")
            if budget is not None:
                budgets.append(budget)
            sources.extend(result.get("sources") or [])
            caveats.extend(result.get("caveats") or [])
            warnings.extend(result.get("warnings") or [])
            counters = result.get("counters") or {}
            total_calls += int(counters.get("tool_calls") or 0)
            total_failures += int(counters.get("tool_failures") or 0)
            if (result.get("lane_results") or {}).get(LANE, LaneResult(lane=LANE)).status == "complete":
                completed += 1
        status = "complete" if completed else "failed"
        return {
            "budget": budgets[0] if len(budgets) == 1 else None,
            "budgets": budgets,
            "sources": sources,
            "caveats": caveats,
            "warnings": warnings,
            "counters": {"tool_calls": total_calls, "tool_failures": total_failures},
            "lane_results": {
                LANE: LaneResult(
                    lane=LANE,
                    status=status,
                    note=f"Wise cost of living for {len(university_candidates)} matching universities",
                    tool_calls=total_calls,
                    tool_failures=total_failures,
                )
            },
        }

    university = named_university
    country = profile.destination_pref
    if not university:
        for card in universities or []:
            name = getattr(card, "name", None)
            if name:
                university, country = name, getattr(card, "country", country)
                break
    if not university and profile.school_code:
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
                limit=1,
                preview=1,
            )
            if cards:
                university, country, universities = cards[0].name, cards[0].country, cards
        except cf.CoursefinderUnavailable:
            university = None

    if not university:
        return {"caveats": ["I need a named university or shortlist before I can compare costs."], "lane_results": {LANE: LaneResult(lane=LANE, status="skipped", note="no university to cost")}}
    result = _estimate(
        university,
        country,
        universities,
        0,
        0,
        started,
        profile.max_monthly_budget_sgd,
        db_path=db_path,
    )
    if result.get("budget") is not None:
        result["budgets"] = [result["budget"]]
    return result
