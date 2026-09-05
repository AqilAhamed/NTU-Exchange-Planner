#!/usr/bin/env bash
# One CloudFront distribution in front of both halves of the app.
#
#   /*          -> S3 (the static Next.js export), read via Origin Access Control
#   /backend/*  -> the Lambda Function URL, signed via Origin Access Control
#
# Serving both from one domain is what lets `API = "/backend"` in
# frontend/lib/api.ts stay exactly as it is: the browser only ever talks to its
# own origin, so there is no CORS to configure and no URL to inject at build
# time. A CloudFront Function strips the /backend prefix before the request
# reaches FastAPI, which serves /api/*.
#
# Run in CloudShell:  bash cloudfront.sh
# Idempotent enough to re-run; it reuses anything already created.
set -euo pipefail

REGION="${REGION:-us-east-1}"
BUCKET="${BUCKET:-nex-ui-400200465364}"
FUNCTION="${FUNCTION:-nex-backend}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

LAMBDA_URL=$(aws lambda get-function-url-config --function-name "$FUNCTION" \
  --region "$REGION" --query FunctionUrl --output text)
LAMBDA_HOST=$(echo "$LAMBDA_URL" | sed -e 's|^https://||' -e 's|/$||')
echo "lambda origin: $LAMBDA_HOST"
echo "s3 origin:     $BUCKET.s3.$REGION.amazonaws.com"

# --- origin access controls ----------------------------------------------
oac_id() {
  aws cloudfront list-origin-access-controls \
    --query "OriginAccessControlList.Items[?Name=='$1'].Id | [0]" --output text 2>/dev/null
}

S3_OAC=$(oac_id nex-s3-oac)
if [ "$S3_OAC" = "None" ] || [ -z "$S3_OAC" ]; then
  S3_OAC=$(aws cloudfront create-origin-access-control --origin-access-control-config \
    '{"Name":"nex-s3-oac","Description":"S3 UI","OriginAccessControlOriginType":"s3","SigningBehavior":"always","SigningProtocol":"sigv4"}' \
    --query 'OriginAccessControl.Id' --output text)
fi
echo "s3 OAC:     $S3_OAC"

L_OAC=$(oac_id nex-lambda-oac)
if [ "$L_OAC" = "None" ] || [ -z "$L_OAC" ]; then
  L_OAC=$(aws cloudfront create-origin-access-control --origin-access-control-config \
    '{"Name":"nex-lambda-oac","Description":"Lambda URL","OriginAccessControlOriginType":"lambda","SigningBehavior":"always","SigningProtocol":"sigv4"}' \
    --query 'OriginAccessControl.Id' --output text)
fi
echo "lambda OAC: $L_OAC"

# --- the path-rewriting function -----------------------------------------
# FastAPI serves /api/*, the browser asks for /backend/api/*. Strip the prefix
# here rather than adding a second route prefix in the app, so the app is
# identical locally and deployed.
cat > /tmp/strip-backend.js <<'JS'
function handler(event) {
    var request = event.request;
    if (request.uri.indexOf('/backend') === 0) {
        request.uri = request.uri.substring('/backend'.length);
        if (request.uri === '') { request.uri = '/'; }
    }
    return request;
}
JS

if ! aws cloudfront describe-function --name nex-strip-backend >/dev/null 2>&1; then
  aws cloudfront create-function --name nex-strip-backend \
    --function-config Comment="strip /backend prefix",Runtime=cloudfront-js-2.0 \
    --function-code fileb:///tmp/strip-backend.js >/dev/null
fi
ETAG=$(aws cloudfront describe-function --name nex-strip-backend --query ETag --output text)
aws cloudfront publish-function --name nex-strip-backend --if-match "$ETAG" >/dev/null 2>&1 || true
FN_ARN=$(aws cloudfront describe-function --name nex-strip-backend \
  --query 'FunctionSummary.FunctionMetadata.FunctionARN' --output text)
echo "function:   $FN_ARN"

