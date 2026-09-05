#!/usr/bin/env bash
# Publish the built static UI to S3 and invalidate CloudFront.
#
# The backend and the UI are deployed by different routes: the backend rides in
# the container image, the UI is 20 static files in an S3 bucket that CloudFront
# fronts with an Origin Access Control. Pushing a new image therefore does NOT
# update the interface, and a deploy that skips this step leaves the old UI
# calling the new API - which mostly works, right up to the moment a changed
# response shape reaches a component that predates it.
#
# Run from the repository root, after `npm run build:static` in frontend/:
#
#   bash deploy/push-ui.sh                 # defaults to frontend/out
#   bash deploy/push-ui.sh path/to/out
#
# Needs the AWS CLI and working credentials. Either works:
#   - on the laptop, with keys exported from the access portal; or
#   - in CloudShell, after uploading frontend/out (it is under 1 MB).
set -euo pipefail

SRC="${1:-frontend/out}"
BUCKET="${BUCKET:-nex-ui-400200465364}"
DIST_ID="${DIST_ID:-E3EBQTLNDXZBV}"
REGION="${REGION:-us-east-1}"

[ -d "$SRC" ] || { echo "no such directory: $SRC" >&2; exit 1; }
# A Next export always has an index.html at its root. Without this guard a
# mistyped path syncs an empty or wrong directory and --delete empties the
# bucket, taking the live site down.
[ -f "$SRC/index.html" ] || { echo "$SRC has no index.html - is that really the export?" >&2; exit 1; }

echo "==> checking credentials"
aws sts get-caller-identity --query Arn --output text

echo "==> syncing hashed assets (immutable)"
# Everything under _next/ carries a content hash in its filename, so it can be
# cached forever. Handled as its own prefix so the --delete below cannot reach
# across and remove the other half mid-deploy.
aws s3 sync "$SRC/_next" "s3://$BUCKET/_next" --delete \
  --cache-control "public,max-age=31536000,immutable" --only-show-errors

echo "==> syncing HTML and root files (no-cache)"
# HTML must not be cached: a stale page references chunk hashes that the sync
# above has just deleted, which is a blank screen rather than an old screen.
aws s3 sync "$SRC" "s3://$BUCKET" --delete --exclude "_next/*" \
  --cache-control "no-cache" --only-show-errors

echo "==> invalidating CloudFront"
ID=$(aws cloudfront create-invalidation --distribution-id "$DIST_ID" \
  --paths "/*" --query 'Invalidation.Id' --output text)
echo "    invalidation $ID created (takes a minute or two)"

echo
echo "Published $(find "$SRC" -type f | wc -l) files to s3://$BUCKET"
