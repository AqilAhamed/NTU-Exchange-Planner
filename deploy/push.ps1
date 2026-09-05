# Build and push the backend image to ECR.
#
# Runs on your laptop, not CloudShell: the image is ~1.3 GB and CloudShell only
# has 1 GB of storage. Needs Docker running and AWS keys in this shell.
#
#   .\deploy\push.ps1 -EcrUri <uri from provision.sh>
#
# Pull fresh keys immediately before running this. The upload takes a while,
# and keys expire every 12 hours - one that dies mid-push wastes the transfer.

param(
    [Parameter(Mandatory = $true)][string]$EcrUri,
    [string]$Region = "us-east-1",
    [string]$Tag = "latest"
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "==> checking credentials" -ForegroundColor Cyan
$identity = aws sts get-caller-identity --query Arn --output text
if ($LASTEXITCODE -ne 0) {
    Write-Host "No working AWS credentials in this shell." -ForegroundColor Red
    Write-Host 'Set $env:AWS_ACCESS_KEY_ID, $env:AWS_SECRET_ACCESS_KEY and'
    Write-Host '$env:AWS_SESSION_TOKEN (all three) from the access portal.'
    exit 1
}
Write-Host "    $identity"

Write-Host "==> building (linux/amd64)" -ForegroundColor Cyan
docker build -t nex-backend:$Tag .
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "==> logging in to ECR" -ForegroundColor Cyan
$registry = $EcrUri.Split("/")[0]
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $registry
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "==> pushing ~1.3 GB, this is the slow part" -ForegroundColor Cyan
docker tag nex-backend:$Tag "${EcrUri}:$Tag"
docker push "${EcrUri}:$Tag"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Push failed. If it got part way and then errored on auth," -ForegroundColor Red
    Write-Host "the 12-hour keys expired - pull fresh ones and re-run." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Pushed ${EcrUri}:$Tag" -ForegroundColor Green
Write-Host "Next: run deploy/deploy-lambda.sh in CloudShell with this URI."
