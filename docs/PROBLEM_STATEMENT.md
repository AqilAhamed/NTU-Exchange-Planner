# Problem statement and agentic justification

> Written first, deliberately. The briefing's canonical example of a *failed*
> problem statement is, verbatim: **"Students need an AI chatbot for course
> advice."** That is the shape this project would take by default, and it is
> simultaneously exposed to "The Solved Problem". Getting the framing right is
> 20% of the score, and it also decides what the demo shows.

---

## The POV

> **An NTU undergraduate planning a GEM Explorer exchange needs a way to compare
> host universities on the things that actually decide the semester — which of
> their modules are already approved, how many courses they will be required to
> take, and what it will cost — because that information exists, but it is
> scattered across 44,004 Coursefinder mapping records and 385 separate GEM
> Explorer brochures that each state their course load in a different unit.**

**Figures, with sources and dates.** All queried directly on 2 September 2026:

| Figure | Value | Source |
|---|---|---|
| Partner universities in Coursefinder | **558** | `coursefinder.db`, `universities` table |
| Module mapping records | **44,004** | `mappings` table |
| Of which approved | **37,467** | `status = 'Approved'` |
| NTU degree programmes covered | **68** | `school_programmes` table |
| Student submission detail fields | **590,365** | `submission_fields` table |
| GEM Explorer programmes listed | **558**, of which **385** are GEM Explorer | `ntu-sa.terradotta.com` search index |
| Coursefinder universities matched to a GEM listing | **525 of 558** (94%) | measured, this build |

## What this is *not*

It is not a chatbot for course advice. It gives no advice. It does not recommend
a university, does not rank by "fit", and does not tell a student what to choose.
It assembles **evidence that already exists** into one comparable view, and it
attaches a source to every claim so the student can check it.

The distinction is not rhetorical. It is visible in the code: `render_agent` is
the only node that can produce an answer, and it drops any source the lane was
not permitted to emit. There is no path by which the system's opinion reaches
the student.

The same evidence-first boundary now covers NTU-student support questions.
Questions about applications, financial aid, funding, tuition, nominations,
withdrawal and credit transfer are routed to a dedicated RAG lane. The supplied
NTU intranet PDFs are ingested offline into a project-local ChromaDB collection;
the running agent retrieves indexed passages and never reads the PDF directory.
This keeps student-specific guidance separate from public host-university
research and provides a clean path to package the vector store for Bedrock.

The distributed project copy contains the populated ChromaDB collection and its
matching local embedding model, so the PDF dataset is not a runtime
requirement. It is retained only as an offline re-ingestion source.

The planner is also conversational rather than turn-isolated. It persists the
student profile, active filters, selected university and previous result cards,
then interprets follow-ups as changes or additions to that state. A request to
broaden a region can therefore keep module, GPA, budget and mapping-type
constraints while releasing the previously selected university.

## Would this problem exist if agentic AI had never been invented?

**Yes**, and it does exist today. A student comparing six universities opens:

- Coursefinder, once per university, filtered to their own programme;
- the GEM Explorer brochure for each, and within it the Coursework tab, the
  Financials tab, and the entry-requirement sheet;
- a currency converter, because the costs are published in DKK, KRW, JPY, CAD
  and EUR;
- and then reconciles credits against NTU academic units by hand.

That is **five tabs per university times six universities**, plus arithmetic
across five currencies. The information is public. The work is the problem.

## What a fixed workflow would miss

This is the crux, and it is why the system is agentic rather than a script.

Six real universities, six different ways of publishing the same fact —
verbatim, from brochures captured on 2 September 2026:

| University | Course load, as published |
|---|---|
| **Aalborg** | a table: `Minimum \| Maximum` → `30 ECTS \| 30 ECTS` |
| **Ajou** | `6 credits (usually 2 courses)` to `19 credits (usually 6~7 courses)` |
| **Akita** | `Minimum : 12 credits (required by Japanese immigration law)`, maximum 18 |
| **Queen's** | `Recommended minimum: 15 credits per term (equivalent to 30 ECTS/year)` |
| **Waterloo** | `1.5 credits (3 undergraduate courses)` to `2.5 credits (5 undergraduate courses)` |
| **Macalester** | *nothing at all* |

No single parser handles that set:

- Three different unit systems (ECTS, local credits, course counts).
- One where the minimum is a **legal** constraint, not an academic one — a
  student who loses that sentence loses the reason they cannot drop a course.
- One where a single table cell carries **two units at once**, so pairing the
  numbers positionally reads Ajou's minimum of 6 as a maximum of 2.
- One with **fractional credits** — an integer parser breaks on Waterloo.
- Exactly one that states an equivalence, and it converts to **ECTS, not AU**.
- One that publishes nothing, where the only correct output is to say so.

A fixed workflow has to pick a number. This system picks an *unit*, keeps the
university's own words, and converts only where the university itself stated the
basis. **Five of those six show "conversion not available"** — and that is the
behaviour worth demonstrating, not hiding.

## Where the agency actually is

Three decisions are made per turn that a script cannot make in advance:

1. **Which lanes to run.** A router classifies the question and commits to a
   plan *before* any work happens. "How much does Ajou cost?" runs finance
   alone; "which universities in Japan, and is the food good?" fans out to
   course-matching and research at once and keeps their sources apart.
2. **Whether the evidence is good enough.** The finance lane reads the
   university's own figures first and falls back to a cost-of-living estimate
   *only* on documented insufficiency — Aalborg publishes DKK in prose and EUR
   in a table, which cannot be summed honestly, so the fallback fires and says
   why.
3. **Whether to answer at all.** With no search provider, the research lane
   returns a structured refusal rather than an uncited claim. With no published
   course load, the workload lane says the brochure is silent.

4. **Whether the question is NTU-specific.** The deterministic router detects
   topic families rather than exact sentences. A general financial-aid question
   can use the NTU reference collection without forcing the student through the
   shortlist intake gate; a host-specific weather or orientation question stays
   in the research/GEM calendar workflow.

## The claim, stated so it can be checked

> Every number this system shows a student came from a named source, and where
> it could not verify something it says so instead of filling the gap.

That claim is checkable — every figure on screen carries a numbered source, and
the refusals are visible in the running app. It is not currently *checked*: this
repository has no automated test suite, so the claim rests on the structure of
the code rather than on a suite that would catch a regression in it. See
`docs/METRICS.md` for exactly what is and is not measured.
