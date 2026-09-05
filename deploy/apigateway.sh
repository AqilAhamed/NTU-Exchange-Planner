#!/usr/bin/env bash
# Replace the Lambda Function URL origin with an API Gateway HTTP API.
#
# Why: this organisation's service control policy denies
# lambda:InvokeFunctionUrl for the CloudFront service principal. Proven, not
# guessed - an IAM-signed request from our own identity returns 200 while
# CloudFront's OAC-signed request to the same URL returns 403, with a correct
# resource policy in place. An SCP sits above the account, so no CloudFront or
# IAM change can fix it.
#
# API Gateway integrates with lambda:InvokeFunction instead, which this account
# demonstrably permits.
#
# Run in CloudShell:  bash apigateway.sh
set -euo pipefail

REGION="${REGION:-us-east-1}"
FUNCTION="${FUNCTION:-nex-backend}"
DIST_ID="${DIST_ID:-E3EBQTLNDXZBV}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
FN_ARN="arn:aws:lambda:$REGION:$ACCOUNT:function:$FUNCTION"

# --- the HTTP API ---------------------------------------------------------
# --target does four things at once: an AWS_PROXY integration, a $default
# route, a $default stage with auto-deploy (so there is no /stage prefix in the
# path), and the lambda:InvokeFunction permission.
API_ID=$(aws apigatewayv2 get-apis --region "$REGION" \
  --query "Items[?Name=='nex-api'].ApiId | [0]" --output text 2>/dev/null || true)
if [ "$API_ID" = "None" ] || [ -z "$API_ID" ]; then
  API_ID=$(aws apigatewayv2 create-api --name nex-api --protocol-type HTTP \
    --target "$FN_ARN" --region "$REGION" --query ApiId --output text)
  echo "created API $API_ID"
else
  echo "reusing API $API_ID"
fi
API_HOST="$API_ID.execute-api.$REGION.amazonaws.com"
echo "api origin: $API_HOST"

# --- the adapter's mode ---------------------------------------------------
# The image sets AWS_LWA_INVOKE_MODE=response_stream for Function URLs. API
# Gateway does not stream, and a Lambda environment variable overrides the
# image's ENV - so this is a config change, not a 1.3 GB rebuild and re-push.
CURRENT=$(aws lambda get-function-configuration --function-name "$FUNCTION" \
  --region "$REGION" --query 'Environment.Variables' --output json)
UPDATED=$(python3 - "$CURRENT" <<'PY'
import json, sys
env = json.loads(sys.argv[1])
env["AWS_LWA_INVOKE_MODE"] = "buffered"
print(json.dumps({"Variables": env}))
PY
)
aws lambda update-function-configuration --function-name "$FUNCTION" \
  --environment "$UPDATED" --region "$REGION" >/dev/null
aws lambda wait function-updated --function-name "$FUNCTION" --region "$REGION"
echo "adapter set to buffered"

# --- a cost guardrail -----------------------------------------------------
# The API Gateway endpoint is public. Reserved concurrency caps how much
# damage anyone who finds it can do to a $20 budget.
aws lambda put-function-concurrency --function-name "$FUNCTION" \
  --reserved-concurrent-executions 10 --region "$REGION" >/dev/null
echo "reserved concurrency capped at 10"

# --- point CloudFront at it ----------------------------------------------
aws cloudfront get-distribution-config --id "$DIST_ID" > /tmp/dist-current.json
ETAG=$(python3 -c "import json;print(json.load(open('/tmp/dist-current.json'))['ETag'])")

python3 - "$API_HOST" <<'PY'
import json, sys
host = sys.argv[1]
doc = json.load(open('/tmp/dist-current.json'))
cfg = doc['DistributionConfig']
for origin in cfg['Origins']['Items']:
    if origin['Id'] == 'lambda-api':
        origin['DomainName'] = host
        # No OAC: API Gateway is reached unsigned. The OAC is what the SCP
        # was refusing, so leaving it attached would reproduce the 403.
        origin.pop('OriginAccessControlId', None)
        origin['OriginAccessControlId'] = ''
json.dump(cfg, open('/tmp/dist-new.json', 'w'), indent=2)
PY

aws cloudfront update-distribution --id "$DIST_ID" \
  --distribution-config file:///tmp/dist-new.json --if-match "$ETAG" \
  --query 'Distribution.Status' --output text
echo "distribution updated"

DOMAIN=$(aws cloudfront get-distribution --id "$DIST_ID" --query 'Distribution.DomainName' --output text)
echo
echo "-------------------------------------------------------------"
echo "  https://$DOMAIN"
echo "-------------------------------------------------------------"
echo "Redeploying takes a few minutes. Test the API Gateway origin directly"
echo "straight away - it does not wait for CloudFront:"
echo
echo "  curl https://$API_HOST/api/health"
echo
echo "Then once the distribution says Deployed:"
echo "  curl https://$DOMAIN/backend/api/health"
