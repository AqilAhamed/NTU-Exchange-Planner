# Architecture

## The graph

```
                                    ┌──────────────────────────────┐
                                    │  the evidence bus            │
                                    │  lanes append, nothing        │
                                    │  overwrites (additive         │
                                    │  reducers in graph/state.py)  │
                                    └──────────────────────────────┘
                                                   ▲
                                                   │
  START                                            │
    │                                              │
    ▼                                              │
┌────────┐   ┌───────────┐   conditional edge      │
│ intake │──▶│ decompose │───returning list[str]────┼──┐
└────────┘   └───────────┘   (this is the fan-out)  │  │
    ▲              │                                │  │
    │              │  commits to PlannedTask[]      │  │
 regex first,      │  BEFORE any lane runs          │  │
 LLM merges over   │                                │  │
                   ▼                                │  │
      ┌────────────┴────────────┬─────────────┬─────┴──┴──────┬──────────────┐
      ▼            ▼            ▼             ▼               ▼              ▼
┌───────────┐ ┌─────────┐ ┌─────────┐ ┌────────────┐ ┌──────────────┐ ┌──────────┐ ┌──────────┐
│  course_  │ │workload │ │ finance │ │  research  │ │  conversion  │ │ official │ │ general  │
│ matching  │ │         │ │         │ │            │ │              │ │  _docs   │ │questions │
└───────────┘ └─────────┘ └─────────┘ └────────────┘ └──────────────┘ └──────────┘ └──────────┘
      │            │            │             │               │              │
   coursefinder  GEM       GEM first,     Tavily          local only     disabled
      (read-only) brochure  Wise            hit-set          + permanent   by design  ChromaDB
                           first, then     grounded         caveat
                           location fallback
      └────────────┴────────────┴─────────────┴───────────────┴──────────────┘
                                   │
                                   ▼
                            ┌─────────────┐   loops at most
                            │   reflect   │◀──max_reflect_iterations
                            └─────────────┘   (a counter in state,
                                   │           not the model's call)
                                   ▼
                            ┌─────────────┐
                            │   render    │  the ONLY node that builds an answer.
                            └─────────────┘  Applies the source policy: drops and
                                   │         warns on anything a running lane
                                   ▼         may not emit.
                                  END
```

**Why a fan-out rather than a chain.** `decompose` is a conditional edge that
returns a **list** of node names, which is how LangGraph runs lanes in the same
superstep. "Which universities in Japan, and is the food good?" runs
course-matching and research concurrently and keeps their evidence separate. The
lanes never call each other and never share a mutable object — each returns a
patch, and the additive reducers merge them.

**Why one renderer.** Having exactly one place where evidence becomes prose is
what makes the source policy enforceable at all. There is a single choke point,
and it is `render_agent`.

## Request path

Locally the Next dev server proxies `/backend/*`; deployed, CloudFront does the
same job. Either way the same FastAPI app answers the same routes.

```
local:    browser ──▶ next.config.ts rewrite /backend/* ──▶ FastAPI :8000

deployed: browser ──▶ CloudFront ─┬─ /*         ──▶ S3 (static export, OAC)
                                  └─ /backend/* ──▶ API Gateway ──▶ Lambda
                                     (a CloudFront Function strips /backend)
```

```
   POST /api/chat          one turn, complete response
   POST /api/chat/stream   SSE: open → plan → task* → done
   GET  /api/universities  shortlist, paginated
   GET  /api/.../mappings  de-duplicated inside SQL
   GET  /api/mappings/{}/details  latest submission
   POST /api/research      the "course load & entry bar" panel
   GET/POST/DELETE /api/chats      session store
   GET  /api/general-questions/source/{id}  indexed Chroma passage
   GET  /api/health        row counts + whether a key is configured
```

Streaming is the one behavioural difference between the two. API Gateway
buffers, so deployed, `/api/chat/stream` delivers its events with the final
response rather than progressively. `AWS_LWA_INVOKE_MODE=buffered` is set on the
Lambda for exactly this reason.

## Data sources and what each is trusted for

| Source | Trusted for | Never used for |
|---|---|---|
| `coursefinder.db` (read-only) | Which mappings are **approved**, for which programme | Anything qualitative |
| GEM Explorer brochure | Course load, entry CGPA, published costs | Anything the brochure does not state |
| Project-local ChromaDB | NTU-student-specific GEM Explorer/SUSEP process and support guidance | Host-university facts or public research |
| Tavily web search | Qualitative claims, each tied to a returned URL | Eligibility or mapping facts |
| Wise | A labelled cost **estimate**, converted from GBP to SGD with a fixed planning rate | A university's own costs |
| Frankfurter (ECB) | An exchange rate, always with its date | A rate when the service is down |
| Nominatim / Wikipedia | The host city, with the method recorded | A city when neither can establish one |

## The source policy

