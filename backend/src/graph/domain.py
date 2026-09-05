"""The domain contracts, mirroring ``frontend/lib/api.ts`` field for field.

That file is the schema of record. Every model here has a counterpart there,
and a mismatch is a bug in this file, not in the frontend.

Two conventions, both required by the hackathon's technical-quality criterion:

* ``extra="forbid"`` — a typo in a field name fails loudly at construction
  instead of silently vanishing from the JSON the UI reads.
* every field carries ``Field(description=...)`` — these models are also the
  tool schemas the agents are prompted with, and "descriptions are the prompt".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low"]
Intent = Literal[
    "core_planning", "research", "mixed", "official_docs", "general_questions", "unknown"
]
SourceType = Literal[
    "coursefinder",
    "gem_explorer",
    "ntu_intranet",
    "official",
    "web",
    "reddit",
    "wise",
    "local",
    "unknown",
]
TaskKind = Literal[
    "profile",
    "course_matching",
    "workload",
    "finance",
    "research",
    "conversion",
    "official_docs",
    "general_questions",
    "clarification",
]
TaskStatus = Literal["pending", "running", "complete", "skipped", "failed"]
ConversionStatus = Literal["not_applicable", "supported", "unsupported", "unknown"]


def utc_now() -> str:
    """A single timestamp format for every ``retrieved_at`` in the system."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Strict(BaseModel):
    """Base for every contract model: unknown fields are an error."""

    model_config = ConfigDict(extra="forbid")


# --- Coursefinder ---------------------------------------------------------


class Programme(Strict):
    """An NTU degree programme, read from ``school_programmes``."""

    code: str = Field(description="NTU programme code, e.g. 'CSC'.")
    name: str = Field(description="Full programme name, e.g. 'Computer Science'.")


class MappingRow(Strict):
    """One approved module mapping between a host university and NTU."""

    mapping_id: int = Field(description="Primary key in the mappings table.")
    host_module_code: str = Field(description="Module code at the host university.")
    host_module_title: str = Field(description="Module title at the host university.")
    ntu_module_code: str = Field(description="NTU module code, or a type token such as 'BDE'.")
    ntu_module_title: str = Field(description="NTU module title.")
    ntu_module_type: str = Field(description="Core, Major-PE, BDE, UE and so on.")
    credits: float = Field(description="NTU academic units awarded. 0 when unpublished.")
    year: str = Field(description="Academic year the mapping was approved for.")
    sem: str = Field(description="Semester, '1' or '2'.")
    has_details: bool = Field(description="True when a student submission is on file.")


class UniversityCard(Strict):
    """A shortlisted partner university with a preview of its mappings."""

    university_id: int = Field(description="Primary key in the universities table.")
    name: str = Field(description="Partner university name as Coursefinder records it.")
    country: str = Field(description="Country, stored uppercase in the database.")
    city_state: str | None = Field(
        default=None, description="Resolved host city/state from the universities table."
    )
    approved_count: int = Field(
        description="Distinct approved mappings for this programme, after de-duplication."
    )
    programme_type: str = Field(description="'GEMX' or 'SUSEP'.")
    mappings_preview: list[MappingRow] = Field(
        default_factory=list, description="First page of mappings, highest-value types first."
    )
    preview_shown: int = Field(default=0, description="How many rows the preview holds.")


# --- evidence -------------------------------------------------------------


class SourceEvidence(Strict):
    """A citation. Every claim the system renders must trace back to one."""

    title: str = Field(description="Human-readable name of the source.")
    url: str = Field(description="Where the claim can be verified. Never blank.")
    type: SourceType = Field(description="Which lane produced it; enforced by the source policy.")
    excerpt: str = Field(description="The supporting text, quoted rather than paraphrased.")
    retrieved_at: str = Field(default_factory=utc_now, description="ISO-8601 UTC fetch time.")
    confidence: Confidence = Field(default="medium", description="Trust in this source.")


