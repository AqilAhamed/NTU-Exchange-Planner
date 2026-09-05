#!/usr/bin/env bash
# Create (or update) the Lambda function and its streaming Function URL.
#
# Run in CloudShell, after deploy/push.ps1 has put the image in ECR:
#
#   bash deploy-lambda.sh <ECR_URI> <ROLE_ARN>
#
# Idempotent: re-running updates the function's image and configuration.
set -euo pipefail

ECR_URI="${1:?usage: deploy-lambda.sh <ECR_URI> <ROLE_ARN>}"
ROLE_ARN="${2:?usage: deploy-lambda.sh <ECR_URI> <ROLE_ARN>}"
REGION="${REGION:-us-east-1}"
FUNCTION="${FUNCTION:-nex-backend}"
TAG="${TAG:-latest}"
# Defaults are what the live function actually runs, not what looked right when
# this was written. update-function-configuration overwrites rather than merges,
# so any value omitted here is silently reset - re-running the script with a
# lower number is a downgrade nobody asked for.
MEMORY="${MEMORY:-3008}"
# 512 MB is the Lambda default and is tight once /tmp holds the ~96 MB Chroma
# and embedding seed AND headless Chromium's scratch space. Ephemeral storage
# above 512 MB is billed per GB-second and is a rounding error at demo volume.
EPHEMERAL="${EPHEMERAL:-1024}"
# openserp searches were measured at 10-11s against a 12s cutoff, so these are
# exposed rather than left to the code defaults. Raise them only if the deployed
# measurement calls for it: the whole request still dies at API Gateway's ~30s.
SEARCH_TIMEOUT="${SEARCH_TIMEOUT:-12}"
RESEARCH_MAX="${RESEARCH_MAX:-14}"
# The engine is a Lambda variable, not just an image default, because it is
# the fastest recovery available if a search engine turns out to refuse AWS
# ranges: duckduckgo | bing | ecosia | yandex | baidu, applied without a
# rebuild and without touching the 1.7 GB image.
OPENSERP_ENGINE="${OPENSERP_ENGINE:-duckduckgo}"

# A Lambda environment variable OVERRIDES the image's own ENV. The Dockerfile
# sets SEARCH_PROVIDER=openserp and ships the headless browser to back it, so
# pinning a different value here silently disables the research lane however
# the image is built - a mismatch that has cost hours before. Default to
# matching the image, and allow an explicit override for a deploy that wants
# research off without a rebuild:
#
#   SEARCH_PROVIDER=null bash deploy-lambda.sh <ECR_URI> <ROLE_ARN>
#
# SEARCH_TIMEOUT_SECONDS and RESEARCH_MAX_SECONDS are not set here: their code
# defaults (12s and 14s) are already sized for API Gateway's ~30s ceiling.
SEARCH_PROVIDER="${SEARCH_PROVIDER:-tavily}"
# Read from the caller's shell so it never enters the repository:
#
#   export TAVILY_API_KEY=tvly-...
#   bash deploy/deploy-lambda.sh <ECR_URI> <ROLE_ARN>
#
# With no key the lane declines honestly, which is correct behaviour and not a
# deployment failure - so this warns rather than exits.
TAVILY_API_KEY="${TAVILY_API_KEY:-}"
if [ "$SEARCH_PROVIDER" = "tavily" ] && [ -z "$TAVILY_API_KEY" ]; then
  echo "warning: SEARCH_PROVIDER=tavily but TAVILY_API_KEY is unset - research will decline" >&2
fi