`graph/policy.py` maps each lane to the source types it may emit. `render_agent`
screens every source against the union of what the **lanes that actually ran**
are permitted to emit, and drops the rest with a counted warning.

| Lane | May cite |
|---|---|
| `course_matching` | `coursefinder`, `gem_explorer` |
| `workload` | `gem_explorer`, `coursefinder` |
| `finance` | `wise`, `local`, `coursefinder` |
| `research` | `gem_explorer`, `official`, `web`, `reddit` |
| `conversion` | `local` |
| `official_docs` | *(nothing — disabled)* |
| `general_questions` | `ntu_intranet` (indexed ChromaDB passages) |

The consequence a judge can check: ask which modules map at Aalborg, and no
forum post, blog or cost scrape can influence the answer. Ask about NTU
financial aid, and the answer is grounded in the student-reference collection,
not a generic web search. Both boundaries are enforced in code; neither is
covered by an automated test (see [METRICS.md](METRICS.md)).

## Conversation state and dynamic follow-ups

Each turn receives the persisted conversation history, profile, active filters,
selected university and prior rendered results. Intake extracts only new user
facts, while the planner resolves references such as “there”, “those modules”
and “in Asia instead” against the current context. A scope-changing follow-up
releases a carried university when it requests broader options, but retains
compatible module, GPA, budget and mapping-type constraints. Restored mapping
types such as BDE are additive: “include BDE” or “show BDE” can re-add those
rows alongside an active module-code filter, while an explicit “only BDE”
request remains a type whitelist.

A partial university name that matches several partners produces a numbered
list to choose from rather than a silent pick, and a reply of “another” moves
to the next match from the earlier request.

This prevents a follow-up from merely repeating the previous response. The
latest query is rendered as a new sequential turn while earlier university
cards remain in the conversation history.

## NTU-student RAG lane

`tools/ingest_general_questions.py` extracts the supplied NTU intranet PDFs
offline using layout-aware, section-preserving chunks, creates local vector
embeddings and stores the records in `data/general_questions_chroma`. Runtime
code in `agents/general_questions_agent.py` imports only
`data.general_questions_store` and queries the persisted collection. It does not
read the PDF directory or serve PDFs.

At query time the lane combines Chroma dense retrieval with an in-memory
BM25-style lexical ranking over the small indexed corpus. Reciprocal-rank
fusion preserves both paraphrase matches and exact policy/acronym matches;
page-level diversity prevents near-duplicate fragments from consuming the
context window. The configured LLM then synthesizes a conversational Markdown
answer from the retrieved evidence — Bedrock Claude Haiku in the deployed
stack, Groq locally. A grounding check drops unsupported or copied claims, a
completeness gate rejects an answer that omits required options or published
figures, and a bounded extractive fallback is used only when synthesis is
unavailable.

Each record stores a stable source ID, document name, page, chunk index and
passage text. The API source endpoint returns the indexed passage for the
clickable citation. This gives the lane a clean migration boundary: the Chroma
artifact and embedding model are packaged with the runtime and seeded into
`/tmp` on Lambda cold start, and could be replaced by a managed vector store
without changing the router or answer contract.

The current project copy contains a populated `ntu_exchange_general_questions`
collection with 46 indexed chunks under `data/general_questions_chroma` and
the matching ONNX MiniLM model under `data/general_questions_embedding`. These
artifacts travel with the project folder; the source PDF dataset is not needed
at query time.

## Boundaries that exist on purpose

- **`data/au_units.py`** is importable only by `au_agent`. The previous build
  applied a hardcoded `2 ECTS = 1 AU` by default to every university; this one
  quarantines that arithmetic behind an explicit request and caveats it
  permanently.
- **`BedrockProvider` carries no API key, by design.** On Lambda the execution
  role holds `bedrock:InvokeModel` and the SDK resolves credentials from the
  environment, so no secret is baked into the image or an environment variable.
  It is the provider the deployed stack runs on; Groq remains the local default
  and the documented fallback if Bedrock access is ever withdrawn.
- **The Bedrock surface is `bedrock-runtime`, not Mantle.** Not a preference:
  this organisation's service control policy carries an explicit deny on
  `bedrock-mantle:CreateInference`, and an SCP cannot be granted around from
  inside the account. `BEDROCK_API` selects between them and defaults to
  `runtime`.
- **`gem_parser.py` is pure.** No network, no clock, no token — so the whole of
  the workload and finance extraction is testable against captured fixtures.

## Stack

Backend: Python 3.12, LangGraph 1.2.11, FastAPI, Pydantic v2, SQLite, ChromaDB,
httpx, rapidfuzz. `pypdf` is used only by the offline ingestion script.
Frontend: Next.js 15.5, React 19.2, Tailwind v4, TypeScript.

Deployed: Lambda container image behind API Gateway, CloudFront over S3 for the
static export, DynamoDB for sessions and the durable cache, Amazon Bedrock for
inference. See [AWS_DEPLOYMENT.md](AWS_DEPLOYMENT.md).
