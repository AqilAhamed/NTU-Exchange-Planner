# AWS deployment runbook

**Status: deployed and serving.**

    https://d3c8p4tqwyomim.cloudfront.net

Account 400200465364, us-east-1. Every lane verified against the live stack:
Coursefinder (557 universities, 43,962 mappings), the ChromaDB RAG corpus
(45 chunks, seeded to /tmp), DynamoDB sessions, and Claude Haiku on Bedrock
through the Lambda execution role with no key anywhere in the system.

Source of truth for the account rules: *IGNITE Hackathon 2026 AWS accounts
access guide for students* (33 pages). Page references below are to that PDF.

---

## 1. The constraints, from the guide

| Rule | Page | Consequence here |
|---|---|---|
| **Region is `us-east-1`** | 33 | Settled. The workshop deck's `ap-southeast-1` is wrong for this account — "multiple *Access denied* bars" is the symptom of being in the wrong region. |
| Access keys **expire every 12 hours** | 23 | Re-login mid-deploy is normal. Never bake keys into an image or a Lambda env var. |
| **Access revoked at $20**, account **terminated at $30** | 16, 28 | The bar shows $30 but $20 is the real ceiling. Cost reporting lags by hours, so you find out late. |
| **One lease per team, ever** | 16 | Request it when you are ready to deploy, not while still building. |
| Approval takes **up to 2 working days** | 17 | This is on the critical path. |
| Registration and leasing are **one person, once** | 3, 11 | The group representative shares username, password and the 2FA secret key (p6) so everyone can log in. |

**Never launch these** (p30) — each eats the $20 while idle: OpenSearch,
SageMaker real-time endpoints, NAT Gateway, ALB/NLB, EC2, RDS, Bedrock
Provisioned Throughput.

**Build on these** (p31): Bedrock on-demand, Lambda + Function URLs, DynamoDB
on-demand, S3, CloudFront.

---

## 2. Bedrock: settled against the account

Verified from CloudShell on the live lease, not inferred from docs.

| Setting | Value | How it was established |
|---|---|---|
| Region | `us-east-1` | Access guide p33, confirmed by a working call |
| API surface | **`bedrock-runtime`** (InvokeModel) | `bedrock-mantle` is SCP-denied; see below |
| Model id | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Returned a completion; the bare id is the foundation model, the `us.` prefix is the cross-region inference profile |
| Prompt caching | Supported | The response carries `cache_creation` / `cache_read` usage fields |

Put these in `backend/.env`:

```
BEDROCK_REGION=us-east-1
BEDROCK_API=runtime
BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0
```

They are also the built-in defaults in `graph/providers.py`, so an unset
environment behaves the same way.

### The Mantle surface is closed, and cannot be opened from inside

Current SDK guidance prefers `AnthropicBedrockMantle` for new code. On this
account it fails:

```
User: .../hackathon2026,<user> is not authorized to perform:
bedrock-mantle:CreateInference ... with an explicit deny in a
service control policy: arn:aws:organizations::.../p-1sclicmp
```

A service control policy sits **above** the account, so no IAM change inside it
can grant this — the policy was almost certainly written against `bedrock` and
never updated for the newer `bedrock-mantle` service. `_bedrock_client()`
therefore defaults to the classic `AnthropicBedrock` client and keeps Mantle
behind `BEDROCK_API=mantle` for the day the policy changes.

**This is why the model id has a date suffix.** The bare
`anthropic.claude-haiku-4-5` form belongs to the Mantle path; the runtime path
wants the full id or an inference profile. An earlier revision of this document
called the dated form wrong — it is correct for the surface this account
actually permits.

### The three model-id forms, and which one to use

Measured against the account, not guessed:

| Form | Result |
|---|---|
| `anthropic.claude-haiku-4-5-20251001-v1:0` (bare foundation model) | **400** — on-demand throughput unsupported; needs an inference profile |
| `global.anthropic.claude-haiku-4-5-20251001-v1:0` (global profile) | **SCP deny** — the organisation blocks global cross-region routing |
| `us.anthropic.claude-haiku-4-5-20251001-v1:0` (US regional profile) | **Works** |

Note the second row carefully: that SCP deny is **per model id**, not
service-wide. `bedrock-runtime` itself is open. An early version of
`verify_bedrock.py` treated any SCP deny as "this whole surface is closed",
abandoned the runtime surface on the `global.` failure, and never tried the
`us.` profile that works — reporting the LLM as unavailable when it was not.
The tool now only abandons a surface when the deny names the service itself
(`bedrock-mantle`), and tries `us.` profiles first.