class PlannedTask(Strict):
    """One unit of the plan the router commits to before any work is done."""

    kind: TaskKind = Field(description="Which lane this task belongs to.")
    status: TaskStatus = Field(default="pending", description="Lifecycle state, streamed to the UI.")
    description: str = Field(description="What the lane is doing, in the student's language.")
    warnings: list[str] = Field(default_factory=list, description="Non-fatal problems hit.")


class ConversionBasis(Strict):
    """The cited evidence that permits a unit conversion. No basis, no conversion."""

    from_unit: str = Field(description="Unit named in the source, e.g. 'ECTS'.")
    to_unit: str = Field(description="Unit converted to, e.g. 'AU'.")
    factor: float = Field(description="Multiplier stated by the source, never assumed.")
    source_excerpt: str = Field(description="The sentence that states the basis.")
    source_url: str | None = Field(default=None, description="Where the basis was published.")


class WorkloadEvidence(Strict):
    """A host university's course-load rule, in that university's own units."""

    university_name: str = Field(description="Host university the rule belongs to.")
    native_unit_text: str = Field(description="The rule as published, e.g. 'min 30 / max 30 ECTS'.")
    unit_label: str | None = Field(default=None, description="ECTS, credits, modules, or None.")
    minimum_value: float | None = Field(default=None, description="Lower bound in native units.")
    maximum_value: float | None = Field(default=None, description="Upper bound in native units.")
    module_count_minimum: int | None = Field(default=None, description="Minimum number of courses.")
    module_count_maximum: int | None = Field(default=None, description="Maximum number of courses.")
    raw_source_excerpt: str = Field(description="Unedited text the values were read from.")
    source_url: str = Field(description="Brochure URL. Asserted non-empty by the fixture tests.")
    retrieved_at: str = Field(default_factory=utc_now, description="ISO-8601 UTC fetch time.")
    conversion_status: ConversionStatus = Field(
        default="unknown",
        description="'supported' only when the source states an explicit conversion basis.",
    )
    conversion_basis: ConversionBasis | None = Field(
        default=None, description="Present only when conversion_status is 'supported'."
    )
    planning_reference: str | None = Field(
        default=None,
        description=(
            "An explicitly labelled, non-official ECTS planning reference for the UI. "
            "Never a host-university or NTU-certified conversion."
        ),
    )
    parse_warnings: list[str] = Field(default_factory=list, description="What could not be read.")


class FinanceEvidence(Strict):
    """One published cost figure, kept in its own currency and period."""

    university_name: str | None = Field(default=None, description="Host university, when known.")
    label: str = Field(description="What the figure covers, e.g. 'Living expenses'.")
    amount: float | None = Field(default=None, description="The figure, unconverted.")
    currency: str | None = Field(default=None, description="ISO currency code as published.")
    period: str | None = Field(default=None, description="'monthly', 'semester', 'year'.")
    includes_rent: bool | None = Field(default=None, description="None when the source is silent.")
    included_items: list[str] = Field(default_factory=list, description="What the figure covers.")
    excluded_items: list[str] = Field(default_factory=list, description="What it excludes.")
    raw_source_excerpt: str = Field(description="Unedited text the figure was read from.")
    source: SourceEvidence | None = Field(default=None, description="Citation for the figure.")
    parse_warnings: list[str] = Field(default_factory=list, description="What could not be read.")


class BudgetComponent(Strict):
    """One line of a budget. Components are never silently summed across currencies."""

    label: str = Field(description="What this line covers.")
    amount: float | None = Field(default=None, description="The figure, in this line's currency.")
    currency: str | None = Field(default=None, description="ISO currency code.")
    period: str = Field(description="'monthly', 'semester', 'year'.")
    source_title: str | None = Field(default=None, description="Which source published it.")
    included: bool = Field(default=True, description="Whether it counts toward the total.")


