# The deployed backend: FastAPI on Lambda, behind a Function URL.
#
# Two things here are not obvious and are load-bearing:
#
# 1. AWS Lambda Web Adapter. Python on Lambda has no native response
#    streaming, so without the adapter the SSE endpoint (/api/chat/stream)
#    could not work and the "watch the router plan, then execute" demo would
#    be lost. The adapter runs this app unchanged and bridges RESPONSE_STREAM.
#
# 2. /var/task is READ-ONLY on Lambda. ChromaDB opens chroma.sqlite3
#    read-write and general_questions_store.client() calls mkdir(), so the
#    Chroma collection and the embedding model are seeded into /tmp on cold
#    start. coursefinder.db is untouched by this: it is opened mode=ro and
#    can stay on the read-only layer.
#
# Platform is pinned because Lambda will not run an arm64 image on an x86_64
# function, and Docker Desktop on an Apple Silicon machine would otherwise
# build one silently.
FROM --platform=linux/amd64 public.ecr.aws/docker/library/python:3.12-slim

COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 \
     /lambda-adapter /opt/extensions/lambda-adapter

ENV AWS_LWA_INVOKE_MODE=response_stream \
    AWS_LWA_PORT=8000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /var/task

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/src ./src
COPY coursefinder.db ./coursefinder.db

# Seed copies. The runtime reads the /tmp copies, not these.
COPY data/general_questions_chroma ./seed/general_questions_chroma
COPY data/general_questions_embedding ./seed/general_questions_embedding

# Search is an outbound HTTPS call, not a sidecar.
#
# This image used to bundle openserp and a 265 MB headless-shell Chromium so
# the research lane had a search engine of its own. That was the right instinct
# - a standalone openserp would mean EC2 or Fargate behind a load balancer,
# three of the access guide's named budget-killers at once - but it cannot work
# on Lambda, and this is measured rather than assumed:
#
#   [diag] shm=df: /dev/shm: No such file or directory
#
# Lambda provides no /dev/shm. Chromium requires shared memory, so openserp
# starts perfectly (Fiber v2.52.13, 68 handlers, listening on :7000) and every
# search then hangs. The symptom is misleading, because SearchProvider.available()
# uses a real search as its probe: the lane reports "no search provider
# connected" while the server is demonstrably healthy. Raising the probe budget
# from 4s to 14s only made the failure slower, which is what proved it.
#
# Tavily replaces it with one HTTPS request: no browser, no sidecar, no
# /dev/shm, and 299 MB less image on a cold start that is already the tightest
# constraint here. The key arrives as a Lambda environment variable, never baked
# in, so it can be rotated without a rebuild and never reaches the registry.
#
# openserp remains selectable (SEARCH_PROVIDER=openserp) and is still the better
# choice on a workstation, where a browser exists and a local instance has no
# per-query cost.
ENV COURSEFINDER_DB=/var/task/coursefinder.db \
    GENERAL_QUESTIONS_CHROMA_DIR=/tmp/nex-data/general_questions_chroma \
    GENERAL_QUESTIONS_EMBEDDING_DIR=/tmp/nex-data/general_questions_embedding \
    LLM_PROVIDER=bedrock \
    SEARCH_PROVIDER=tavily \
    HOME=/tmp \
    STORAGE_BACKEND=dynamodb \
    AWS_REGION=us-east-1

EXPOSE 8000

# The seed copy is guarded so a warm container does not repeat it. Written
# inline rather than as a shell script on purpose: a .sh file authored on
# Windows picks up CRLF line endings, and `exec format error` inside a Lambda
# cold start is a genuinely miserable thing to debug.
# With the search sidecar gone, uvicorn is PID 1 directly, which is what the
# Lambda Web Adapter wants: it waits on the HTTP port, and the container's life
# should follow the web app and nothing else.
CMD ["/bin/sh", "-c", "\
if [ ! -d /tmp/nex-data/general_questions_chroma ]; then \
  mkdir -p /tmp/nex-data && \
  cp -r /var/task/seed/general_questions_chroma /tmp/nex-data/ && \
  cp -r /var/task/seed/general_questions_embedding /tmp/nex-data/; \
fi; \
exec uvicorn api.chat:app --app-dir /var/task/src --host 0.0.0.0 --port ${AWS_LWA_PORT:-8000}"]