### Bedrock pricing is not first-party pricing

Claude on Bedrock is partner-operated and billed by AWS at its own rates. The
`$1/$5 per M` figure in the original design is the Anthropic first-party rate.
Check <https://aws.amazon.com/bedrock/pricing/> for the us-east-1 Haiku rate
before budgeting.

### If Bedrock is ever revoked mid-event

`LLM_PROVIDER=groq` with `GROQ_API_KEY` set on the Lambda is a working fallback
that needs no AWS permission and no rebuild — `langchain-groq` already ships in
the image. Lambda has internet egress as long as it stays out of a VPC, which
it must anyway (a NAT Gateway is a named budget-killer).

## 3. Architecture, as deployed

```
  browser
     |
     v
  CloudFront  ── /*         ──> S3 (static Next.js export, OAC-signed)
              └─ /backend/* ──> API Gateway HTTP API ──> Lambda
                                   (CF Function strips /backend)
                                            |
        ┌──────────────┬────────────────────┴───────┬──────────────┐
        v              v                            v              v
  coursefinder.db  Chroma + ONNX               DynamoDB        Bedrock
  (in image, ro)   (seeded to /tmp)         (sessions+cache)  (Haiku, on-demand)
```

| Piece | Value |
|---|---|
| CloudFront | `d3c8p4tqwyomim.cloudfront.net` (dist `E3EBQTLNDXZBV`) |
| UI bucket | `nex-ui-400200465364` (private, OAC) |
| API | `iy87ulo4ri.execute-api.us-east-1.amazonaws.com` |
| Lambda | `nex-backend`, 2048 MB, 120 s, x86_64, image `nex-backend:latest` |
| Role | `nex-backend-lambda-role` |
| Tables | `nex-sessions`, `nex-chats`, `nex-cache` (TTL on `expires_at`) |

### Why API Gateway and not the Lambda Function URL

The original design used a Function URL with CloudFront Origin Access Control.
**That is blocked in this account.** Proven, not assumed:

| Request | Result |
|---|---|
| Our IAM identity, SigV4, direct to the Function URL | **200** |
| CloudFront OAC, SigV4, same URL, correct resource policy | **403** |
| Anonymous, auth type `NONE`, resource policy allowing `*` | **403** |

Two explicit `Allow` statements with no effect, while the S3 OAC on the same
distribution works — so CloudFront signing is fine and the organisation denies
`lambda:InvokeFunctionUrl` above the account. The same shape as the
`bedrock-mantle` deny in §2. An SCP cannot be worked around from inside, so
API Gateway (`lambda:InvokeFunction`, an action this account permits) is the
way in.

### Two things that cost hours, recorded so they don't again

**`apigatewayv2 create-api --target` does not reliably add the Lambda invoke
permission.** It is documented as creating the integration, route, stage and
permission. The permission was missing, so API Gateway returned
`Internal Server Error` while never invoking Lambda at all. Add it explicitly:

```bash
aws lambda add-permission --function-name nex-backend --statement-id apigw-invoke \
  --action lambda:InvokeFunction --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:us-east-1:<account>:<api-id>/*" --region us-east-1
```

**Read the logs before theorising about response formats.** A `200 OK` in the
Lambda log during that period was a direct Function URL call, not API Gateway;
mistaking it for a working integration sent us through invoke-mode and payload
-format debugging for what was a missing IAM statement.
`aws logs tail` showing *no invocation* is the fastest way to tell "the app
broke" from "the app was never called".

### What this costs

