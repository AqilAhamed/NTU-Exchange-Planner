"""Request and response models for the HTTP layer.

Every endpoint declares one. Returning a bare dict would make the OpenAPI
schema a lie and would let a field silently disappear from what the UI reads.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from graph.domain import (
    AnswerBody,
    AnswerError,
    BudgetEstimate,
    Conversion,
    EligibilityEvidence,
    FinanceEvidence,
    MappingRow,
    PlannedTask,
    Profile,
    Programme,
    ResearchFinding,
    SourceEvidence,
    Strict,
    UniversityCard,
    WorkloadEvidence,
)


class HealthResponse(BaseModel):
    """Liveness plus enough detail to diagnose a broken local setup at a glance."""

    status: str = Field(description="'ok' when the Coursefinder database is readable.")
    llm_provider: str = Field(description="Provider selected by LLM_PROVIDER.")
    llm_configured: bool = Field(
        description="True when the selected provider has credentials. The "
        "deterministic lanes work regardless."
    )
    storage_backend: str = Field(
        default="sqlite",
        description="Where sessions, chats and the cache live: 'sqlite' or 'dynamodb'.",
    )
    coursefinder_db: str = Field(description="Resolved path to the read-only database.")
    available: bool = Field(description="Whether the database opened successfully.")
    universities: int | None = Field(default=None, description="Partner university count.")
    school_programmes: int | None = Field(default=None, description="NTU programme count.")
    mappings: int | None = Field(default=None, description="Total module mapping rows.")
    submissions: int | None = Field(default=None, description="Detail submission rows.")
    submission_fields: int | None = Field(default=None, description="Detail field rows.")
    general_questions_chroma_chunks: int | None = Field(
        default=None, description="Indexed NTU-student RAG chunk count."
    )
    general_questions_chroma_path: str | None = Field(
        default=None, description="Resolved runtime ChromaDB path."
    )
    cost_of_living_excludes_rent: bool = Field(
        default=True, description="Whether Wise Property allocations are excluded from displayed budgets."
    )
    error: str | None = Field(default=None, description="Present only when unavailable.")


class UniversitiesResponse(Strict):
    """One page of the shortlist. Field names are fixed by ``api.ts``."""

    cards: list[UniversityCard] = Field(description="Universities on this page.")
    has_more: bool = Field(description="Whether a further page exists.")
    total: int = Field(
        default=0,
        description=(
            "Universities matching the whole filter. Under a CGPA filter this is the "
            "number eligible among those actually checked, not a claim about the rest."
        ),
    )
    offset: int = Field(default=0, description="Offset this page started at.")
    notes: list[str] = Field(
        default_factory=list,
        description="What this page does not cover, such as a truncated CGPA check.",
    )


class MappingsResponse(Strict):
    """One page of a university's approved mappings."""

    mappings: list[MappingRow] = Field(description="De-duplicated rows, ranked by module type.")
    has_more: bool = Field(default=False, description="Whether a further page exists.")
    total: int = Field(default=0, description="De-duplicated mappings for this university.")


class MappingDetailsResponse(Strict):
    """The most recent student submission for one mapping."""

    details: dict[str, str] = Field(description="Field label to value. Empty when none on file.")


class ProgrammesResponse(Strict):
    """The NTU programmes and partner countries the database actually holds."""

    programmes: list[Programme] = Field(description="Every NTU programme.")
    countries: list[str] = Field(description="Every country with a partner university.")


class ChatSummary(Strict):
    """A row in the sidebar's chat list."""

    session_id: str = Field(description="Session identifier.")
    title: str = Field(description="Chat title, taken from the first message.")
    folder: str | None = Field(default=None, description="Reserved for grouping.")
    created_at: str = Field(description="ISO-8601 UTC creation time.")
    updated_at: str = Field(description="ISO-8601 UTC time of the last turn.")


class ChatsResponse(Strict):
    chats: list[ChatSummary] = Field(description="Most recently used first.")


class CreatedChatResponse(Strict):
    session_id: str = Field(description="Identifier of the newly created chat.")


class ChatMessage(BaseModel):
    """One turn. ``payload`` carries the whole answer envelope for assistant turns."""

    model_config = ConfigDict(extra="allow")

    role: str = Field(description="'user' or 'assistant'.")
    content: str = Field(description="The message text.")


class ChatHistoryResponse(Strict):
    session_id: str = Field(description="Session identifier.")
    messages: list[ChatMessage] = Field(description="The full conversation, oldest first.")


class DeletedChatResponse(Strict):
    deleted: bool = Field(description="Whether a chat was removed.")
    session_id: str = Field(description="Session identifier that was requested.")


