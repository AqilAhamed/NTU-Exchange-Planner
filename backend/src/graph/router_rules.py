"""Deterministic intent classification. No LLM, no API key, no network.

The router runs *before* any work is done, so the plan is a commitment rather
than a description of what happened to come back. The previous build inferred
intent afterwards, from whether cards had been returned, which meant a question
about safety that happened to match a university was reported as course
planning.

Rules first, model second. These patterns settle the great majority of real
questions on their own; :mod:`agents.decompose_agent` only spends an LLM call
when :func:`classify` reports ``ambiguous``, and it falls back to this result
whenever the model's answer cannot be parsed. Routing is therefore fully
unit-testable with no credentials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from graph.domain import Profile

# Lanes, in the order a student would recognise them.
COURSE_MATCHING = "course_matching"
WORKLOAD = "workload"
FINANCE = "finance"
RESEARCH = "research"
CONVERSION = "conversion"
OFFICIAL_DOCS = "official_docs"
GENERAL_QUESTIONS = "general_questions"
CLARIFICATION = "clarification"

# Intents, fixed by frontend/lib/api.ts.
CORE_PLANNING = "core_planning"
MIXED = "mixed"
UNKNOWN = "unknown"

DETERMINISTIC_LANES = frozenset({COURSE_MATCHING, WORKLOAD, FINANCE, CONVERSION})

# Every lane that has a node in the graph. ``clarification`` is deliberately
# absent: it is an outcome, not a lane that runs.
ALL_LANES = frozenset(
    DETERMINISTIC_LANES | {RESEARCH, OFFICIAL_DOCS, GENERAL_QUESTIONS}
)


def _p(*alternatives: str) -> re.Pattern[str]:
    return re.compile("|".join(alternatives), re.IGNORECASE)


# NTU's own rules and paperwork. This lane is deliberately disabled — the
# authoritative PDFs are not a source we are licensed to reproduce — so it
# returns a structured "unavailable" and points the student at the office.
OFFICIAL_PATTERNS = _p(
    r"\bvisa\b", r"\bimmigration\b", r"\bwork permit\b", r"\binsurance\b",
    r"\bntu (?:policy|policies|rules|regulation)", r"\bleave of absence\b",
    r"\btranscript\b", r"\bcredit transfer polic", r"\bapplication deadline\b",
    r"\bhow do i apply\b", r"\bwhen (?:do|can) i apply\b", r"\bscholarship\b",
    r"\bstudent pass\b", r"\bexchange agreement\b",
)

# An explicit request to convert one unit into another. Distinguished from a
# workload question by requiring a conversion verb or an X-to-Y construction:
# "how many credits do I need" is workload, "how many AU is 30 ECTS" is not.
CONVERSION_PATTERNS = _p(
    r"\bconvert(?:ed|ing|s)?\b", r"\bconversion\b",
    r"\b\d+(?:\.\d+)?\s*(?:ects|au|credits?|mcs?)\b.{0,24}\b(?:in|to|into|=|equals?)\b",
    r"\b(?:in|to|into)\s+(?:ects|aus?|academic units?|sgd|singapore dollars?)\b",
    r"\bexchange rate\b", r"\bequivalent (?:of|to|in)\b", r"\bhow much is .{0,20}\bin sgd\b",
    # "how many AU is 30 ECTS" — a unit named up front with a number behind it.
    # "how many credits do I need" has no number and stays a workload question.
    r"\bhow many\s+(?:ects|aus?|academic units?|credits?|mcs?)\b.{0,20}\d",
)

# What the host university requires you to take, in its own units.
WORKLOAD_PATTERNS = _p(
    r"\bworkload\b", r"\bcourse ?load\b", r"\bstudy load\b", r"\bfull[- ]?time\b",
    r"\bc?gpa\b", r"\bgrade\s+point\s+average\b", r"\bacademic\s+average\b",
    r"\bhow many (?:courses?|modules?|classes|subjects|credits?|ects)\b",
    r"\b(?:minimum|maximum|min|max)\s+(?:number of\s+)?(?:credits?|ects|courses?|modules?)\b",
    r"\bcredits? (?:required|needed|per semester)\b", r"\bhow many .{0,16}can i take\b",
)

# Money.
FINANCE_PATTERNS = _p(
    r"\bcosts?\b", r"\bcost of living\b", r"\bbudget\b", r"\bafford\b", r"\bexpensive\b",
    r"\bcheap(?:er|est)?\b", r"\bprices?\b", r"\brent\b", r"\baccommodation cost",
    r"\bliving expenses?\b", r"\bhow much (?:does|will|do|would|is|are)\b",
    r"\bper month\b", r"\bmonthly\b", r"\bspend(?:ing)?\b", r"\bmoney\b",
)

# The finance lane is also used by shortlist budget constraints. The Wise
# cost-of-living lookup itself is narrower: it must run only when the student
# asks for a destination's living cost/budget, not merely because a planning
# sentence contains a number or the word "monthly".
COST_OF_LIVING_PATTERNS = _p(
    r"\bcost of living\b",
    r"\bliving costs?\b",
    r"\bhow much\b[^.;!?]{0,60}\b(?:cost|budget|expense|rent)\b",
    r"\b(?:cost|expenses?|rent)\b[^.;!?]{0,45}\b(?:at|in|for)\b",
    r"\b(?:monthly|per\s+month)\b[^.;!?]{0,35}\b(?:cost|expenses?|rent)\b",
    r"\bbudget\b[^.;!?]{0,45}\b(?:at|in|for)\b",
    r"\b(?:budget|cost|expenses?|rent)\b[^.;!?]{0,24}\b(?:needed|required)\b",
    r"\bhow much\b[^.;!?]{0,60}\b(?:spend|spending|money)\b",
    r"\b(?:spend|spending|money)\b[^.;!?]{0,45}\b(?:at|in|for)\b",
)


def is_cost_request(message: str) -> bool:
    """Whether the wording explicitly requests a destination cost estimate."""
    text = message or ""
    if not COST_OF_LIVING_PATTERNS.search(text):
        return False
    # A cost phrase without a destination should not silently cost the first
    # shortlist university or turn a generic monthly-budget statement into a
    # Wise lookup. A destination may be a university fragment or a recognised
    # country, and can appear before or after the cost wording.
    from data.coursefinder_db import match_universities
    from data.destinations import resolve_country

    return bool(match_universities(text) or resolve_country(text))

# Qualitative questions that no database can answer.
RESEARCH_PATTERNS = _p(
    r"\bsafe(?:ty)?\b", r"\bcrime\b", r"\bdangerous\b",
    r"\bhalal\b", r"\bvegetarian\b", r"\bvegan\b", r"\bfood\b", r"\bcuisine\b",
    r"\bculture\b", r"\bcultural\b", r"\bweather\b", r"\bclimate\b", r"\bcold\b",
    r"\borientation\b", r"\barrival\b", r"\bfirst\s+class\b", r"\blast\s+class\b",
    r"\bexam(?:ination)?(?:s)?(?:\s+period)?\b", r"\bprogramme\s+dates?\b",
    r"\bacademic\s+calendar\b", r"\bterm\s+dates?\b",
    r"\b(?:when|what time)\b[^.;!?]{0,35}\b(?:semester|sem\s*[12]|fall|spring)\b",
    r"\b(?:semester\s*[12]|sem\s*[12]|fall|spring)\b[^.;!?]{0,35}"
    r"\b(?:start|begin|end|finish|dates?)\b",
    r"\b(?:what|which)\b[^.;!?]{0,35}\bdates?\b[^.;!?]{0,25}"
    r"\b(?:semester\s*[12]|sem\s*[12]|fall|spring)\b",
    r"\b(?:what|which)\b[^.;!?]{0,30}\b(?:period|months?)\b[^.;!?]{0,30}"
    r"\b(?:semester\s*[12]|sem\s*[12]|fall|spring)\b",
    r"\b(?:semester\s*[12]|sem\s*[12]|fall|spring)\b[^.;!?]{0,30}"
    r"\b(?:period|months?)\b",
    r"\bnightlife\b", r"\bsocial life\b", r"\bstudent life\b", r"\bneighbou?rhood\b",
    r"\bvibe\b", r"\bwhat(?:'s| is) it like\b", r"\bworth it\b", r"\breviews?\b",
    r"\breddit\b", r"\bexperiences?\b", r"\blanguage barrier\b", r"\benglish[- ]taught\b",
    r"\bracism\b", r"\bdiscriminat", r"\bhealthcare\b", r"\bhospital\b",
    r"\bbetter (?:fit|choice|option)\b", r"\bwhich is better\b", r"\bbest\b", r"\bcompare\b",
    r"\bversus\b", r"\bvs\.?\b", r"\brecommend\b", r"\bshould i (?:pick|choose|go)\b",
    # Questions about a host's student community and everyday experience are
    # qualitative research, even when they do not use words such as "culture"
    # or "diversity". Keep these shape-based so the rule works for any
    # nationality, university or phrasing.
    r"\b(?:people|students?|population|demograph(?:y|ic)|divers(?:e|ity)|"
    r"nationalit(?:y|ies)|ethnic(?:ity|ities)|communities?)\b",
    r"\b(?:clubs?|societies|activities|facilities|campus|libraries?|"
    r"housing|dorm(?:s|itory)?|transport|location|city)\b",
    r"\b(?:winter|snow(?:y|fall)?|summer|sunny|seasonal)\b",
)

# Weather is special-cased when it is asked directly about one named host.
# Without this narrower pattern, UNIVERSITY_DETAIL_PATTERNS treats
# "what is the weather like at UC3M?" as a full university briefing because
# of the words "what ... like", adding workload and finance work the student
# never requested.
WEATHER_PATTERNS = _p(
    r"\bweather\b", r"\bclimate\b", r"\b(?:how|is it)\s+(?:cold|hot)\b",
    r"\b(?:rainy|snowy|sunny|seasonal)\b",
)

# Planning the shortlist itself.
PLANNING_PATTERNS = _p(
    r"\buniversit(?:y|ies)\b", r"\bunis?\b", r"\bpartners?\b", r"\bschools?\b", r"\bmodules?\b",
    r"\bcourses?\b", r"\bmapp?(?:ed|ing|ings)\b", r"\bshortlist\b", r"\boptions\b",
    r"\bwhere can i go\b", r"\bexchange\b", r"\bgem\b", r"\bsusep\b",
    r"\bplan(?:ning)?\b", r"\bapproved\b", r"\bdestinations?\b", r"\bmatch(?:es|ing)?\b",
)

# The broad planning vocabulary above is useful for ordinary shortlist
# requests, but words such as "university" can also appear only as part of a
# named host ("University of Tokyo"). These are the shapes that explicitly
# ask for planning work and therefore should disqualify a weather-only route.
EXPLICIT_PLANNING_PATTERNS = _p(
    r"\b(?:find|show|list|recommend|suggest|shortlist)\b",
    r"\bwhere\s+can\s+i\s+go\b",
    r"\b(?:which|what)\s+(?:universit(?:y|ies)|unis?|partners?|options?|modules?|courses?)\b",
    r"\b(?:approved|mapped|mapping|module matching)\b",
)

# Constraints can be the whole request and need the course-matching lane even
# when the student does not repeat the word "university" ("leave out BDEs",
# "only show SC4000"). The patterns identify a *shape*, while intake parses
# the actual values and the database validates the resulting query.
CONSTRAINT_PATTERNS = _p(
    r"\b(?:ignore|exclude|excluding|omit|without|leave\s+out|drop|remove|"
    r"don't\s+show|do\s+not\s+show|only\s+show|limited\s+to|restrict(?:ed)?\s+to)\b",
    r"\b[A-Za-z]{2,5}\s*[- ]?\d{3,5}[A-Za-z]?\b",
    r"\b(?:anywhere|somewhere|in|from)\s+(?:in\s+)?(?:Europe|European|Asia|Asian|EU|"
    r"Scandinavia|Nordics?|North America|Oceania|Africa|Americas?)\b",
    r"\b(?:budget|spend(?:ing)?|afford|under|up\s+to|max(?:imum)?|only)\b"
    r"[^.;,!?]{0,28}(?:\$|s\$|sgd|dollars?|monthly|per\s+month)\s*\d",
    r"(?:\$|s\$|sgd)\s*\d[^.;,!?]{0,24}\b(?:only|maximum|max|per\s+month|monthly)\b",
)

# "Tell me more about UC3M" is a request for a briefing on ONE university, not
# for a shortlist. Without this the router falls through to the
# complete-profile default and answers a question about one place with six
# other places, which is what a student notices immediately.
UNIVERSITY_DETAIL_PATTERNS = _p(
    r"\btell me (?:more )?(?:about )?",
    r"\b(?:know |find out )?more (?:about|on)\b",
    r"\bdetails? (?:about|on|for)\b",
    r"\binfo(?:rmation)? (?:about|on)\b",
    r"\bwhat(?:'s| is) it like\b",
    r"\bwhat(?:'s| is) ([A-Z][\w]+ )?.{0,30}like\b",
    r"\btell me about\b",
)

# Everything a briefing on one university is made of.
DETAIL_LANES = [WORKLOAD, FINANCE, RESEARCH]

# A research criterion can apply to a shortlist rather than to one named
# university: "which partners have halal options?", "which uni is best for
# research?", or "where can I go to experience snow?". Those requests need
# Coursefinder's eligible candidates before the web search can be meaningful.
CANDIDATE_SCOPE_PATTERNS = _p(
    r"\bwhere\s+can\s+i\s+go\b",
    r"\b(?:which|what|find|show|list|recommend|suggest)\b[^.;!?]{0,100}"
    r"\b(?:universit(?:y|ies)|unis?|partners?|schools?|options?|places?)\b",
    r"\b(?:universit(?:y|ies)|unis?|partners?|schools?|options?|places?)\b"
    r"[^.;!?]{0,80}\b(?:with|that|which|likely|best|offer|have)\b",
)

# A follow-up that only makes sense against the previous turn.
FOLLOW_UP_PATTERNS = _p(
    r"^\s*(?:and |but |what about |how about |ok |okay )",
    r"\b(?:there|that one|it|them|those|this one)\b",
    r"^\s*(?:why|how|what) (?:about|come)\b",
)

# A student does GEM Explorer or SUSEP, never both. That exclusivity is not a
# guess: it is the shape of the data model itself, where programme_type is a
# single value per mapping. The system knows this one, so it answers it.
BOTH_PROGRAMMES_PATTERNS = _p(
    r"\b(?:gem ?x?|gem explorer)\b.{0,40}\bsusep\b",
    r"\bsusep\b.{0,40}\b(?:gem ?x?|gem explorer)\b",
    r"\bboth (?:programmes|programs|exchanges)\b",
    r"\bdo both\b",
)

GEM_OR_SUSEP_ANSWER = (
    "No - you do one or the other. GEM Explorer (overseas) and SUSEP (within Singapore) "
    "are alternative exchange programmes, and every module mapping in Coursefinder belongs "
    "to exactly one of them, so a semester spent on one is not a semester spent on the "
    "other. Pick the programme first, and I will shortlist against it. Confirm the specifics "
    "with NTU Global Education & Mobility before you apply."
)

GREETING_PATTERNS = _p(r"^\s*(?:hi|hey|hello|yo|sup|good (?:morning|afternoon|evening))\b\W*$")

# NTU-student process and policy topics covered by the supplied intranet
# dataset. These are topic families, not exact questions, so natural wording
# such as "how do I submit my courses?" and "what happens after nomination?"
# reaches the same retrieval lane.
GENERAL_TOPIC_PATTERNS = _p(
    r"\beligib(?:le|ility)\b", r"\bqualif(?:y|ied|ication)\b",
    r"\bhow (?:do|can|to) (?:i|we) apply\b", r"\bapplication(?:s)?\b",
    r"\bapply(?:ing)?\b", r"\bnomination\b", r"\bacceptance letter\b",
    r"\bletter of participation\b|\blop\b", r"\bwithdraw(?:al|ing)?\b",
    r"\bparticipat(?:e|es|ed|ing|ion)\b", r"\b(?:allowed|permitted)\b",
    r"\b(?:before|after)\b",
    r"\bcredit transfer\b", r"\btranscript(?:s)?\b", r"\bre-?enrol(?:ment)?\b",
    r"\bpre[- ]?departure\b", r"\bpassport\b", r"\bvisa\b", r"\bimmigration\b",
    r"\binsurance\b", r"\brecruitment\b", r"\ballocation\b", r"\baccept(?:ed|ance)?\b",
    r"\borientation\b", r"\bcourse matching\b", r"\bstudent resources?\b",
    r"\bhow many rounds?\b", r"\bdeadline(?:s)?\b", r"\bapplication period\b",
    r"\bminimum (?:c?gpa|gpa)\b",
    r"\bfinancial aid\b|\bfinancial assistance\b|\bfunding\b|\bscholarships?\b",
    r"\btravel grants?\b|\bgrants?\b|\bbursar(?:y|ies)\b",
    r"\btuition fees?\b|\btuition\b[^.;!?]{0,30}\bfees?\b|\bpay\b[^.;!?]{0,30}\btuition\b",
)
GENERAL_PROGRAM_PATTERNS = _p(
    r"\bgem\s*(?:explorer|mobility|portal|x)?\b", r"\bsusep\b",
    r"\bntu\b", r"\bogem\b", r"\bglobal education and mobility\b",
    r"\bexchange\b", r"\bhost university\b",
    r"\bpartner universit(?:y|ies)\b",
)
GENERAL_DEFINITION_PATTERNS = _p(
    r"\bwhat(?:'s| is)\b", r"\bwhat does .* mean\b", r"\bhow does .* work\b",
    r"\btell me (?:more )?about\b", r"\bexplain\b",
)
GENERAL_PREPARATION_PATTERNS = _p(
    r"\bpre-?departure\b", r"\bprepare(?:d|s|ing|ation)?\b", r"\bpreparations?\b",
    r"\bchecklist\b", r"\bpacking\b", r"\bpaperwork\b", r"\bdocuments?\b",
    r"\b(?:before|prior to)\b[^.;!?]{0,20}\b(?:go|going|leave|leaving|depart|departure|travel)\b",
    r"\bwhat (?:do|should|must|will) i need to\b[^.;!?]{0,30}\b(?:prepare|bring|get|submit|do)\b",
    r"\bwhat should i do before\b", r"\bthings to\b[^.;!?]{0,20}\b(?:prepare|bring|do)\b",
)
GENERAL_APPLICATION_PATTERNS = _p(
    r"\bhow (?:do|can) (?:i|we) apply\b", r"\bhow to apply\b",
    r"\bapplication (?:process|steps?|procedure)\b",
    r"\b(?:can|could|am) i apply\b",
)

# A numeric GPA stated by the student is an eligibility constraint for the
# shortlist. It is different from asking for a host university's published
# minimum CGPA, which belongs to the workload/eligibility evidence lane. Keep
# this as a shape-based rule so any value and natural wording are supported.
STUDENT_CGPA_VALUE_PATTERN = _p(
    r"\b(?:my|i\s+have|i've\s+got|i\s+got)\b[^.;!?]{0,24}"
    r"\b(?:c?gpa|grade\s+point\s+average)\b[^.;!?]{0,16}"
    r"\b\d(?:\.\d+)?\b",
    r"\b(?:c?gpa|grade\s+point\s+average)\s*(?:of|is|=|:)\s*\d(?:\.\d+)?\b",
    r"\b(?:a\s+)?\d(?:\.\d+)?\s*(?:c?gpa|grade\s+point\s+average)\b",
)

CGPA_APPLICATION_QUESTION_PATTERN = _p(
    r"\b(?:can|could|may|am)\s+i\b[^.;!?]{0,45}"
    r"\b(?:apply|eligible|qualify|participate)\b",
    r"\b(?:apply|eligible|qualify|participate)\b[^.;!?]{0,45}"
    r"\b(?:with|given|having)\b[^.;!?]{0,20}"
    r"\b(?:c?gpa|grade\s+point\s+average)\b",
)

CGPA_SHORTLIST_PATTERN = _p(
    r"\b(?:which|what|show|find|list|filter|shortlist)\b[^.;!?]{0,55}"
    r"\b(?:universit(?:y|ies)|partners?|options?|destinations?)\b",
    r"\b(?:universit(?:y|ies)|partners?|options?|destinations?)\b[^.;!?]{0,55}"
    r"\b(?:with|using|under|above|below)\b[^.;!?]{0,20}"
    r"\b(?:c?gpa|grade\s+point\s+average)\b",
)

CORRECTION_PATTERN = _p(
    r"^\s*(?:no|nope|nah|not quite|that's not what i meant|that is not what i meant)\b",
    r"\b(?:i meant|what i meant was|instead(?:,|\s+ i)?|rather(?:,|\s+ i)?)\b",
)


def routing_message(message: str) -> tuple[str, bool]:
    """Use the corrected clause as the active routing request when present."""
    text = (message or "").strip()
    if not text or not CORRECTION_PATTERN.search(text):
        return text, False
    match = re.search(
        r"\b(?:i meant|what i meant was)\b\s*(?:(?:that|to)\s+)?(.+)$",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if match and match.group(1).strip():
        return match.group(1).strip(), True
    # If the student says "No, instead show Europe", keep the request after
    # the correction marker and remove only the conversational preface.
    match = re.search(r"\b(?:instead|rather)\b\s*(.+)$", text, re.IGNORECASE | re.DOTALL)
    if match and match.group(1).strip():
        return match.group(1).strip(), True
    return text, True


def is_general_question(
    message: str,
    named_university: str | None = None,
    has_prior_results: bool = False,
) -> bool:
    """Whether a message belongs to the NTU-student intranet RAG lane.

    A host university referent without an explicit NTU/GEM/SUSEP context is
    kept in the host-research lane. For example, "what is orientation there?"
    remains a question about that university, while "are GEM Explorer students
    required to attend orientation?" uses the NTU guidance corpus.
    """
    text = (message or "").strip()
    if not text:
        return False
    has_program_context = bool(GENERAL_PROGRAM_PATTERNS.search(text))
    has_definition = bool(GENERAL_DEFINITION_PATTERNS.search(text))
    has_topic = bool(GENERAL_TOPIC_PATTERNS.search(text))
    has_preparation = bool(GENERAL_PREPARATION_PATTERNS.search(text))
    if not has_topic and not (has_definition and has_program_context) and not has_preparation:
        return False
    if named_university and not has_program_context:
        return False
    if has_program_context:
        return True
    if (
        has_prior_results
        and has_topic
        and not FINANCE_PATTERNS.search(text)
        and (
            GENERAL_DEFINITION_PATTERNS.search(text)
            or re.match(r"^\s*(?:and|also|what about|how about|there|that|those|these)\b", text, re.IGNORECASE)
        )
    ):
        # Short follow-ups such as "what about eligibility?" omit GEM/NTU
        # because the preceding answer already established the subject. Keep
        # them in the same corpus lane instead of treating them as a fresh
        # shortlist request.
        return True
    # This app is specifically an NTU exchange assistant. A standalone
    # preparation or application question is therefore about the exchange
    # process unless the student explicitly names a host university (handled
    # above). Do not send it to the disabled official-documents lane or block
    # it behind the shortlist profile gate.
    if has_preparation or GENERAL_APPLICATION_PATTERNS.search(text):
        return True
    # Standalone process terms that are distinctive in the NTU corpus are
    # useful even when the user omits the programme name.
    return bool(
        re.search(
            r"\b(?:credit transfer|transcript|withdrawal|pre[- ]?departure|nomination|"
            r"acceptance letter|letter of participation|student resources?)\b",
            text,
            re.IGNORECASE,
        )
        or re.search(
            r"\b(?:financial aid|financial assistance|funding|scholarships?|travel grants?|"
            r"grants?|bursar(?:y|ies)|tuition fees?|tuition support)\b",
            text,
            re.IGNORECASE,
        )
        or (has_definition and re.search(r"\b(?:susep|gem)\b", text, re.IGNORECASE))
    )


def needs_candidate_research(
    message: str, lanes: list[str] | tuple[str, ...], named_university: str | None = None
) -> bool:
    """Whether research must run after Coursefinder supplies candidate cards.

    Research about one named host can run directly. Research used as a
    selection criterion needs a candidate set first; otherwise a generic web
    search can recommend universities NTU does not offer for the student's
    programme and term.
    """
    if named_university:
        return False
    active = set(lanes)
    return (
        COURSE_MATCHING in active
        and RESEARCH in active
        and bool(CANDIDATE_SCOPE_PATTERNS.search(message or ""))
    )


@dataclass
class RouteDecision:
    """What the router committed to, and why."""

    intent: str = UNKNOWN
    lanes: list[str] = field(default_factory=list)
    ambiguous: bool = False
    matched: dict[str, bool] = field(default_factory=dict)
    reason: str = ""

    @property
    def is_greeting(self) -> bool:
        return self.reason == "greeting"


def _intent_for(lanes: list[str]) -> str:
    """Derive the api.ts intent from the lanes that will actually run."""
    lane_set = set(lanes)
    if not lane_set:
        return UNKNOWN
    if lane_set == {GENERAL_QUESTIONS}:
        return GENERAL_QUESTIONS
    if lane_set == {OFFICIAL_DOCS}:
        return OFFICIAL_DOCS
    has_research = RESEARCH in lane_set
    has_deterministic = bool(lane_set & DETERMINISTIC_LANES)
    if has_research and has_deterministic:
        return MIXED
    if has_research:
        return RESEARCH
    if OFFICIAL_DOCS in lane_set and has_deterministic:
        return MIXED
    return CORE_PLANNING


def classify(
    message: str,
    profile: Profile | None = None,
    named_university: str | None = None,
    has_prior_results: bool = False,
    profile_from_message: bool = False,
) -> RouteDecision:
    """Route a message to lanes without calling a model.

    Precedence matters and is deliberate:

    1. **Official docs** first — a visa question must not be answered with a
       module shortlist just because it mentions a university.
    2. **Conversion** next, but only on an explicit conversion construction.
    3. Then workload, finance and research, which are not mutually exclusive.
    4. Course matching last, as the default for anything that reads like
       planning or that arrives with a complete profile and no other signal.
    """
    text = (message or "").strip()
    profile = profile or Profile()

    if not text:
        return RouteDecision(intent=UNKNOWN, lanes=[], ambiguous=True, reason="empty")

    if GREETING_PATTERNS.search(text):
        return RouteDecision(intent=UNKNOWN, lanes=[], ambiguous=False, reason="greeting")

    text, corrected = routing_message(text)

    # "My GPA is X; can I apply?" is a shortlist eligibility filter, not a
    # request to read a generic course-load page. Handle it before the broad
    # general-question and workload patterns so the profile gate and the
    # Coursefinder/GEM CGPA verification can do their work.
    if STUDENT_CGPA_VALUE_PATTERN.search(text):
        if (
            CGPA_APPLICATION_QUESTION_PATTERN.search(text)
            and not CGPA_SHORTLIST_PATTERN.search(text)
        ):
            lanes = [GENERAL_QUESTIONS]
            if named_university:
                # A named host adds a second, complementary check for that
                # university's published requirement; the NTU process answer
                # still comes from the general-questions corpus.
                lanes.append(WORKLOAD)
            return RouteDecision(
                intent=(MIXED if named_university else GENERAL_QUESTIONS),
                lanes=lanes,
                ambiguous=False,
                reason="cgpa_policy_question" + ("_after_correction" if corrected else ""),
            )
        if named_university and WORKLOAD_PATTERNS.search(text):
            # The GPA is context for the host-specific workload/eligibility
            # check. It must not replace the question the student actually
            # asked with a new shortlist request.
            return RouteDecision(
                intent=CORE_PLANNING,
                lanes=[WORKLOAD],
                ambiguous=False,
                reason="host_workload_with_student_cgpa"
                + ("_after_correction" if corrected else ""),
            )
        return RouteDecision(
            intent=CORE_PLANNING,
            lanes=[COURSE_MATCHING],
            ambiguous=False,
            reason="cgpa_constraint" + ("_after_correction" if corrected else ""),
        )

    # NTU-student process/policy questions are answered from the supplied
    # intranet corpus, before host-university detail or open-web routing gets a
    # chance to reinterpret them.
    if is_general_question(
        text,
        named_university=named_university,
        has_prior_results=has_prior_results,
    ):
        return RouteDecision(
            intent=GENERAL_QUESTIONS,
            lanes=[GENERAL_QUESTIONS],
            ambiguous=False,
            reason="ntu_student_reference" + ("_after_correction" if corrected else ""),
        )

    # A direct weather question about one named host is one research task, not
    # a broad university briefing. Keep this before UNIVERSITY_DETAIL_PATTERNS
    # because "what is the weather like" matches that broad conversational
    # shape as well. If the student explicitly asks for cost, workload, or a
    # conversion in the same message, the normal multi-lane route still wins.
    if named_university and WEATHER_PATTERNS.search(text) and not any(
        pattern.search(text)
        for pattern in (
            OFFICIAL_PATTERNS,
            CONVERSION_PATTERNS,
            WORKLOAD_PATTERNS,
            FINANCE_PATTERNS,
            EXPLICIT_PLANNING_PATTERNS,
            CONSTRAINT_PATTERNS,
        )
    ):
        return RouteDecision(
            intent=RESEARCH,
            lanes=[RESEARCH],
            ambiguous=False,
            reason="direct_weather" + ("_after_correction" if corrected else ""),
        )

    # A briefing on one named university: workload, cost and the qualitative
    # side, but explicitly NOT a shortlist. The student named a place; answering
    # with six others is the wrong answer to the question asked.
    if named_university and UNIVERSITY_DETAIL_PATTERNS.search(text):
        detail_lanes = list(DETAIL_LANES)
        # A broad Singapore-university briefing should not silently trigger a
        # Wise budget. Cost is opt-in for local partners; retain finance only
        # when the current message actually asks about cost or affordability.
        if profile.programme_type == "SUSEP" and not FINANCE_PATTERNS.search(text):
            detail_lanes = [lane for lane in detail_lanes if lane != FINANCE]
        return RouteDecision(
            intent=_intent_for(detail_lanes),
            lanes=detail_lanes,
            ambiguous=False,
            reason="university_detail",
        )

    # A question the system can answer outright, with no lane and no shortlist.
    if BOTH_PROGRAMMES_PATTERNS.search(text):
        return RouteDecision(
            intent=CORE_PLANNING, lanes=[], ambiguous=False, reason="gem_or_susep"
        )

    matched = {
        OFFICIAL_DOCS: bool(OFFICIAL_PATTERNS.search(text)),
        CONVERSION: bool(CONVERSION_PATTERNS.search(text)),
        WORKLOAD: bool(WORKLOAD_PATTERNS.search(text)),
        FINANCE: bool(FINANCE_PATTERNS.search(text)),
        RESEARCH: bool(RESEARCH_PATTERNS.search(text)),
        COURSE_MATCHING: bool(
            PLANNING_PATTERNS.search(text) or CONSTRAINT_PATTERNS.search(text)
        ),
    }

    lanes: list[str] = []

    if matched[OFFICIAL_DOCS]:
        lanes.append(OFFICIAL_DOCS)

    # A workload question phrased with "how many credits" also trips the
    # conversion pattern. Workload wins: the student wants the host's rule, not
    # arithmetic.
    if matched[CONVERSION] and not matched[WORKLOAD]:
        lanes.append(CONVERSION)

    if matched[WORKLOAD]:
        lanes.append(WORKLOAD)
    if matched[FINANCE]:
        lanes.append(FINANCE)
    if matched[RESEARCH]:
        lanes.append(RESEARCH)

    if matched[COURSE_MATCHING] and not matched[OFFICIAL_DOCS]:
        # "what's the course load at Akita" is one question about one place:
        # the word "course" there belongs to "course load", not to a request
        # for a shortlist the student already has.
        already_answered = WORKLOAD in lanes and bool(named_university)
        if not already_answered:
            lanes.insert(0, COURSE_MATCHING)

    # A follow-up about a university already on the table is planning, not
    # noise. Checked before the profile default so the reason stays honest.
    if not lanes and (named_university or has_prior_results) and FOLLOW_UP_PATTERNS.search(text):
        return RouteDecision(
            intent=CORE_PLANNING, lanes=[COURSE_MATCHING], ambiguous=False, matched=matched,
            reason="follow_up",
        )

    # A stated programme and term with nothing else in the message is a request
    # for the shortlist, even without a planning keyword: "CSC, semester 1".
    #
    # But only when *this* message is what supplied them. If the profile came
    # from an earlier turn and this message says something the rules do not
    # recognise, that is genuine ambiguity and must escalate - otherwise
    # unrecognised text silently becomes a shortlist request, which is the old
    # "infer the intent from whatever came back" defect wearing a new hat.
    if not lanes and profile.school_code and profile.preferred_semester:
        return RouteDecision(
            intent=CORE_PLANNING,
            lanes=[COURSE_MATCHING],
            ambiguous=not profile_from_message,
            matched=matched,
            reason="complete_profile_default",
        )

    if not lanes:
        return RouteDecision(
            intent=UNKNOWN, lanes=[], ambiguous=True, matched=matched, reason="no_rule_matched"
        )

    return RouteDecision(
        intent=_intent_for(lanes), lanes=lanes, ambiguous=False, matched=matched,
        reason="rules",
    )


def describe(lane: str, profile: Profile | None = None) -> str:
    """One line of plan text, in the student's language, for a lane."""
    programme = (profile.school_name or profile.school_code) if profile else ""
    where = f" for {programme}" if programme else ""
    course_description = f"Find partner universities with approved module mappings{where}"
    if profile:
        if profile.destination_region:
            course_description += f" in {profile.destination_region}"
        elif profile.destination_pref:
            course_description += f" in {profile.destination_pref.title()}"
        if profile.module_codes:
            course_description += f" for {', '.join(profile.module_codes)}"
        if profile.included_module_types:
            course_description += f" limited to {', '.join(profile.included_module_types)}"
        if profile.restored_module_types:
            alongside = " alongside the requested modules" if profile.module_codes else ""
            course_description += (
                f" including {', '.join(profile.restored_module_types)} mappings"
                + alongside
            )
        if profile.excluded_module_types:
            course_description += f" excluding {', '.join(profile.excluded_module_types)}"
    return {
        COURSE_MATCHING: course_description,
        WORKLOAD: "Read each host university's own course-load rules",
        FINANCE: (
            "Estimate a monthly budget from published figures"
            + (
                f" and compare it with the S${profile.max_monthly_budget_sgd:,.0f} limit"
                if profile and profile.max_monthly_budget_sgd is not None
                else ""
            )
        ),
        RESEARCH: "Research the qualitative question and cite the sources",
        CONVERSION: "Work out the conversion you asked for",
        OFFICIAL_DOCS: "Check NTU's own policy documents",
        GENERAL_QUESTIONS: "Retrieve the answer from NTU's GEM Explorer and SUSEP student reference documents",
        CLARIFICATION: "Ask for the one or two details needed to answer",
    }.get(lane, lane)