class BudgetEstimate(Strict):
    """A monthly budget, assembled from components that keep their own sources."""

    university_name: str | None = Field(default=None, description="Host university.")
    components: list[BudgetComponent] = Field(default_factory=list, description="The line items.")
    monthly_total: float | None = Field(
        default=None, description="Withheld entirely when components span currencies."
    )
    monthly_total_currency: str | None = Field(default=None, description="Currency of the total.")
    rent_included: bool | None = Field(default=None, description="None when unstated.")
    fallback_used: bool = Field(
        default=False, description="True when cost-of-living data replaced official figures."
    )
    source_label: str | None = Field(default=None, description="'GEM Explorer' or 'Wise'.")
    caveats: list[str] = Field(default_factory=list, description="Everything the student must know.")


class ResearchFinding(Strict):
    """A qualitative claim that carries its own citation, or it is dropped."""

    question: str = Field(description="The question this finding answers.")
    claim: str = Field(description="The claim, stated plainly.")
    source: SourceEvidence = Field(description="Where the claim came from. Mandatory.")
    confidence: Confidence = Field(default="medium", description="Trust in this finding.")
    caveats: list[str] = Field(default_factory=list, description="Forum sources are always caveated.")


class EligibilityEvidence(Strict):
    """A published entry requirement checked against the student's profile."""

    university_name: str = Field(description="Host university the requirement belongs to.")
    requirement: str = Field(description="The requirement as published, e.g. 'CGPA 3.5'.")
    required_value: float | None = Field(default=None, description="Parsed threshold, when readable.")
    student_value: float | None = Field(default=None, description="The student's stated figure.")
    meets: bool | None = Field(
        default=None,
        description="True, False, or None when the requirement is not published. "
        "None never removes a university from the shortlist.",
    )
    source: SourceEvidence | None = Field(default=None, description="Citation for the requirement.")
    notes: list[str] = Field(default_factory=list, description="Why the check is inconclusive.")


# --- conversation ---------------------------------------------------------


class Profile(Strict):
    """What the system knows about the student and their current plan.

    The exchange programme and term identify the student, while the remaining
    fields are optional planning constraints extracted from natural language.
    Keeping them in the profile makes a follow-up such as ``also leave out
    BDEs`` behave like a refinement of the same shortlist rather than a new,
    unconstrained request.
    """

    school_code: str = Field(default="", description="NTU programme code. Required to answer.")
    school_name: str = Field(default="", description="Programme name for display.")
    preferred_semester: str = Field(default="", description="Exchange term. Required to answer.")
    programme_type: str = Field(default="GEMX", description="'GEMX' or 'SUSEP', never both.")
    destination_pref: str | None = Field(default=None, description="Country filter, optional.")
    destination_region: str | None = Field(
        default=None, description="Region or continent preference, e.g. 'Europe'."
    )
    destination_countries: list[str] = Field(
        default_factory=list, description="Countries represented by the region preference."
    )
    module_codes: list[str] = Field(
        default_factory=list, description="NTU module codes the shortlist must include."
    )
    excluded_module_types: list[str] = Field(
        default_factory=list, description="NTU mapping types to omit, e.g. 'BDE'."
    )
    included_module_types: list[str] = Field(
        default_factory=list, description="NTU mapping types to keep when explicitly requested."
    )
    restored_module_types: list[str] = Field(
        default_factory=list,
        description=(
            "NTU mapping types restored additively alongside the active module-code filter, "
            "for example BDE after the student asks to show it again."
        ),
    )
    cgpa: float | None = Field(
        default=None, description="Student CGPA on NTU's 5.0 scale, when stated."
    )
    max_monthly_budget_sgd: float | None = Field(
        default=None, description="Student's maximum monthly budget in SGD, when stated."
    )


class Conversion(Strict):
    """A unit conversion the student explicitly asked for."""

    kind: str = Field(description="'au' or 'currency'.")
    summary: str = Field(description="The result in one sentence, including its caveat.")
    details: dict[str, Any] = Field(default_factory=dict, description="Inputs and workings.")


