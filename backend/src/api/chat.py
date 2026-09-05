"""FastAPI application.

The frontend in ``frontend/`` is the contract: ``frontend/lib/api.ts`` declares
the exact shapes these endpoints must return, and ``next.config.ts`` proxies
``/backend/*`` to this app on port 8000. A mismatch here is a bug here.

Phases 0-2 ship the health probe, the Coursefinder endpoints, the chat store
and the planning graph behind ``POST /api/chat``. ``POST /api/research`` and
``POST /api/chat/stream`` land in Phases 3-4.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agents import mapping_agent
from api.schemas import (
    ChatHistoryResponse,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatsResponse,
    ChatSummary,
    CostOfLivingOut,
    CreatedChatResponse,
    DeletedChatResponse,
    HealthResponse,
    MappingDetailsResponse,
    MappingsResponse,
    ProgrammesResponse,
    ResearchRequest,
    UniversitiesResponse,
    UniversityBriefing,
)
from data import coursefinder_db as cf
from data import gem_client, gem_parser, host_city, wise
from data import general_questions_store as general_store
from data.destinations import countries as known_countries
from data.destinations import resolve_country, resolve_region
from graph.build_graph import arun_turn, build_graph
from graph.config import cors_allow_origins
from graph.domain import Conversion, Profile, UniversityCard
from graph.providers import provider_from_env
from graph.state import initial_state
from services import session_store, store

app = FastAPI(
    title="NTU Exchange Planner",
    version="2.0.0",
    description="Pre-exchange planning for NTU GEM Explorer and SUSEP.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

SUSEP_COURSE_LOAD = (
    "The total workload taken in the semester (including the workload in the host university) "
    "should not exceed the maximum semester academic load prescribed by the School if they "
    "were to spend their semester in NTU and not on Student Exchange Programme (SEP)."
)
SUSEP_MINIMUM_CGPA = "3.5/5"
CHAT_STREAM_TIMEOUT_SECONDS = 110.0
# The non-streaming endpoint gets the same ceiling. Without one, a lane blocked
# on an external service holds the request open for as long as that service
# takes to give up.
CHAT_TURN_TIMEOUT_SECONDS = 110.0


@app.exception_handler(StarletteHTTPException)
async def _plain_text_errors(_: Request, exc: StarletteHTTPException) -> PlainTextResponse:
    """Return errors as plain sentences.

    ``api<T>()`` in the frontend throws the raw response body as an Error and
    renders it straight into the chat, so a JSON blob would be shown to the
    student verbatim.

    Registered against Starlette's HTTPException, which FastAPI subclasses,
    so an unmatched route returns a sentence too rather than {"detail": ...}.
    """
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return PlainTextResponse(detail, status_code=exc.status_code)


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    stats = cf.db_stats()
    provider = provider_from_env()
    try:
        general_questions_chunks = int(general_store.collection().count())
    except Exception:  # noqa: BLE001 - health must still report Coursefinder status
        general_questions_chunks = None
    return HealthResponse(
        status="ok" if stats.get("available") else "degraded",
        llm_provider=provider.name,
        llm_configured=provider.available(),
        storage_backend=store.backend_name(),
        cost_of_living_excludes_rent=True,
        general_questions_chroma_chunks=general_questions_chunks,
        general_questions_chroma_path=str(general_store.general_questions_chroma_path()),
        **{k: v for k, v in stats.items() if k in HealthResponse.model_fields},
    )


@app.get("/api/programmes", response_model=ProgrammesResponse)
def programmes() -> ProgrammesResponse:
    """What the student can actually be asked to choose between."""
    try:
        return ProgrammesResponse(
            programmes=cf.all_programmes(), countries=known_countries()
        )
    except cf.CoursefinderUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/universities", response_model=UniversitiesResponse)
def universities(
    school: str = Query(description="NTU programme code, e.g. 'CSC'."),
    programme_type: str = Query(default="GEMX", description="'GEMX' or 'SUSEP'."),
    offset: int = Query(default=0, ge=0, description="Rows already shown."),
    limit: int = Query(default=cf.DEFAULT_PAGE, ge=1, le=cf.MAX_LIMIT, description="Page size."),
    country: str | None = Query(default=None, description="Optional country filter."),
    countries: list[str] | None = Query(default=None, description="Optional multi-country region filter."),
    module_codes: list[str] | None = Query(default=None, description="Only these NTU module codes."),
    exclude_module_types: list[str] | None = Query(default=None, description="Mapping types to omit."),
    include_module_types: list[str] | None = Query(default=None, description="Mapping types to keep."),
    restore_module_types: list[str] | None = Query(
        default=None,
        description="Mapping types to restore additively alongside module-code filters.",
    ),
    cgpa: float | None = Query(
        default=None, ge=0.0, le=5.0, description="Student CGPA used for eligibility filtering."
    ),
) -> UniversitiesResponse:
    """Partner universities with approved mappings for this programme.

    A region-shaped ``country`` expands to the represented countries rather
    than being treated as a literal country: "anywhere in Europe" is a
    preference, and answering it with nothing would be wrong.
    """
    if not school.strip():
        raise HTTPException(status_code=400, detail="A programme code is required.")
    try:
        resolved_country = resolve_country(country) if country else None
        region = resolve_region(country) if country else None
        # None means either a region or an unknown country. Regions are
        # expanded below; an unknown explicit country must remain a literal
        # zero-result filter, never an unrestricted one.
        if country and not resolved_country and not region:
            resolved_country = " ".join(country.split()).upper()
        requested_countries = [
            (
                resolve_country(value)
                or (" ".join(value.split()).upper() if not resolve_region(value) else None)
            )
            for value in (countries or [])
            if resolve_country(value) or (not resolve_region(value) and value.strip())
        ]
        if not resolved_country and region:
            requested_countries = region[1]
        cards, total, has_more = cf.eligible_universities(
            school_code=school.strip().upper(),
            programme_type=programme_type.strip().upper() or "GEMX",
            country=resolved_country,
            countries=requested_countries,
            # A CGPA filter runs after an external brochure check, so paging
            # must start from the same candidate pool every time and be applied
            # only once ineligible partners have been removed.
            offset=(0 if cgpa is not None else offset),
            limit=(mapping_agent.MAX_CGPA_CHECKS if cgpa is not None else limit),
            module_codes=module_codes,
            exclude_module_types=exclude_module_types,
            include_module_types=include_module_types,
            restored_module_types=restore_module_types,
        )
        notes: list[str] = []
        if cgpa is not None:
            # The same helper the graph lane uses, so "show more" cannot page
            # by a different rule than the first page was built with.
            page = mapping_agent.apply_cgpa_filter(
                cards, total, cgpa, offset=offset, limit=limit
            )
            cards, total, has_more = page.cards, page.total, page.has_more
            notes = page.caveats
    except cf.CoursefinderUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return UniversitiesResponse(
        cards=cards, has_more=has_more, total=total, offset=offset, notes=notes
    )


@app.get("/api/universities/{university_id}/mappings", response_model=MappingsResponse)
def university_mappings(
    university_id: int,
    school: str = Query(description="NTU programme code."),
    programme_type: str = Query(default="GEMX", description="'GEMX' or 'SUSEP'."),
    offset: int = Query(default=0, ge=0, description="Rows already shown."),
    limit: int = Query(default=12, ge=1, le=cf.MAX_LIMIT, description="Page size."),
    module_codes: list[str] | None = Query(default=None, description="Only these NTU module codes."),
    exclude_module_types: list[str] | None = Query(default=None, description="Mapping types to omit."),
    include_module_types: list[str] | None = Query(default=None, description="Mapping types to keep."),
    restore_module_types: list[str] | None = Query(
        default=None,
        description="Mapping types to restore additively alongside module-code filters.",
    ),
) -> MappingsResponse:
    if not school.strip():
        raise HTTPException(status_code=400, detail="A programme code is required.")
    code = school.strip().upper()
    kind = programme_type.strip().upper() or "GEMX"
    try:
        rows = cf.approved_mappings(
            university_id=university_id,
            school_code=code,
            programme_type=kind,
            offset=offset,
            limit=limit,
            module_codes=module_codes,
            exclude_module_types=exclude_module_types,
            include_module_types=include_module_types,
            restored_module_types=restore_module_types,
        )
        total = cf.approved_mapping_count(
            university_id,
            code,
            kind,
            module_codes=module_codes,
            exclude_module_types=exclude_module_types,
            include_module_types=include_module_types,
            restored_module_types=restore_module_types,
        )
    except cf.CoursefinderUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return MappingsResponse(mappings=rows, total=total, has_more=offset + len(rows) < total)


@app.get("/api/mappings/{mapping_id}/details", response_model=MappingDetailsResponse)
def mapping_details(mapping_id: int) -> MappingDetailsResponse:
    """The most recent student submission for one mapping.

    No submission on file is a normal state, not an error: it returns an empty
    object and the card shows nothing rather than an alarming message.
    """
    try:
        return MappingDetailsResponse(details=cf.mapping_details(mapping_id))
    except cf.CoursefinderUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# --- chats ----------------------------------------------------------------


@app.get("/api/chats", response_model=ChatsResponse)
def list_chats() -> ChatsResponse:
    return ChatsResponse(chats=[ChatSummary(**row) for row in session_store.list_chats()])


@app.post("/api/chats", response_model=CreatedChatResponse)
def create_chat() -> CreatedChatResponse:
    return CreatedChatResponse(session_id=session_store.create_session())


@app.get("/api/chats/{session_id}", response_model=ChatHistoryResponse)
def get_chat(session_id: str) -> ChatHistoryResponse:
    state = session_store.get_state(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="That chat no longer exists.")
    return ChatHistoryResponse(session_id=session_id, messages=state.get("messages", []))


@app.post("/api/chats/{session_id}/cancel")
async def cancel_chat_turn(session_id: str) -> dict[str, object]:
    """Stop an in-flight answer while keeping the conversation available."""
    if session_store.get_state(session_id) is None:
        raise HTTPException(status_code=404, detail="That chat no longer exists.")

    task = session_store.cancel_turn(session_id)
    if task is not None and task is not asyncio.current_task():
        try:
            # The stream handles cancellation in its finally block. Waiting
            # here closes the small race where the next turn arrives while the
            # old stream is still registered as active.
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    return {"cancelled": task is not None, "session_id": session_id}


@app.delete("/api/chats/{session_id}", response_model=DeletedChatResponse)
def delete_chat(session_id: str) -> DeletedChatResponse:
    return DeletedChatResponse(
        deleted=session_store.delete_chat(session_id), session_id=session_id
    )


@app.get("/api/general-questions/source/{source_id:path}")
def general_questions_source(source_id: str) -> PlainTextResponse:
    """Return an indexed Chroma passage for a clickable RAG citation.

    The endpoint exposes only the chunk identified by a stored vector id; it
    never reads or serves the source PDFs at runtime.
    """
    try:
        record = general_store.source_record(source_id)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="The NTU intranet reference database is unavailable."
        ) from exc
    if not record:
        raise HTTPException(status_code=404, detail="That intranet source is not available.")
    return PlainTextResponse(
        f"{record['document']} - page {record['page']}\n\n{record['text']}"
    )


# --- the planner ----------------------------------------------------------


NL = chr(10)


def _display_gpa(raw: str | None) -> str | None:
    """Show the value without repeating the field label in the UI."""
    if not raw:
        return None
    return raw.replace("Minimum CGPA:", "", 1).strip()


def _briefing_cost(name: str, country: str | None, university_id: int | None = None) -> CostOfLivingOut:
    """Return Wise's monthly estimate and distribution for the university card."""
    record = cf.university(university_id) if university_id is not None else cf.university_by_name(name, country)
    location = (record or {}).get("city_state") or None
    resolved_country = (record or {}).get("country") or country
    if not location:
        city = host_city.resolve(name, resolved_country)
        location = city.name if city else None

    cost = wise.fetch(location, resolved_country) if location else wise.fetch_country(resolved_country)
    if not cost.usable:
        cost = wise.fetch_country(resolved_country)
    if not cost.usable:
        return CostOfLivingOut(
            city=location,
            error=cost.error or "Wise has no usable cost-of-living estimate for this destination.",
            url=cost.url,
            source="Wise",
        )

    label = location or resolved_country or "the destination"
    property_expense = next(
        (expense for expense in cost.expenses if expense.label.casefold() == "property"),
        None,
    )
    excludes_rent = property_expense is not None
    monthly_total = (
        round(
            sum(
                expense.amount_sgd
                for expense in cost.expenses
                if expense is not property_expense
            ),
            2,
        )
        if excludes_rent
        else cost.monthly_sgd
    )
    return CostOfLivingOut(
        city=label,
        summary=(
            f"About S${monthly_total:,.0f} per month excluding rent in {label} "
            "."
            if excludes_rent
            else f"About S${monthly_total:,.0f} per month in {label}."
        ),
        single_person_monthly=f"S${monthly_total:,.0f}",
        monthly_total_sgd=monthly_total,
        rent_included=False if excludes_rent else None,
        items=[
            {
                "label": f"{expense.label} ({expense.percentage}%)",
                "price": f"S${expense.amount_sgd:,.0f}",
                "amount_sgd": expense.amount_sgd,
            }
            for expense in cost.expenses
        ],
        url=cost.url,
        source=f"Wise — {cost.level} data",
    )


