#!/usr/bin/env bash
# Provision everything the Lambda needs, except the Lambda itself.
#
# Run this in CloudShell: it already has credentials, so there is nothing to
# paste and nothing to expire mid-command.
#
#   bash provision.sh
#
# Idempotent - safe to re-run after a partial failure. Nothing here costs money:
# DynamoDB on-demand bills per request, an empty ECR repository is free, and IAM
# is free.
set -euo pipefail

REGION="${REGION:-us-east-1}"
ROLE_NAME="${ROLE_NAME:-nex-backend-lambda-role}"
REPO_NAME="${REPO_NAME:-nex-backend}"

echo "region: $REGION"

# --- DynamoDB -------------------------------------------------------------
# PAY_PER_REQUEST is not optional: provisioned capacity is a named budget
# killer in the access guide.
for table in nex-sessions nex-chats; do
  if aws dynamodb describe-table --table-name "$table" --region "$REGION" >/dev/null 2>&1; then
    echo "table $table already exists"
  else
    aws dynamodb create-table --table-name "$table" \
      --attribute-definitions AttributeName=session_id,AttributeType=S \
      --key-schema AttributeName=session_id,KeyType=HASH \
      --billing-mode PAY_PER_REQUEST --region "$REGION" >/dev/null
    echo "created $table"
  fi
done

if aws dynamodb describe-table --table-name nex-cache --region "$REGION" >/dev/null 2>&1; then
  echo "table nex-cache already exists"
else
  aws dynamodb create-table --table-name nex-cache \
    --attribute-definitions AttributeName=key,AttributeType=S \
    --key-schema AttributeName=key,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST --region "$REGION" >/dev/null
  echo "created nex-cache"
fi

echo "waiting for tables to become ACTIVE..."
for table in nex-sessions nex-chats nex-cache; do
  aws dynamodb wait table-exists --table-name "$table" --region "$REGION"
done

# The cache relies on this. purge_expired() is deliberately a no-op on
# DynamoDB, so without the TTL attribute expired rows accumulate forever.
aws dynamodb update-time-to-live --table-name nex-cache \
  --time-to-live-specification "Enabled=true,AttributeName=expires_at" \
  --region "$REGION" >/dev/null 2>&1 || echo "TTL already enabled"
echo "TTL set on nex-cache"

# --- ECR ------------------------------------------------------------------
REPO_URI=$(aws ecr describe-repositories --repository-names "$REPO_NAME" \
  --region "$REGION" --query "repositories[0].repositoryUri" --output text 2>/dev/null || true)
if [ -z "$REPO_URI" ] || [ "$REPO_URI" = "None" ]; then
  REPO_URI=$(aws ecr create-repository --repository-name "$REPO_NAME" \
    --region "$REGION" --query "repository.repositoryUri" --output text)
  echo "created ECR repository"
fi
echo "ECR URI: $REPO_URI"

# --- IAM ------------------------------------------------------------------
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "role $ROLE_NAME already exists"
else
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"
      }]
    }' >/dev/null
  echo "created role $ROLE_NAME"
fi

# CloudWatch Logs only. Everything else is granted narrowly below.
aws iam attach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# Least privilege on purpose: the three tables by name, and Bedrock invoke on
# the us. regional inference profiles plus their underlying foundation models.
# An inference profile call is authorised against BOTH resources, so listing
# only the profile ARN produces a confusing AccessDenied.
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name nex-backend-access \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      {
        \"Effect\": \"Allow\",
        \"Action\": [\"bedrock:InvokeModel\", \"bedrock:InvokeModelWithResponseStream\"],
        \"Resource\": [
          \"arn:aws:bedrock:*::foundation-model/*\",
          \"arn:aws:bedrock:$REGION:$ACCOUNT:inference-profile/*\"
        ]
      },
      {
        \"Effect\": \"Allow\",
        \"Action\": [
          \"dynamodb:GetItem\", \"dynamodb:PutItem\", \"dynamodb:UpdateItem\",
          \"dynamodb:DeleteItem\", \"dynamodb:Scan\", \"dynamodb:BatchWriteItem\"
        ],
        \"Resource\": [
          \"arn:aws:dynamodb:$REGION:$ACCOUNT:table/nex-sessions\",
          \"arn:aws:dynamodb:$REGION:$ACCOUNT:table/nex-chats\",
          \"arn:aws:dynamodb:$REGION:$ACCOUNT:table/nex-cache\"
        ]
      }
    ]
  }"
echo "attached nex-backend-access policy"

ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query "Role.Arn" --output text)

echo
echo "-------------------------------------------------------------"
echo "Provisioned. Carry these two into the next steps:"
echo "  ECR_URI=$REPO_URI"
echo "  ROLE_ARN=$ROLE_ARN"
echo "-------------------------------------------------------------"
echo "Next: push the image from your laptop (deploy/push.ps1), then"
echo "run deploy/deploy-lambda.sh back here."