class ChatRequest(BaseModel):
    """One turn from the student. ``session_id`` is absent on the first message."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(description="What the student typed.")
    session_id: str | None = Field(default=None, description="Existing chat to continue.")


class ChatResponse(BaseModel):
    """The answer envelope, plus the flat keys the current frontend reads.

    ``api.ts`` declares ``Payload`` with both shapes: the structured envelope
    fields and a set of legacy keys (``narration``, ``cards``, ``conversions``,
    ``research``, ``has_more``). Both are populated from the same evidence, so
    they cannot disagree. The legacy keys move under a ``legacy`` object once
    the components that read them are rewritten.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(description="Chat this turn belongs to.")
    messages: list[ChatMessage] = Field(description="The full conversation after this turn.")

    # --- envelope ---
    answer: AnswerBody = Field(description="Summary, body, recommendations, follow-ups.")
    sources: list[SourceEvidence] = Field(description="Every citation behind this answer.")
    confidence: str = Field(description="'high', 'medium' or 'low'.")
    caveats: list[str] = Field(description="Limits the student must know about.")
    intent: str = Field(description="What the router decided this question was.")
    planned_tasks: list[PlannedTask] = Field(description="The plan and each task's outcome.")
    universities: list[dict[str, Any]] = Field(description="Serialized university cards.")
    workload_evidence: list[WorkloadEvidence] = Field(description="Host course-load rules.")
    finance_evidence: list[FinanceEvidence] = Field(description="Published cost figures.")
    eligibility_evidence: list[EligibilityEvidence] = Field(
        description="Entry requirements checked against the student's profile."
    )
    budget: BudgetEstimate | None = Field(default=None, description="Assembled budget, if asked.")
    budgets: list[BudgetEstimate] = Field(
        default_factory=list, description="One budget per matching university."
    )
    research_findings: list[ResearchFinding] = Field(description="Cited qualitative findings.")
    conversion_results: list[dict[str, Any]] = Field(description="Explicit conversions.")
    errors: list[AnswerError] = Field(description="Failures worth surfacing.")
    clarification: str | None = Field(default=None, description="Set when a detail is missing.")
    profile: Profile | None = Field(default=None, description="Profile used for this turn.")

    # --- legacy keys the current components read ---
    narration: str = Field(default="", description="Prose shown as the assistant message.")
    cards: list[UniversityCard] = Field(default_factory=list, description="Shortlist cards.")
    total_universities: int = Field(default=0, description="Universities matching the filter.")
    conversions: list[Conversion] = Field(default_factory=list, description="Conversion summaries.")
    research: str | None = Field(default=None, description="Legacy research prose slot.")
    has_more: bool = Field(default=False, description="Whether more universities exist.")


class ResearchRequest(BaseModel):
    """The body UniversityCard.knowMore() posts (UniversityCard.tsx:96)."""

    model_config = ConfigDict(extra="forbid")

    university_id: int = Field(description="Coursefinder university id.")
    name: str = Field(description="University name as Coursefinder records it.")
    country: str | None = Field(default=None, description="Country, used to disambiguate.")
    term: str | None = Field(default=None, description="Exchange term, when known.")
    school: str | None = Field(default=None, description="NTU programme code.")
    programme_type: str | None = Field(default=None, description="'GEMX' or 'SUSEP'.")


class CostOfLivingOut(Strict):
    """Wise cost-of-living figures, in the shape UniversityCard already renders."""

    city: str | None = Field(default=None, description="City, when the source names one.")
    summary: str | None = Field(default=None, description="One-line description.")
    vs_singapore: str | None = Field(default=None, description="Unused; comparison not published.")
    single_person_monthly: str | None = Field(default=None, description="Total, if published.")
    monthly_total_sgd: float | None = Field(
        default=None,
        description="The current displayed monthly total in SGD; excludes Property when rent_included is false.",
    )
    rent_included: bool | None = Field(default=None, description="False when Wise's Property allocation is excluded.")
    items: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Label/price pairs, with optional numeric amount_sgd for interactive totals.",
    )
    url: str | None = Field(default=None, description="Where the figures were published.")
    source: str | None = Field(default=None, description="Which source they came from.")
    error: str | None = Field(default=None, description="Present when nothing was usable.")


class UniversityBriefing(Strict):
    """The 'Know more' panel. Mirrors the UniversityBriefing type in api.ts.

    ``min_au`` and ``max_au`` are populated **only** when the brochure states an
    explicit conversion basis. A blank AU figure beside a populated native
    figure is the honest reading of a university that publishes ECTS or credits
    and says nothing about NTU academic units.
    """

    name: str = Field(description="University name.")
    country: str | None = Field(default=None, description="Country.")
    gem_program: str | None = Field(default=None, description="Matched GEM programme title.")
    term: str | None = Field(default=None, description="Exchange term this was asked for.")
    source: str | None = Field(default=None, description="Where the briefing came from.")
    course_load_raw: str | None = Field(default=None, description="The rule in its own words.")
    au_summary: str | None = Field(default=None, description="What can and cannot be converted.")
    min_ects: float | None = Field(default=None, description="Set only when the unit is ECTS.")
    max_ects: float | None = Field(default=None, description="Set only when the unit is ECTS.")
    min_au: float | None = Field(default=None, description="Only with a cited conversion basis.")
    max_au: float | None = Field(default=None, description="Only with a cited conversion basis.")
    au_note: str = Field(default="", description="Why a conversion was or was not made.")
    planning_reference: str | None = Field(
        default=None,
        description="A clearly labelled ECTS-only planning reference, never an official conversion.",
    )
    gpa: str | None = Field(default=None, description="Published minimum CGPA.")
    brochure_url: str | None = Field(default=None, description="The real brochure link.")
    error: str | None = Field(default=None, description="Present when the portal was unreachable.")
    cost_of_living: CostOfLivingOut | None = Field(default=None, description="Cost of living.")
    module_conversions: list[dict[str, Any]] = Field(
        default_factory=list, description="Empty: per-module AU conversion is not published."
    )