**SSE streaming.** API Gateway buffers, so `/api/chat/stream` delivers its
events with the final response rather than progressively. The app is unaffected
in substance; the visible "watch the router plan, then execute" sequence is
not. `AWS_LWA_INVOKE_MODE=buffered` is set as a Lambda environment variable
(overriding the image's `response_stream`) so this needed no rebuild.

**A ~30 s ceiling** instead of the app's own 110 s. Warm turns are far inside
it — a shortlist returns in under a second — but a cold start plus a slow
Bedrock call could clip it.

**A public API endpoint.** Unlike the OAC-signed Function URL, the API Gateway
URL is reachable by anyone who finds it. The account's own concurrency limit
(~10) caps the damage; `put-function-concurrency` could not lower it further
because reserving any would drop unreserved below the account minimum.

### Wise supplies the cost lane

The finance lane reads the enriched `universities.city_state` column, tries the
country-scoped Wise city page first, and uses Wise's aggregate country page
when the city page is incomplete or unsearchable. The GBP headline is converted
to SGD with the fixed planning rate in `WISE_GBP_TO_SGD` (default `1.72`), and
the optional Distribution of Expenses percentages become monthly line items.

Wise responses are cached durably for the configured TTL. To warm a shared
DynamoDB cache before a demo:

```powershell
$env:STORAGE_BACKEND="dynamodb"
$env:AWS_ACCESS_KEY_ID="..."; $env:AWS_SECRET_ACCESS_KEY="..."; $env:AWS_SESSION_TOKEN="..."
backend/.venv/Scripts/python.exe tools/prewarm_costs.py CSC --limit 12
```

Warming is optional; Wise can also be fetched on demand from Lambda. The
enrichment itself is reproducible with `tools/enrich_university_locations.py`.

### openserp runs inside the Lambda container

The original design said openserp must "never become an EC2 instance behind an
ALB — three budget-killers at once". It is now a background process in the same
image instead: 28 MB of binary plus a 253 MB headless-shell, costing nothing
when no request is in flight.

Three things this needed, each found by it failing:

**The browser is not optional.** The openserp binary alone starts fine and then
downloads ~150 MB of Chromium on the first search — hopeless against a 512 MB
`/tmp` on every cold start. Its own image ships a headless-shell and points at
it with `OPENSERP_APP_BROWSER_PATH`, so both are copied from there.

**Four missing libraries.** `python:3.12-slim` lacks `libexpat`, `libnspr4`,
`libnss3` and `libnssutil3`. Without them the browser fails to launch, openserp
returns `engine_internal`, and the app reports "no search provider connected" —
a correct refusal for entirely the wrong reason. `libnss3` and `libexpat1`
cover all four.

**Timeouts had to come down.** `search.TIMEOUT_SECONDS` was 35 and
`research_agent.MAX_SECONDS` was 25 — both written for the local streaming path
with its 110 s deadline. Behind API Gateway the request dies at ~30 s, and a
search that outlives the gateway gives a timeout instead of an answer. Now 12
and 14, both environment-tunable (`SEARCH_TIMEOUT_SECONDS`,
`RESEARCH_MAX_SECONDS`) so the deployed value can change without a rebuild.

Measured in the container under Lambda's read-only rootfs: **1.9 s** for a cold
search including browser launch, effectively instant warm, and **12 s** for a
full research turn end to end. The 48 s figure seen before the libraries were
added was retry backoff, not the browser.

`data/search.py` also carries `OpenSerpCloudProvider` (`SEARCH_PROVIDER=cloud`
plus `OPENSERP_API_KEY`) if the bundled sidecar ever needs to go.

If the streaming demo matters more than the tidy URL, the Function URL still
works for IAM-signed callers and can be driven directly from a laptop.

## 4. Code changes required first

Do all of this **before** requesting the lease. The lease clock starts on
approval and you cannot get a second one.

### 4.1 `BedrockProvider.invoke()` — **done**

Implemented in `backend/src/graph/providers.py`, verified against
`anthropic` 1.3.0 offline. `invoke()` no longer raises `NotImplementedError`;
without credentials it fails with `Could not resolve AWS credentials from
session`, which means the request shape is valid all the way to auth.

What it does, and the three things that bit during implementation:

- Uses the classic **`AnthropicBedrock`** client (`bedrock-runtime` InvokeModel)
  by default, because `bedrock-mantle` is SCP-denied on this account — see §2.
  `BEDROCK_API=mantle` switches surfaces if that policy ever changes.
- **`temperature` is not forwarded.** Sampling parameters were removed from the
  SDK: `messages.create()` accepts no `temperature` / `top_p` / `top_k` at all,
  so passing the `0.0` this codebase uses everywhere raises `TypeError` rather
  than being ignored. Groq still honours it. On Bedrock, determinism now rests
  on the model default and on prompts that were already written to be strict.
- **`system` is split out of the message list.** The codebase speaks the
  LangChain dialect where `system` is a role; the Anthropic SDK takes it as a
  top-level parameter and rejects the role. `split_system()` does this once,
  rather than at every call site.
- **Thinking blocks are skipped when reading the response.** `content` is a
  list of typed blocks; concatenating them blindly would let reasoning text
  into a JSON payload the router then tries to parse.
- The system prompt is sent with `cache_control: ephemeral`. Prompt caching is
  supported on Bedrock and is the single biggest lever on token cost.
- `max_tokens` is capped at 2048 (`BEDROCK_MAX_TOKENS`) — every caller wants a
  routing decision or a few sentences, and it is a hard ceiling on the most
  expensive half of the budget.
- `available()` resolves the local credential chain (env, profile, or Lambda
  container role) without calling AWS, matching the honesty rule `GroqProvider`
  follows. It does **not** check model access — only the account can answer
  that, and a probe would spend the budget the check exists to protect.

`anthropic[bedrock]>=0.40.0` is in `requirements.txt`.

### 4.2 Sessions and cache on DynamoDB — **done**

`services/store.py` holds both backends behind one narrow interface;
`session_store.py` and `cache.py` now contain no SQL at all and never learn
which backend they are talking to. Selected by `STORAGE_BACKEND`
(`sqlite` default, `dynamodb` in the deployed build).

**Both backends, not a migration.** Replacing SQLite outright would have made
the local loop require an AWS account or a container, trading away the offline,
no-credential path this product is built around and the demo depends on.

| Table | Key | Holds |
|---|---|---|
| `nex-sessions` | `session_id` | The state JSON `save_state` writes |
| `nex-chats` | `session_id` | Title, folder, timestamps for the sidebar |
| `nex-cache` | `key` | The `cache.py` payloads, plus an `expires_at` epoch attribute |

Verified against moto's in-memory DynamoDB with tables created exactly as
Step 5 creates them — both backends pass the same behavioural checks:
round-tripping profile and carried context, the rule that a later turn must not
rename a chat, namespace clearing, `get_or_set` not re-running its producer on
a hit, expired rows never being served, and the post-deletion tombstone not
resurrecting a chat.

Four things that are easy to get wrong here and are already handled:

- **`UpdateItem`, not `PutItem`, for sessions.** `PutItem` replaces the whole
  item and would silently drop `created_at` on every save.
- **`key` and `namespace` are DynamoDB reserved words.** Every reference goes
  through an expression-attribute name, or the scan fails at runtime.
- **`purge_expired()` is a no-op on DynamoDB.** The table TTL deletes expired
  rows for free; scanning to find them would cost strictly more than nothing.
  It still works normally on SQLite.
- **`ttl_seconds == 0` means "never expires"** in this app, so no `expires_at`
  is written for those rows — otherwise the table TTL would delete exactly the
  entries meant to be permanent.

`/api/health` now reports `storage_backend`, so Step 9 tells you which one the
deployed container actually picked up.

### 4.3 Static export for S3 — **done**

`npm run build:static` produces `frontend/out/`. The export is **opt-in** via
`NEXT_OUTPUT=export` rather than always-on, because a static export cannot
serve rewrites: turning it on unconditionally would silently remove the
`/backend` proxy `npm run dev` depends on. `npm run dev` and `npm run build`
are unchanged.

The `rewrites` key is omitted entirely from the export config rather than
returning an empty list — Next detects the *presence* of the key, so leaving it
in warns "rewrites … are not applied when exporting" on every build, which
reads like a misconfiguration when it is the intended one.

`API = "/backend"` in `lib/api.ts` is untouched and correct in both worlds:
locally the dev proxy handles it, in the deployed stack CloudFront does.

`cross-env` was added as a devDependency so the script sets the variable on
Windows as well as POSIX.

### 4.4 Dockerfile — **done**

`Dockerfile` and `.dockerignore` are at the repository root. Built and run
locally: **1.32 GB**, comfortably under Lambda's 10 GB image limit.

Two things in it are load-bearing and neither is obvious:

**AWS Lambda Web Adapter.** Python on Lambda has no native response streaming.
Without the adapter, `/api/chat/stream` could not work and the demo would fall
back to the buffered endpoint, losing the "watch the router commit to a plan,
then execute it" moment. The adapter runs the existing uvicorn app unchanged.

**`/var/task` is read-only on Lambda.** ChromaDB opens `chroma.sqlite3`
read-write and `general_questions_store.client()` calls `mkdir()`, so the
Chroma collection and the ONNX embedding model are shipped as `seed/` copies
and seeded into `/tmp/nex-data` on cold start, guarded so a warm container does
not repeat it. `coursefinder.db` is unaffected — it is opened `mode=ro` and
stays on the read-only layer. The copy is ~91 MB against the 512 MB `/tmp`
default.

The seed copy is written inline in `CMD` rather than as a `.sh` file on
purpose: a shell script authored on Windows carries CRLF line endings, and
`exec format error` during a Lambda cold start is a miserable thing to debug.
`--platform=linux/amd64` is pinned so an arm64 machine cannot silently build an
image the x86_64 function will refuse to run.

#### Verified locally, under Lambda's actual constraints

Run with `--read-only --tmpfs /tmp` — a read-only root with only `/tmp`
writable, which is what Lambda gives you:

| Check | Result |
|---|---|
| `/api/health` | `ok`, 557 universities, 43,962 mappings, 590,013 submission fields |
| Chroma from `/tmp` | 45 chunks, read-only rootfs |
| Shortlist via `POST /api/chat` | 6 cards, 7 sources |
| RAG lane (the one that writes) | 2 findings, `ntu_intranet` sources |
| SSE via `POST /api/chat/stream` | `open`, `plan`, 2x `task`, `done` |

```bash
docker build -t nex-backend:local .
docker run --rm --read-only --tmpfs /tmp:size=512m -p 8080:8000 \
  -e LLM_PROVIDER=null -e SEARCH_PROVIDER=null \
  -e STORAGE_BACKEND=sqlite -e APP_STATE_DB=/tmp/app_state.db \
  nex-backend:local
```

Those overrides are for local testing only. The image defaults are
`LLM_PROVIDER=bedrock` and `STORAGE_BACKEND=dynamodb`, which is what Lambda
should run with.

## 5. Deployment, step by step

### Step 0 — before AWS

1. Finish §4 and confirm locally: `docker run -p 8000:8000 <image>` then
   `curl localhost:8000/api/health` returns `status: "ok"`.
2. Push the code to your own git repository. Teardown does not preserve it.

### Step 1 — account and lease (one person, once)

1. Group representative builds the username: `hackathon2026,` + the registered
   leader email, no space (p3). Example:
   `hackathon2026,bella.tan@example.com`
2. Sign in at <https://d-9667b91afb.awsapps.com/start>, enter the emailed
   verification code (p4).
3. Register MFA → **Authenticator app** (p5). Click **Show secret key** and
   share that string with the team so everyone can add it to their own
   authenticator (p6). Set the password and share it too (p9).
4. **Applications** tab → *Innovation Sandbox Ignite Hackathon Application*
   (p12) → **Request a new lease** (p13) → template **Hackathon 2026** (p14) →
   accept the terms (p15) → **Submit request** (p16).
5. Wait for approval, up to 2 working days (p17). The email often lands in
   spam; log in periodically to check status rather than waiting on it (p18).

### Step 2 — credentials, every session

**Confirm the region selector reads `us-east-1` before anything else** (p33).

Expand the sandbox account → **Access keys** (p23) → copy the export block into
your shell. They expire in 12 hours, so expect to redo this. Put them in the
environment or a git-ignored `.env` (p25) — never in the image, never in a
Lambda environment variable.

### Step 3 — cost guardrails, before the first deploy

Do this first, not last.

1. **AWS Budgets alarm at $8**, with email. Well under the $12 warning and the
   $20 revocation, because the console bar lags by hours (p28).
2. Confirm the kill switch works: setting `LLM_PROVIDER=null` on the Lambda
   degrades to the deterministic path with no redeploy. The shortlist, course
   loads and citations all still work — this is how the whole product was
   built.

### Step 4 — enable Bedrock model access

In the Bedrock console, **us-east-1**, request access to Claude Haiku 4.5 if it
is not already enabled. Then verify from the CLI before writing any code
against it:

```bash
aws bedrock list-foundation-models --region us-east-1 \
  --query "modelSummaries[?contains(modelId,'haiku')].modelId"
```

### Step 5 — DynamoDB tables

```bash
aws dynamodb create-table --table-name nex-sessions \
  --attribute-definitions AttributeName=session_id,AttributeType=S \
  --key-schema AttributeName=session_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST --region us-east-1
```

Repeat for `nex-chats` and `nex-cache`, then enable TTL on `nex-cache`:

```bash
aws dynamodb update-time-to-live --table-name nex-cache \
  --time-to-live-specification "Enabled=true,AttributeName=expires_at" \
  --region us-east-1
```

### Step 6 — push the image to ECR

```bash
aws ecr create-repository --repository-name nex-backend --region us-east-1
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin <acct>.dkr.ecr.us-east-1.amazonaws.com
docker build -t nex-backend .
docker tag nex-backend:latest <acct>.dkr.ecr.us-east-1.amazonaws.com/nex-backend:latest
docker push <acct>.dkr.ecr.us-east-1.amazonaws.com/nex-backend:latest
```

The push is 2–3 GB. Do it on a connection you trust, and watch the 12-hour key
expiry — a push that outlives the credentials fails partway.

### Step 7 — the Lambda function

Create from the container image, with:

- **Memory 2048 MB** — CPU scales with memory, and the ONNX embedding model
  needs it. Test 3008 MB too; faster can be *cheaper* because you are billed
  per GB-second.
- **Timeout 120 s** — just above the app's own 110 s ceiling.
- **Ephemeral storage 512 MB** (default) is enough; nothing writes large files.
- **Execution role** with `AmazonBedrockFullAccess` (or an inline policy for
  `bedrock:InvokeModel` / `InvokeModelWithResponseStream`) and DynamoDB
  read/write on the three tables. Nothing else.
- **No VPC.** Attaching one forces a NAT Gateway for egress, which is ~$1/day
  and explicitly called out on p30.

Then the Function URL:

```bash
aws lambda create-function-url-config --function-name nex-backend \
  --auth-type AWS_IAM --invoke-mode RESPONSE_STREAM --region us-east-1
```

Use `AWS_IAM` auth and let CloudFront sign the requests (OAC), so the URL is
not openly callable. `NONE` is simpler but leaves an unauthenticated endpoint
that anyone can bill you for.

### Step 8 — frontend to S3 + CloudFront

```bash
cd frontend && npm run build          # with output: "export"
aws s3 sync out/ s3://<bucket>/ --region us-east-1
```

CloudFront distribution with two origins:

| Behaviour | Origin | Notes |
|---|---|---|
| `/backend/*` | Lambda Function URL | Attach a CloudFront **Function** on viewer-request that strips the `/backend` prefix, since FastAPI serves `/api/*`. Forward all headers, no caching. |
| `/*` (default) | S3 (via OAC) | Cache normally. |

Keep the S3 bucket private and reach it through Origin Access Control.

### Step 9 — verify, in this order

```bash
curl https://<distribution>/backend/api/health
```

Expect `status: "ok"`, real row counts, `llm_provider: "bedrock"`,
`general_questions_chroma_chunks: 45`. Then a shortlist question through the
UI, then one question that actually calls Bedrock, then check the budget bar.

**Warm it before demoing.** A 2–3 GB image has a slow first cold start. Hit
`/api/health` a few minutes before you present.

---

## 6. What will cost you money

Nothing here is always-on, which is the whole point. Realistic demo spend is
low single-digit dollars, dominated by:

- **Bedrock tokens** — the dominant variable cost, and the only one that scales
  with use. The durable cache in `services/cache.py` means a repeated demo
  costs almost nothing.
- **ECR storage** — ~$0.10/GB-month, so ~$0.30 for a 3 GB image.
- **Lambda, DynamoDB, S3, CloudFront** — pennies at demo volume.

Standing controls: the $8 Budgets alarm, the `LLM_PROVIDER=null` kill switch,
the reflect-loop cap already held in state, and prompt caching on the system
prompt (supported on Bedrock; worth it above ~1k tokens).

---

## 7. Teardown

`destroy` leaves **S3 buckets, ECR repositories and CloudWatch log groups**
behind. Before the account is frozen or deleted:

- push all code to your own repository;
- download anything you need to keep;
- empty and delete the S3 buckets and ECR repositories by hand;
- delete the CloudWatch log groups.

An orphaned 3 GB ECR repository quietly bills against a lease you can never
replace.

---

## 8. What this design deliberately does not do

- **No S3 Vectors, no vector service.** 45 chunks do not need one. If the image
  size becomes the problem, that is when to revisit it.
- **No always-on compute.** An idle weekend costs nothing.
- **No fine-tuning.** Correctness here comes from evidence extraction and the
  source policy, not from model knowledge.
- **No openserp in the cloud.** The research lane declines instead, which is a
  designed outcome rather than a gap.