# --- the distribution -----------------------------------------------------
# Managed policy ids, stable across accounts:
#   CachingOptimized           658327ea-f89d-4fab-a63d-7e88639e58f6
#   CachingDisabled            4135ea2d-6df8-44a3-9df3-4b5a84be39ad
#   AllViewerExceptHostHeader  b689b0a8-53d0-40ab-baf2-68738e2966ac
#
# AllViewerExceptHostHeader is required on the Lambda behaviour: a Function URL
# signs against its own host, so forwarding the CloudFront Host header breaks
# SigV4 with a 403 that looks like a permissions problem.
cat > /tmp/nex-dist.json <<JSON
{
  "CallerReference": "nex-$(date +%s)",
  "Comment": "NTU Exchange Planner",
  "Enabled": true,
  "DefaultRootObject": "index.html",
  "PriceClass": "PriceClass_100",
  "Origins": {
    "Quantity": 2,
    "Items": [
      {
        "Id": "s3-ui",
        "DomainName": "$BUCKET.s3.$REGION.amazonaws.com",
        "OriginAccessControlId": "$S3_OAC",
        "S3OriginConfig": { "OriginAccessIdentity": "" }
      },
      {
        "Id": "lambda-api",
        "DomainName": "$LAMBDA_HOST",
        "OriginAccessControlId": "$L_OAC",
        "CustomOriginConfig": {
          "HTTPPort": 80,
          "HTTPSPort": 443,
          "OriginProtocolPolicy": "https-only",
          "OriginSslProtocols": { "Quantity": 1, "Items": ["TLSv1.2"] },
          "OriginReadTimeout": 60,
          "OriginKeepaliveTimeout": 5
        }
      }
    ]
  },
  "DefaultCacheBehavior": {
    "TargetOriginId": "s3-ui",
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": {
      "Quantity": 2, "Items": ["GET", "HEAD"],
      "CachedMethods": { "Quantity": 2, "Items": ["GET", "HEAD"] }
    },
    "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
    "Compress": true
  },
  "CacheBehaviors": {
    "Quantity": 1,
    "Items": [
      {
        "PathPattern": "/backend/*",
        "TargetOriginId": "lambda-api",
        "ViewerProtocolPolicy": "https-only",
        "AllowedMethods": {
          "Quantity": 7,
          "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
          "CachedMethods": { "Quantity": 2, "Items": ["GET", "HEAD"] }
        },
        "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
        "OriginRequestPolicyId": "b689b0a8-53d0-40ab-baf2-68738e2966ac",
        "Compress": true,
        "FunctionAssociations": {
          "Quantity": 1,
          "Items": [{ "EventType": "viewer-request", "FunctionARN": "$FN_ARN" }]
        }
      }
    ]
  }
}
JSON

DIST_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='NTU Exchange Planner'].Id | [0]" --output text 2>/dev/null || true)
if [ "$DIST_ID" = "None" ] || [ -z "$DIST_ID" ]; then
  DIST_ID=$(aws cloudfront create-distribution --distribution-config file:///tmp/nex-dist.json \
    --query 'Distribution.Id' --output text)
  echo "created distribution $DIST_ID"
else
  echo "reusing distribution $DIST_ID"
fi

DIST_ARN="arn:aws:cloudfront::$ACCOUNT:distribution/$DIST_ID"
DOMAIN=$(aws cloudfront get-distribution --id "$DIST_ID" --query 'Distribution.DomainName' --output text)

# --- let CloudFront read the bucket and invoke the function ---------------
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Effect\": \"Allow\",
    \"Principal\": { \"Service\": \"cloudfront.amazonaws.com\" },
    \"Action\": \"s3:GetObject\",
    \"Resource\": \"arn:aws:s3:::$BUCKET/*\",
    \"Condition\": { \"StringEquals\": { \"AWS:SourceArn\": \"$DIST_ARN\" } }
  }]
}"
echo "bucket policy set"

aws lambda add-permission --function-name "$FUNCTION" \
  --statement-id cloudfront-oac --action lambda:InvokeFunctionUrl \
  --principal cloudfront.amazonaws.com --source-arn "$DIST_ARN" \
  --function-url-auth-type AWS_IAM --region "$REGION" >/dev/null 2>&1 \
  && echo "lambda permission added" || echo "lambda permission already present"

echo
echo "-------------------------------------------------------------"
echo "  https://$DOMAIN"
echo "-------------------------------------------------------------"
echo "Deploying takes 5-15 minutes. Watch it with:"
echo "  aws cloudfront get-distribution --id $DIST_ID --query Distribution.Status --output text"
echo
echo "When it says Deployed, check the API path first:"
echo "  curl https://$DOMAIN/backend/api/health"