class AnswerBody(Strict):
    """The prose half of an answer."""

    summary: str = Field(default="", description="One or two sentences, the headline.")
    body: str = Field(default="", description="The full answer in markdown.")
    recommendations: list[str] = Field(default_factory=list, description="Concrete next actions.")
    next_questions: list[str] = Field(default_factory=list, description="Useful follow-ups.")


class AnswerError(Strict):
    """A failure the student is entitled to see rather than have hidden."""

    code: str = Field(description="Stable machine-readable code.")
    message: str = Field(description="Plain-language explanation.")
    details: dict[str, Any] = Field(default_factory=dict, description="Diagnostic context.")


class AnswerEnvelope(Strict):
    """The single object ``render_agent`` builds and the API returns.

    No other node constructs one, so there is exactly one place where evidence
    becomes an answer.
    """

    answer: AnswerBody = Field(default_factory=AnswerBody, description="The prose answer.")
    sources: list[SourceEvidence] = Field(default_factory=list, description="Every citation used.")
    confidence: Confidence = Field(default="medium", description="Confidence in the whole answer.")
    caveats: list[str] = Field(default_factory=list, description="Limits the student must know.")
    intent: Intent = Field(default="unknown", description="What the router decided this was.")
    profile: Profile | None = Field(default=None, description="Profile used for this turn.")
    planned_tasks: list[PlannedTask] = Field(
        default_factory=list, description="The plan, with each task's final status."
    )
    universities: list[dict[str, Any]] = Field(
        default_factory=list, description="Serialized UniversityCard objects."
    )
    workload_evidence: list[WorkloadEvidence] = Field(default_factory=list, description="Load rules.")
    finance_evidence: list[FinanceEvidence] = Field(default_factory=list, description="Cost figures.")
    eligibility_evidence: list[EligibilityEvidence] = Field(
        default_factory=list,
        description="Published entry requirements checked against the student's profile.",
    )
    budget: BudgetEstimate | None = Field(default=None, description="Assembled budget, if asked for.")
    budgets: list[BudgetEstimate] = Field(
        default_factory=list, description="One budget per matching university when a partial name is ambiguous."
    )
    research_findings: list[ResearchFinding] = Field(
        default_factory=list, description="Cited qualitative findings."
    )
    conversion_results: list[dict[str, Any]] = Field(
        default_factory=list, description="Explicit conversions the student asked for."
    )
    clarification: str | None = Field(
        default=None, description="Set only when programme or term is missing."
    )
    errors: list[AnswerError] = Field(default_factory=list, description="Failures worth surfacing.")


def envelope_from_model_output(raw: Any) -> tuple[AnswerEnvelope, list[str]]:
    """Build an envelope from untrusted model output without ever raising.

    A language model will eventually return the wrong shape. When it does the
    turn must still produce a usable answer, and the failure must be *counted*
    rather than swallowed: the returned warnings feed the schema-validation
    pass-rate metric.
    """
    warnings: list[str] = []
    if isinstance(raw, AnswerEnvelope):
        return raw, warnings
    if not isinstance(raw, dict):
        warnings.append("malformed_model_output: expected an object")
        return AnswerEnvelope(), warnings
    try:
        return AnswerEnvelope.model_validate(raw), warnings
    except Exception as exc:  # noqa: BLE001 - any validation failure is the same outcome
        warnings.append(f"malformed_model_output: {exc.__class__.__name__}")

    # Second chance: keep the fields that do validate, drop the ones that do not,
    # so one bad list does not cost the student the entire answer.
    salvaged: dict[str, Any] = {}
    for name in AnswerEnvelope.model_fields:
        if name not in raw:
            continue
        try:
            AnswerEnvelope.model_validate({name: raw[name]})
        except Exception:  # noqa: BLE001
            warnings.append(f"dropped_field: {name}")
            continue
        salvaged[name] = raw[name]
    try:
        return AnswerEnvelope.model_validate(salvaged), warnings
    except Exception:  # noqa: BLE001
        warnings.append("unrecoverable_model_output")
        return AnswerEnvelope(), warnings
