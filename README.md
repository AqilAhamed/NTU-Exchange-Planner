# NTU Exchange Planner

A pre-exchange planning assistant for NTU students choosing a **GEM Explorer**
(overseas) or **SUSEP** (Singapore) semester.

### ▶ Live: **https://d3c8p4tqwyomim.cloudfront.net**

Deployed on AWS — CloudFront, Lambda, DynamoDB and Amazon Bedrock, in
`us-east-1`. No credentials are stored anywhere in the system; the Lambda
execution role supplies them.

A student states their degree programme and Semester 1 or Semester 2. The
system shortlists partner universities that have *approved* Coursefinder module
mappings, shows the mapping history, explains each host university's course-load
rules **in that university's own units**, estimates a monthly budget from
published figures, and answers qualitative questions with citations. Follow-up
turns reuse the active profile, filters, selected university and prior results
so the chat behaves as one conversation rather than isolated queries.

**Every number it shows came from a named source. Where it cannot verify
something, it says so instead of filling the gap.**

- [Problem statement and agentic justification](docs/PROBLEM_STATEMENT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [AWS deployment runbook](docs/AWS_DEPLOYMENT.md)
- [Evaluation and metrics](docs/METRICS.md)

---

## The end-to-end pipeline

One question in, one grounded answer out. Every stage below is a real node in
the LangGraph assembly in `backend/src/graph/build_graph.py`.

```
  Student question
        │
        ▼
  ┌───────────────────────────────────────────────────────────────┐
  │ 1. INTAKE                                                      │
  │    intake_fallback  deterministic regex extraction, always     │
  │                     runs first, needs no credentials           │
  │    intake_agent     LLM extraction layered on top; every       │
  │                     value re-validated against the database    │
  └───────────────────────────────────────────────────────────────┘
        │  Profile{programme, semester, country, CGPA, budget, …}
        ▼
  ┌───────────────────────────────────────────────────────────────┐
  │ 2. ROUTE  (decompose_agent)                                    │
  │    Commits to a plan BEFORE any work happens. The plan is      │
  │    returned to the UI, so the student watches the router       │
  │    decide, then watches it execute.                            │
  └───────────────────────────────────────────────────────────────┘
        │  conditional edge returns list[str] → true fan-out
        ├──────────┬──────────┬──────────┬──────────┬─────────────┐
        ▼          ▼          ▼          ▼          ▼             ▼
   course_     workload    finance   research   general       conversion
   matching                                     questions
        │          │          │          │          │             │
  Coursefinder  GEM       city_state  Tavily     ChromaDB     au_agent /
  (SQLite,    Explorer    → Wise      hit-set    dense+BM25   currency_agent
   mode=ro)   brochure    → SGD       grounded   + RRF        (caveated)
        └──────────┴──────────┴──────────┴──────────┴─────────────┘
                              │
                              ▼  additive reducers merge lane output
  ┌───────────────────────────────────────────────────────────────┐
  │ 3. REFLECT  (reflect_agent)                                    │
  │    Bounded critique. The loop is capped by a counter held in   │
  │    state, not by the model's judgement, and it may only        │
  │    re-enter lanes that have not already run.                   │
  └───────────────────────────────────────────────────────────────┘
        │
        ▼
  ┌───────────────────────────────────────────────────────────────┐
  │ 4. RENDER  (render_agent)                                      │
  │    The ONLY node permitted to build an answer. Applies the     │
  │    source policy from graph/policy.py at this single choke     │
  │    point, so no lane can smuggle a source past it.             │
  └───────────────────────────────────────────────────────────────┘
        │
        ▼
   Answer + citations + confidence + caveats + task trace
```

### Why the lanes run in parallel

The conditional edge returns a `list[str]`, so LangGraph fans out to every
selected lane at once. They write into shared state through **additive
reducers** (`graph/state.py`), which is what lets concurrent lanes append
without clobbering one another. A shortlist that also needs cost and workload
costs roughly what the slowest lane costs, not the sum.

### Where the evidence discipline lives

`graph/policy.py` holds a **source allow-list in code**: which lane may cite
which kind of source. It is enforced at the render choke point rather than
requested in a prompt, so a model cannot talk its way past it.

The research lane goes further. Search returns a **hit set**, and any claim
citing a URL outside that set is dropped and counted — the count is itself
reported. A finding either carries a URL that the search actually returned, or
it does not survive.

### What each source is allowed to answer

| Source | Answers | Never answers |
|---|---|---|
| `coursefinder.db` (read-only) | Which mappings are **approved**, for which programme | Anything qualitative |
| GEM Explorer brochure | Course load, entry CGPA, published costs | Anything the brochure does not state |
| Project-local ChromaDB | NTU-student GEM Explorer / SUSEP process and support guidance | Host-university facts or public research |
| Tavily web search | Qualitative claims, each tied to a returned URL | Eligibility or mapping facts |
| Wise | A labelled cost **estimate**, GBP→SGD at a fixed planning rate | A university's own costs |
| Frankfurter (ECB) | An exchange rate, always with its date | A rate when the service is down |
| Nominatim / Wikipedia | The host city, with the method recorded | A city when neither can establish one |

---

## Deployed architecture

```
  browser
     │
     ▼
  CloudFront ── /*         ──▶ S3 (static Next.js export, OAC-signed)
             └─ /backend/* ──▶ API Gateway HTTP API ──▶ Lambda (container)
                                  CloudFront Function strips /backend
                                            │
        ┌──────────────┬────────────────────┴───────┬──────────────┐
        ▼              ▼                            ▼              ▼
  coursefinder.db  Chroma + ONNX               DynamoDB        Bedrock
  (in image, ro)   (seeded to /tmp)         (sessions+cache)  (Haiku 4.5)
```

| Piece | Value |
|---|---|
| CloudFront | `d3c8p4tqwyomim.cloudfront.net` |
| Lambda | `nex-backend` — container, 3008 MB, 120 s, x86_64, 1024 MB `/tmp` |
| Model | `us.anthropic.claude-haiku-4-5-20251001-v1:0` (US inference profile) |
| Tables | `nex-sessions`, `nex-chats`, `nex-cache` (on-demand, TTL on cache) |
| Image | 1.32 GB |

**Cold start ≈ 10 s, warm ≈ 0.8 s.** No VPC, deliberately — a VPC forces a NAT
Gateway, which bills around the clock.

Three findings from deploying this are recorded in
[docs/AWS_DEPLOYMENT.md](docs/AWS_DEPLOYMENT.md) because each cost hours:
the organisation's service control policy denies `lambda:InvokeFunctionUrl`
(hence API Gateway); `docker build` emits a manifest list that Lambda rejects
unless built with `--provenance=false`; and **Lambda has no `/dev/shm`**, so a
bundled headless-Chromium search sidecar starts cleanly and then hangs on every
search.

---

## Setting it up

This folder ships **without** `backend/.env`, `backend/.venv`,
`frontend/node_modules` or any build cache — those hold credentials or are
specific to one machine. The steps below recreate them.

Nothing else is missing. `coursefinder.db`, the Chroma collection and the ONNX
embedding model are all included, so there is no dataset to download and the
source PDFs are not needed.

**Requirements:** Python 3.12 or newer, Node 20 or newer with npm. Windows is
the tested path; on macOS and Linux the same commands work with
`.venv/bin/python` in place of `.venv\Scripts\python.exe`.

### 1. API keys — optional, it runs without them

Both have free tiers and neither asks for a card. **The application starts and
answers without either**; each missing key disables one capability cleanly
rather than breaking the app.

| Key | Where to get it | What it powers | Without it |
|---|---|---|---|
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) → API Keys (starts `gsk_`) | Intake, routing, answer synthesis | Falls back to regex intake and deterministic routing. Every lane still answers, more tersely. |
| `TAVILY_API_KEY` | [app.tavily.com](https://app.tavily.com) → API Keys (starts `tvly-`), 1,000 searches a month | The research lane | Research questions decline with an explanation. Shortlist, workload and cost are unaffected. |

Copy the template:

```bash
cp backend/.env.example backend/.env
```

Then edit `backend/.env` and fill in what you have:

```
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_your_key_here

SEARCH_PROVIDER=tavily
TAVILY_API_KEY=tvly_your_key_here
```

Paste each key as one continuous string on one line. A stray space or a line
break inside the value is the usual cause of a `401 Unauthorized`, and the key
will still *look* correct.

`backend/.env` is listed in `.gitignore`. Never commit or share it.

### 2. Start it

```powershell
.\start.bat
```

That creates the virtual environment, installs both dependency sets, runs
`npm install`, starts the API on **:8000** and the UI on **:3000**, waits for
both health endpoints to answer, and opens a browser. It detects stale services
on those ports and falls back to a free one rather than failing.

The manual equivalent, if you would rather run the two halves yourself:

```bash
python -m venv backend/.venv
backend/.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
backend/.venv/Scripts/python.exe -m uvicorn api.chat:app --app-dir backend/src --port 8000
```

```bash
cd frontend
npm install
npm run dev
```

Then open **http://localhost:3000**.

### 3. Path variables

Every path resolves from the repository root, so run commands from there. All
are overridable in `backend/.env`.

| Variable | Default | What it points at |
|---|---|---|
| `COURSEFINDER_DB` | `coursefinder.db` | The 179 MB read-only university database |
| `GENERAL_QUESTIONS_CHROMA_DIR` | `data/general_questions_chroma` | Persisted ChromaDB collection |
| `GENERAL_QUESTIONS_EMBEDDING_DIR` | `data/general_questions_embedding` | Local ONNX MiniLM model |
| `APP_STATE_DB` | `data/app_state.db` | Local session and chat history (created on first run) |
| `BACKEND_URL` | `http://127.0.0.1:8000` | Read by `next.config.ts` for the dev proxy |

`PYTHONPATH` must include `backend/src` when invoking Python directly, because
the app imports `api`, `graph`, `agents` and `data` as top-level packages:

```bash
PYTHONPATH=backend/src backend/.venv/Scripts/python.exe -c "from graph import policy; print(policy.LANE_SOURCES.keys())"
```

`start.bat` and the `uvicorn --app-dir backend/src` command above both set this
for you. In the container the same paths point into `/tmp`, because `/var/task`
is read-only on Lambda and ChromaDB opens its store read-write.

### 4. Running with Docker instead

The `Dockerfile` builds the exact image the deployed Lambda runs, so this is the
closest local equivalent to production:

```bash
docker build --provenance=false --sbom=false -t nex-backend .
docker run --rm -p 8000:8000 --env-file backend/.env -e STORAGE_BACKEND=sqlite nex-backend
```

`--provenance=false --sbom=false` is not optional if you intend to deploy the
image: without it buildx emits a manifest list with an attestation manifest,
and AWS Lambda rejects it outright with *"image manifest ... not supported"*.

The API is then on **:8000**; allow ~10s on first start while the Chroma
collection and embedding model are copied into `/tmp`. The UI is separate — run
`npm run dev` in `frontend/`, or serve the pre-built static export in
`frontend/out/`.

One trap with `--env-file`: Docker does **not** strip quotes. A line written as
`TAVILY_API_KEY="tvly-..."` arrives inside the container with the quote
characters still attached, and the provider then answers `401` on a key that
looks perfectly correct. Write values unquoted, or pass them with `-e` instead.

### 5. Verify

```bash
curl http://127.0.0.1:8000/api/health
```

A healthy install reports `status: "ok"`, **557** universities, **43,962**
mappings and **46** ChromaDB chunks. `degraded` means `coursefinder.db` could
not be opened, and the shortlist lane will decline rather than guess.

### If something looks wrong

| Symptom | Cause |
|---|---|
| `status: "degraded"` | `coursefinder.db` is not in the repository root, or is the 0-byte placeholder rather than the 179 MB file |
| Research questions always decline | No `TAVILY_API_KEY`, or `SEARCH_PROVIDER=null` |
| Answers are terse, no synthesis | No `GROQ_API_KEY` — this is the deterministic fallback working as designed |
| `401 Unauthorized` from a provider | A space or newline inside the key in `backend/.env` |
| Port already in use | The launcher picks a free port and prints it; read its output rather than assuming 3000 |
| `ModuleNotFoundError: data` when running anything under `backend/` | Run from the repository root, or set `PYTHONPATH=src` |

### Configuration

`backend/.env`, never committed. It is in `.gitignore`.

| Variable | Default | Effect if unset |
|---|---|---|
| `LLM_PROVIDER` | `groq` locally, `bedrock` deployed | `null` gives the no-credential deterministic mode |
| `GROQ_API_KEY` | *(none)* | Intake and routing fall back to regular expressions. Everything still answers. |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | — |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Used when `LLM_PROVIDER=bedrock` |
| `SEARCH_PROVIDER` | `tavily` | `openserp`, `cloud` or `null` |
| `TAVILY_API_KEY` | *(none)* | Research lane declines cleanly |
| `WISE_GBP_TO_SGD` | `1.72` | The fixed planning rate, so costs are deterministic |
| `WISE_TIMEOUT_SECONDS` | `8` | Sized so city + country fallback fits inside API Gateway's ~30 s |
| `STORAGE_BACKEND` | `sqlite` locally, `dynamodb` deployed | — |
| `COURSEFINDER_DB` | `coursefinder.db` | Health endpoint reports `degraded` |
| `GENERAL_QUESTIONS_CHROMA_DIR` | `data/general_questions_chroma` | The persisted vector store |
| `GENERAL_QUESTIONS_EMBEDDING_DIR` | `data/general_questions_embedding` | Project-local ONNX embedding cache |
| `BACKEND_URL` | `http://127.0.0.1:8000` | Read by `next.config.ts` for the dev proxy |

### The research lane

Deployed it uses **Tavily** (1,000 free searches a month) — one HTTPS call, no
browser, no sidecar.

`openserp` remains selectable and is a good local option, where a browser
exists and a self-hosted instance has no per-query cost:

```bash
docker compose up -d openserp
```

It **cannot run on Lambda**: there is no `/dev/shm`, Chromium needs shared
memory, so it starts and then hangs on every search. With
`SEARCH_PROVIDER=null` the core planning lanes remain fully usable and only
research questions decline.

### The NTU-student RAG corpus

The supplied intranet PDFs are ingestion inputs, not runtime dependencies.
Rebuild the portable ChromaDB artifact from the project root:

```bash
backend\.venv\Scripts\python.exe tools\ingest_general_questions.py --reset
```

At query time the lane combines Chroma's dense retrieval with exact-term
BM25-style ranking, fuses them with reciprocal rank fusion, promotes genuine
structured tables when a question asks for options, amounts, eligibility or
dates, then synthesises a grounded answer with numbered citations. Generated
claims are checked against their cited passages, and a completeness gate
rejects an answer that omits required options or published figures. If the LLM
is unavailable it falls back to a bounded extractive answer rather than
printing a database chunk. **The original PDFs are never read at query time.**

---

## What each part does

### Repository map

| Path | Purpose |
|---|---|
| `start.bat` / `start.ps1` | Windows launchers. Bootstrap the venv and npm, start both servers, wait for health, open a browser |
| `backend/src/` | The FastAPI application and the LangGraph agent system — everything below |
| `backend/requirements.txt` | Runtime Python dependencies |
| `backend/requirements-dev.txt` | Adds pytest, ruff and moto on top of the above |
| `backend/.env.example` | Template for secrets and configuration; copy to `backend/.env` |
| `coursefinder.db` | Read-only SQLite, 179 MB: 557 universities, 43,962 approved mappings, 73,752 submissions |
| `data/general_questions_chroma/` | Persisted ChromaDB collection, 46 indexed chunks |
| `data/general_questions_embedding/` | Project-local ONNX MiniLM embedding model |
| `frontend/` | Next.js 15 UI. `lib/api.ts` is the schema of record |
| `frontend/out/` | Pre-built static export — what is served from S3 |
| `tools/` | Offline scripts, never imported at runtime |
| `deploy/` | AWS deployment scripts |
| `docs/` | Architecture, AWS runbook, problem statement, metrics |
| `Dockerfile` | The container image the deployed Lambda runs |
| `docker-compose.yml` | Optional local openserp search sidecar |


### `backend/src/graph/` — the machinery

| File | Purpose |
|---|---|
| `build_graph.py` | LangGraph assembly. A router fans out to lanes that run concurrently and converge on one renderer. |
| `router_rules.py` | Deterministic intent classification. No LLM, so routing is testable with no credentials. |
| `policy.py` | The source allow-list. Which lane may cite which kind of source, enforced in code rather than in a prompt. |
| `state.py` | Shared state and the additive reducers that let parallel lanes append without clobbering each other. |
| `domain.py` | Pydantic contracts mirroring `frontend/lib/api.ts` field for field. |
| `metrics.py` | The six graded metrics, derived from what a run actually recorded. |
| `providers.py` | LLM seam: Bedrock (deployed), Groq (local and fallback), Null. |
| `terms.py`, `config.py` | Exchange-term handling plus path and environment resolution. |

### `backend/src/agents/` — the specialized agents

| Agent | Job |
|---|---|
| `intake_fallback` | Deterministic profile extraction. Always runs first. |
| `intake_agent` | LLM extraction layered over it; every value re-validated against the database. |
| `decompose_agent` | **The router.** Commits to a plan before any work happens. |
| `mapping_agent` | Core planning lane: the shortlist, from Coursefinder. |
| `workload_agent` | Course-load rules, in each university's own units. |
| `cost_of_living_agent` | Finance lane: reads the enriched Coursefinder city/state, then Wise monthly cost and expense distribution in SGD. |
| `research_agent` | Qualitative questions, answered only with citations from the search hit set. |
| `general_questions_agent` | NTU-student GEM Explorer and SUSEP guidance, from the project-local ChromaDB corpus. |
| `au_agent`, `currency_agent` | Explicit conversions the student asked for, permanently caveated. |
| `official_docs_agent` | Deliberately disabled. Recognises policy questions and refers them. |
| `reflect_agent` | Bounded critique, capped by a counter held in state. |
| `render_agent` | The only node that builds an answer. Applies the source policy. |

### `backend/src/data/` — the adapters

`coursefinder_db.py` (read-only SQLite, parameterized, de-duplication inside
SQL) · `gem_client.py` (Terra Dotta OAuth, search, brochure fetch) ·
`gem_parser.py` (pure, network-free evidence extraction) · `programmes.py` and
`destinations.py` (resolution derived from the database, not hardcoded) ·
`host_city.py` · `wise.py` · `currency.py` · `search.py` · `au_units.py`
(quarantined behind `au_agent`) · `general_questions_store.py` (ChromaDB access).

### `frontend/`

Next.js 15 / React 19 / Tailwind v4, built as a static export and served from
S3. `lib/api.ts` is the **schema of record** — the backend mirrors it, not the
other way round. `app/components/Evidence.tsx` holds the panels that render
sources, workload, eligibility, budget and live task progress.

### Scripts

| Script | Purpose |
|---|---|
| `start.ps1` | Full bootstrap: venv, dependencies, both servers, health check |
| `start.bat` | Fast path once bootstrapped |
| `tools/ingest_general_questions.py` | Offline PDF extraction and ChromaDB embedding build |
| `tools/enrich_university_locations.py` | Populates `universities.city_state` for the finance lane |
| `deploy/push.ps1` | Build and push the container image to ECR |
| `deploy/deploy-lambda.sh` | Create or update the Lambda and its configuration |
| `deploy/push-ui.sh` | Sync the static export to S3 and invalidate CloudFront |
| `docker-compose.yml` | Optional local openserp search sidecar |

---

## Design decisions worth knowing

**The frontend is the contract.** `frontend/lib/api.ts` declares every type; a
mismatch is a bug in the backend.

**`coursefinder.db` is opened read-only, always.** `mode=ro` URIs, parameterized
queries, never LLM-generated SQL. Connections are per-request, because FastAPI
runs sync endpoints on a threadpool.

**No conversion without a cited basis.** A host university's course load is
reported in its own units. An AU or ECTS figure appears only when the brochure
itself states the equivalence.

**Refusing is a supported outcome.** No search provider, no published course
load, an unresolvable city, an unavailable exchange rate: each produces a
structured, explained "I don't have this" rather than a plausible number.

**Every loop is bounded by a counter in state**, not by the model's judgement.

**Conversation state is first-class.** The session store keeps messages, profile
values, active filters, selected-university context, rendered results and
pagination. A follow-up can broaden a region, change a constraint or ask about a
previously mentioned place without losing context.

**Ambiguity is asked about, not guessed.** A partial university name matching
several partners produces a numbered list to choose from rather than a silent
pick.

**Mapping exclusions are enforced before rendering**, in the Coursefinder query
rather than after grouping, so excluded rows cannot reappear in the detail panel.

---

## Known limits

- **No automated test suite is included in this submission.** Verify the
  install with the health endpoint above, which reports whether Coursefinder
  opened, how many rows it holds and how many chunks are indexed.
- **The endpoints have no authentication.** `GET /api/chats` returns every
  conversation stored in the backing store, and any caller who knows a session
  id can read or delete it. Acceptable for a demo; a blocker for real use.
- The deployed API endpoint is publicly reachable. Account-level Lambda
  concurrency caps the damage, but it is not access control.
- The GEM parser is validated against 6 brochures out of 385, by hand; name
  matching reaches 525 of 558 universities (94%).
- Streaming is buffered in production. API Gateway does not stream, so
  `/api/chat/stream` delivers its events with the final response rather than
  progressively. The local dev server streams normally.
- A CGPA-filtered shortlist checks at most 50 candidate universities, because
  each costs a brochure read. When more partners match, the response says so
  instead of reporting the eligible count it found as the count overall.
- Cost figures are Wise estimates converted at a fixed planning rate, not a
  university's own published costs, and are labelled as such everywhere.