def _carried_context(state: dict) -> dict:
    """What a follow-up turn needs from the turns before it.

    Persisted alongside the profile because a conversation is not just a
    profile: "how much does it cost there?" only means anything if the session
    remembers which university "there" was. The active term is persisted
    explicitly too, so research follow-ups can scope a question such as weather
    to the same exchange period even when the student omits it.
    """
    profile = state.get("profile")
    preferred_semester = getattr(profile, "preferred_semester", "")
    if not preferred_semester and isinstance(profile, dict):
        preferred_semester = profile.get("preferred_semester", "")
    return {
        "named_university": state.get("named_university"),
        "university_candidates": list(state.get("university_candidates") or []),
        "preferred_semester": preferred_semester or None,
        "cgpa": state.get("cgpa"),
        "budget_sgd": state.get("budget_sgd"),
    }




def _payload(
    envelope,
    session_id: str,
    messages: list[dict],
    total_universities: int = 0,
    has_more: bool = False,
) -> ChatResponse:
    """Serialize one turn into the shape ``api.ts`` declares.

    The envelope fields and the legacy flat keys are built from the same
    evidence, so they cannot disagree with each other.
    """
    cards = [UniversityCard.model_validate(card) for card in envelope.universities]
    narration = envelope.answer.summary
    if envelope.answer.body:
        narration = narration + (NL * 2) + envelope.answer.body
    if envelope.caveats:
        narration = narration + (NL * 2) + NL.join(
            f"⚠️ {c}" for c in envelope.caveats
        )

    return ChatResponse(
        session_id=session_id,
        messages=[ChatMessage(**m) for m in messages],
        answer=envelope.answer,
        sources=envelope.sources,
        confidence=envelope.confidence,
        caveats=envelope.caveats,
        intent=envelope.intent,
        planned_tasks=envelope.planned_tasks,
        universities=envelope.universities,
        workload_evidence=envelope.workload_evidence,
        finance_evidence=envelope.finance_evidence,
        eligibility_evidence=envelope.eligibility_evidence,
        budget=envelope.budget,
        budgets=envelope.budgets,
        research_findings=envelope.research_findings,
        conversion_results=envelope.conversion_results,
        errors=envelope.errors,
        clarification=envelope.clarification,
        profile=envelope.profile,
        narration=narration.strip(),
        cards=cards,
        total_universities=total_universities or len(cards),
        conversions=[Conversion.model_validate(c) for c in envelope.conversion_results],
        research=None,
        has_more=has_more,
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Run one turn of the planning graph.

    The whole payload is persisted alongside the assistant message, so
    reopening a chat restores the shortlist and its citations rather than just
    the prose.

    Async so the turn can be bounded by the same timeout the streaming
    endpoint uses. A lane waiting on Terra Dotta or Wise used to be able to
    hold this request open indefinitely.
    """
    message = (request.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Please type a question first.")

    session_id = request.session_id or session_store.create_session(
        title=session_store.title_from_message(message)
    )
    if not session_store.begin_turn(session_id):
        raise HTTPException(status_code=404, detail="That chat was deleted.")
    state = session_store.get_state(session_id) or {"messages": [], "profile": None}
    history = list(state.get("messages") or [])
    stored_profile = state.get("profile")
    profile = Profile.model_validate(stored_profile) if stored_profile else None
    context = state.get("context") or {}

    try:
        try:
            async with asyncio.timeout(CHAT_TURN_TIMEOUT_SECONDS):
                final = await arun_turn(
                    message, session_id, history=history, profile=profile, context=context
                )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail="The planner took too long to finish this question. Please try again.",
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail="The planner hit an unexpected error on that question. Try rephrasing it.",
            ) from exc

        if session_store.is_deleted(session_id):
            raise HTTPException(status_code=409, detail="That chat was deleted.")

        envelope = final.get("envelope")
        if envelope is None:
            raise HTTPException(status_code=500, detail="The planner produced no answer.")

        payload = _payload(
            envelope,
            session_id,
            [],
            total_universities=int(final.get("total_universities") or 0),
            has_more=bool(final.get("has_more")),
        )
        messages = history + [
            {"role": "user", "content": message},
            {
                "role": "assistant",
                "content": payload.narration,
                "payload": payload.model_dump(exclude={"messages", "session_id"}),
            },
        ]
        session_store.save_state(
            session_id,
            {
                "messages": messages,
                "profile": envelope.profile.model_dump() if envelope.profile else None,
                "context": _carried_context(final),
            },
            title=session_store.title_from_message(message),
        )

        payload.messages = [ChatMessage(**m) for m in messages]
        return payload
    finally:
        session_store.end_turn(session_id)


@app.post("/api/research", response_model=UniversityBriefing)
def research(request: ResearchRequest) -> UniversityBriefing:
    """The "Know more" briefing for one university (UniversityCard.tsx:96).

    Everything here comes from that university's own GEM Explorer brochure, and
    every field that cannot be read from it stays empty rather than being
    filled with a plausible number.
    """
    name = (request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A university name is required.")

    if (request.programme_type or "").strip().upper() == "SUSEP":
        return UniversityBriefing(
            name=name,
            country=request.country,
            term=request.term,
            source="SUSEP",
            course_load_raw=SUSEP_COURSE_LOAD,
            gpa=SUSEP_MINIMUM_CGPA,
            cost_of_living=None,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        brochure_future = pool.submit(gem_client.brochure_for, name, request.country)
        cost_future = pool.submit(_briefing_cost, name, request.country, request.university_id)
        result = brochure_future.result()
        cost = cost_future.result()

    url = result.program.url if result.program else ""
    workload = None
    eligibility = None
    if result.ok and result.data:
        workload, _ = gem_parser.parse_workload(result.data, name, url)
        eligibility = gem_parser.parse_cgpa(result.data, name, url)

    min_ects = max_ects = min_au = max_au = None
    course_load_raw = None
    au_note = ""
    au_summary = None

    if workload is not None:
        course_load_raw = workload.native_unit_text
        unit = (workload.unit_label or "").lower()
        if unit.startswith("ects"):
            min_ects, max_ects = workload.minimum_value, workload.maximum_value
        basis = workload.conversion_basis
        if workload.conversion_status == "supported" and basis is not None:
            # A conversion is only ever made on the university's own stated
            # basis, and the result lands in the field for the unit the basis
            # actually names. Queen's states credits-to-ECTS; writing that
            # result into the AU field would relabel one foreign unit as
            # another, which is precisely the error this system exists to stop.
            low = round(workload.minimum_value * basis.factor, 2) if workload.minimum_value is not None else None
            high = round(workload.maximum_value * basis.factor, 2) if workload.maximum_value is not None else None
            if basis.to_unit == "AU":
                min_au, max_au = low, high
            elif basis.to_unit == "ECTS":
                min_ects, max_ects = low, high
            au_note = (
                f"This brochure states its own equivalence, so the converted figure is the "
                f"university's, not an assumption: {basis.source_excerpt}"
            )
            if basis.to_unit != "AU":
                au_note += (
                    f" It converts to {basis.to_unit}, not to NTU academic units — no AU "
                    "equivalence is published."
                )
            au_summary = f"{basis.from_unit} to {basis.to_unit}, factor {basis.factor:g}."
        else:
            au_note = (
                "No AU conversion is shown. This brochure states the load in "
                f"{workload.unit_label or 'its own units'} and publishes no equivalence to NTU "
                "academic units, so converting would mean inventing a factor. Your school's "
                "exchange coordinator confirms the AU award."
            )
            au_summary = "Conversion not available."
    else:
        au_note = "This brochure does not publish a course load."

    return UniversityBriefing(
        name=name,
        country=request.country,
        gem_program=result.program.name if result.program else None,
        term=request.term,
        source="GEM Explorer" if result.ok and result.data else "Wise",
        course_load_raw=course_load_raw,
        au_summary=au_summary,
        min_ects=min_ects,
        max_ects=max_ects,
        min_au=min_au,
        max_au=max_au,
        au_note=au_note,
        planning_reference=workload.planning_reference if workload is not None else None,
        gpa=_display_gpa(eligibility.requirement if eligibility else None),
        brochure_url=url or None,
        cost_of_living=cost,
        module_conversions=[],
    )


# --- streaming ------------------------------------------------------------

# Node name -> the PlannedTask kind it reports against.
_NODE_TO_KIND = {
    "course_matching": "course_matching",
    "workload": "workload",
    "finance": "finance",
    "research": "research",
    "conversion": "conversion",
    "official_docs": "official_docs",
    "general_questions": "general_questions",
}


def _sse(event: str, data: dict) -> str:
    """One server-sent event. Newlines inside the payload would end the frame."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    """The same turn as ``/api/chat``, streamed as it happens.

    This exists because the plan is the interesting part. The router commits to
    a set of tasks *before* any lane runs, so the student watches the system
    decide what to do and then do it — which is the visible evidence that it
    plans, acts and adapts rather than emitting one blob at the end.

    Events: ``plan`` once the router has decided, ``task`` on each lane
    transition, ``done`` with the full payload, ``error`` if the turn fails.
    A client that ignores the intermediate events still gets a complete answer
    from ``done``, so streaming is an enhancement rather than a requirement.
    """
    message = (request.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Please type a question first.")

    session_id = request.session_id or session_store.create_session(
        title=session_store.title_from_message(message)
    )
    state = session_store.get_state(session_id) or {"messages": [], "profile": None}
    history = list(state.get("messages") or [])
    stored_profile = state.get("profile")
    profile = Profile.model_validate(stored_profile) if stored_profile else None
    context = state.get("context") or {}

    async def events():
        stream_task = asyncio.current_task()
        if not session_store.begin_turn(session_id, stream_task):
            deleted = session_store.is_deleted(session_id)
            yield _sse(
                "error",
                {
                    "message": (
                        "That chat was deleted."
                        if deleted
                        else "That chat is still finishing its previous response. Please try again in a moment."
                    ),
                    "detail": "deleted" if deleted else "active",
                },
            )
            return
        try:
            yield _sse("open", {"session_id": session_id})
            graph = build_graph()
            initial = initial_state(
                message, session_id, history=history, profile=profile, context=context
            )
            final: dict = {}
            announced: set[str] = set()

            async with asyncio.timeout(CHAT_STREAM_TIMEOUT_SECONDS):
                async for update in graph.astream(initial, stream_mode="updates"):
                    for node, patch in (update or {}).items():
                        if not isinstance(patch, dict):
                            continue
                        final.update(patch)

                        if node == "decompose":
                            tasks = [t.model_dump() for t in patch.get("planned_tasks") or []]
                            yield _sse("plan", {"planned_tasks": tasks, "intent": patch.get("intent")})
                            for planned_task in tasks:
                                if planned_task["status"] == "pending":
                                    yield _sse("task", {**planned_task, "status": "running"})
                                    announced.add(planned_task["kind"])
                            continue

                        kind = _NODE_TO_KIND.get(node)
                        if kind and kind in announced:
                            result = (patch.get("lane_results") or {}).get(node)
                            yield _sse(
                                "task",
                                {
                                    "kind": kind,
                                    "status": getattr(result, "status", "complete"),
                                    "description": getattr(result, "note", ""),
                                    "warnings": list(getattr(result, "warnings", []) or []),
                                },
                            )

            if session_store.is_deleted(session_id):
                return

            envelope = final.get("envelope")
            if envelope is None:
                yield _sse("error", {"message": "The planner produced no answer."})
                return

            payload = _payload(
                envelope,
                session_id,
                [],
                total_universities=int(final.get("total_universities") or 0),
                has_more=bool(final.get("has_more")),
            )
            messages = history + [
                {"role": "user", "content": message},
                {
                    "role": "assistant",
                    "content": payload.narration,
                    "payload": payload.model_dump(exclude={"messages", "session_id"}),
                },
            ]
            session_store.save_state(
                session_id,
                {
                    "messages": messages,
                    "profile": envelope.profile.model_dump() if envelope.profile else None,
                    "context": _carried_context(final),
                },
                title=session_store.title_from_message(message),
            )
            payload.messages = [ChatMessage(**m) for m in messages]
            yield _sse("done", payload.model_dump())
        except asyncio.CancelledError:
            # DELETE /api/chats/{id} cancels the active stream. Do not emit an
            # error or persist anything after cancellation.
            return
        except TimeoutError:
            yield _sse(
                "error",
                {
                    "message": "The planner took too long to finish this question. Please try again.",
                    "detail": "timeout",
                },
            )
        except Exception as exc:  # noqa: BLE001 - the stream must close cleanly
            yield _sse(
                "error",
                {
                    "message": "The planner hit an unexpected error on that question.",
                    "detail": exc.__class__.__name__,
                },
            )
        finally:
            session_store.end_turn(session_id, stream_task)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