# AWS_LWA_INVOKE_MODE=buffered overrides the image's response_stream. It is not
# optional: API Gateway buffers the response, and an adapter left in streaming
# mode behind it returns a broken response rather than a slow one. Omitting it
# here does not leave it alone - the whole environment map is replaced on every
# deploy, so a missing key silently reverts to the image default.
# AWS_REGION is a RESERVED Lambda variable and cannot be set here - Lambda
# supplies it. The app reads BEDROCK_REGION first for exactly this reason, so
# inference and DynamoDB stay in one region without touching the reserved name.
ENV_VARS="Variables={
LLM_PROVIDER=bedrock,
BEDROCK_API=runtime,
BEDROCK_REGION=$REGION,
BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0,
STORAGE_BACKEND=dynamodb,
AWS_LWA_INVOKE_MODE=buffered,
SEARCH_TIMEOUT_SECONDS=$SEARCH_TIMEOUT,
RESEARCH_MAX_SECONDS=$RESEARCH_MAX,
SEARCH_PROVIDER=$SEARCH_PROVIDER,
OPENSERP_ENGINE=$OPENSERP_ENGINE,
TAVILY_API_KEY=$TAVILY_API_KEY,
TAVILY_SEARCH_DEPTH=basic,
NEX_SESSIONS_TABLE=nex-sessions,
NEX_CHATS_TABLE=nex-chats,
NEX_CACHE_TABLE=nex-cache
}"
ENV_VARS=$(echo "$ENV_VARS" | tr -d '\n ')

if aws lambda get-function --function-name "$FUNCTION" --region "$REGION" >/dev/null 2>&1; then
  echo "updating existing function"
  aws lambda update-function-code --function-name "$FUNCTION" \
    --image-uri "${ECR_URI}:${TAG}" --region "$REGION" >/dev/null
  aws lambda wait function-updated --function-name "$FUNCTION" --region "$REGION"
  aws lambda update-function-configuration --function-name "$FUNCTION" \
    --timeout 120 --memory-size "$MEMORY" --environment "$ENV_VARS" \
    --ephemeral-storage '{"Size":'"$EPHEMERAL"'}' \
    --region "$REGION" >/dev/null
else
  echo "creating function"
  # 2048 MB because CPU scales with memory and the ONNX embedder needs it.
  # Timeout 120s sits just above the app's own 110s ceiling.
  # No --vpc-config on purpose: a VPC forces a NAT Gateway, ~$1/day.
  aws lambda create-function --function-name "$FUNCTION" \
    --package-type Image --code ImageUri="${ECR_URI}:${TAG}" \
    --role "$ROLE_ARN" --timeout 120 --memory-size "$MEMORY" \
    --ephemeral-storage '{"Size":'"$EPHEMERAL"'}' \
    --architectures x86_64 --environment "$ENV_VARS" \
    --region "$REGION" >/dev/null
fi

aws lambda wait function-active-v2 --function-name "$FUNCTION" --region "$REGION"
echo "function is active"

# RESPONSE_STREAM is what keeps /api/chat/stream working through the Lambda
# Web Adapter. AWS_IAM auth means the URL is not openly callable; CloudFront
# signs requests to it with OAC.
if aws lambda get-function-url-config --function-name "$FUNCTION" --region "$REGION" >/dev/null 2>&1; then
  URL=$(aws lambda update-function-url-config --function-name "$FUNCTION" \
    --auth-type AWS_IAM --invoke-mode RESPONSE_STREAM \
    --region "$REGION" --query FunctionUrl --output text)
else
  URL=$(aws lambda create-function-url-config --function-name "$FUNCTION" \
    --auth-type AWS_IAM --invoke-mode RESPONSE_STREAM \
    --region "$REGION" --query FunctionUrl --output text)
fi

echo
echo "-------------------------------------------------------------"
echo "Function URL: $URL"
echo "-------------------------------------------------------------"
echo
echo "It is AWS_IAM protected, so curl will return 403 - that is correct."
echo "To smoke-test the container before wiring CloudFront, temporarily run:"
echo
echo "  aws lambda update-function-url-config --function-name $FUNCTION \\"
echo "    --auth-type NONE --invoke-mode RESPONSE_STREAM --region $REGION"
echo "  aws lambda add-permission --function-name $FUNCTION \\"
echo "    --statement-id public-url --action lambda:InvokeFunctionUrl \\"
echo "    --principal '*' --function-url-auth-type NONE --region $REGION"
echo "  curl \"\${URL}api/health\""
echo
echo "Then put it back to AWS_IAM immediately - an open URL on a \$20 budget"
echo "is someone else's free compute:"
echo
echo "  aws lambda remove-permission --function-name $FUNCTION \\"
echo "    --statement-id public-url --region $REGION"
echo "  aws lambda update-function-url-config --function-name $FUNCTION \\"
echo "    --auth-type AWS_IAM --invoke-mode RESPONSE_STREAM --region $REGION"
