"""Find the Bedrock configuration this account will actually accept.

Run this after pulling fresh access keys from the portal, and before creating
any AWS resource. It answers, in the order they fail:

  1. Do credentials resolve? (they expire every 12 hours)
  2. Which region serves Bedrock for this account?
  3. Which Claude model ids and inference profiles does it expose?
  4. Which API surface is permitted - and which id actually answers?

Step 4 exists because the hackathon organisation's service control policy
carries an explicit deny on ``bedrock-mantle:CreateInference``. An SCP deny is
set above the account and cannot be granted around from inside it, so the
newer Messages-API surface is simply closed here and the classic
``bedrock-runtime`` InvokeModel path is the one to use. Rather than assume
that, this script tries each combination and reports the first that works.

Usage, from the repository root:

    backend/.venv/Scripts/python.exe tools/verify_bedrock.py [region]

Exit code is 0 only when a real completion came back.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

# us-east-1 first, per the access guide; ap-southeast-1 probed as a fallback
# because "wrong region" and "no model access" look identical otherwise.
CANDIDATE_REGIONS = ("us-east-1", "ap-southeast-1")

# Ordering matters more than it looks. A bare foundation-model id returns 400
# ("on-demand throughput isn't supported, use an inference profile"), and the
# organisation's SCP denies the `global.` cross-region profiles outright. The
# `us.` regional profiles are the ones that work, so try those first.
PREFERRED = (
    "us.anthropic.claude-haiku-4-5",
    "us.anthropic.claude-haiku",
    "haiku-4-5",
    "haiku",
)


def _bedrock(region: str, service: str = "bedrock"):
    import boto3

    return boto3.client(service, region_name=region)


def list_models(region: str) -> tuple[list[str], str | None]:
    """Claude foundation-model ids in one region, or the error that stopped us."""
    try:
        summaries = _bedrock(region).list_foundation_models().get("modelSummaries", [])
    except Exception as exc:  # noqa: BLE001 - the error is the diagnostic
        return [], f"{exc.__class__.__name__}: {exc}"
    ids = [str(item.get("modelId", "")) for item in summaries]
    return sorted({mid for mid in ids if "claude" in mid.lower()}), None


def list_profiles(region: str) -> list[str]:
    """Cross-region inference profiles, which newer models often require."""
    try:
        page = _bedrock(region).list_inference_profiles()
    except Exception:  # noqa: BLE001 - profiles are a bonus, not a requirement
        return []
    out = []
    for item in page.get("inferenceProfileSummaries", []):
        pid = str(item.get("inferenceProfileId", ""))
        if "claude" in pid.lower():
            out.append(pid)
    return sorted(set(out))


def rank(candidates: list[str]) -> list[str]:
    """Try the cheapest, most likely models first."""
    def score(mid: str) -> tuple[int, str]:
        low = mid.lower()
        for index, marker in enumerate(PREFERRED):
            if marker in low:
                return (index, mid)
        return (len(PREFERRED), mid)

    return sorted(set(candidates), key=score)


def try_call(region: str, api: str, model: str) -> tuple[bool, str]:
    """One real completion. Returns (ok, detail)."""
    from graph.providers import LLMRequest, _bedrock_client, _anthropic_text

    try:
        client = _bedrock_client(region, api)
        response = client.messages.create(
            model=model,
            max_tokens=16,
            messages=[{"role": "user", "content": "Say OK."}],
        )
    except Exception as exc:  # noqa: BLE001 - the error is the result
        message = str(exc)
        if "service control policy" in message or "explicit deny" in message:
            # Which SCP deny this is matters. A deny naming the *service*
            # closes the whole surface; a deny on one model id (the `global.`
            # cross-region profiles are denied here) says nothing about the
            # others, and treating it as fatal skips ids that do work.
            if "bedrock-mantle" in message:
                return False, "SCP deny: bedrock-mantle service closed"
            return False, "SCP deny for this model id only"
        if "AccessDenied" in exc.__class__.__name__ or "403" in message[:80]:
            return False, "access denied"
        if "ValidationException" in message or "not supported" in message:
            return False, "invalid id for this surface"
        return False, f"{exc.__class__.__name__}: {message[:90]}"
    _ = LLMRequest, _anthropic_text  # imported for the caller's benefit
    text = "".join(
        getattr(b, "text", "") for b in getattr(response, "content", []) or []
        if getattr(b, "type", None) == "text"
    )
    usage = getattr(response, "usage", None)
    tokens = ""
    if usage is not None:
        tokens = (f", tokens in {getattr(usage, 'input_tokens', 0)} "
                  f"out {getattr(usage, 'output_tokens', 0)}")
    return True, f"{text.strip()[:40]!r}{tokens}"


def main() -> int:
    from graph.providers import BedrockProvider

    provider = BedrockProvider()
    override = sys.argv[1] if len(sys.argv) > 1 else ""
    region = override or provider.region()

    print(f"configured region   {region}")
    print(f"configured model    {provider.default_model()}")
    print(f"configured surface  {provider.api()}")

    if not provider.available():
        print("\nFAIL  No AWS credentials resolved.")
        print("      Keys expire every 12 hours. In PowerShell the portal's")
        print("      bash 'export ...' lines do nothing - use the PowerShell tab,")
        print("      or set $env:AWS_ACCESS_KEY_ID / _SECRET_ACCESS_KEY /")
        print("      _SESSION_TOKEN by hand. All three are required.")
        return 1
    print("credentials         resolved")

    # --- which region, and what does it hold? -------------------------------
    print("\nprobing regions (listing is free):")
    found: dict[str, list[str]] = {}
    for candidate in dict.fromkeys((region, *CANDIDATE_REGIONS)):
        models, error = list_models(candidate)
        if error:
            print(f"  {candidate:<16} error  {error[:66]}")
            continue
        found[candidate] = models
        print(f"  {candidate:<16} {len(models)} Claude model(s)")

    working = [name for name, models in found.items() if models]
    if not working:
        print("\nFAIL  No region returned a Claude model. Request model access in")
        print("      the Bedrock console and re-run.")
        return 1
    if region not in working:
        print(f"\n  NOTE  '{region}' is empty but {working[0]} is not; using that.")
        region = working[0]

    profiles = list_profiles(region)
    print(f"\nregion in use       {region}")
    print("foundation models:")
    for mid in found[region]:
        print(f"  {mid}")
    if profiles:
        print("inference profiles:")
        for pid in profiles:
            print(f"  {pid}")

    # --- which surface and id actually answer? ------------------------------
    candidates = rank([*profiles, *found[region]])
    print(f"\ntrying {len(candidates)} candidate id(s) against each API surface.")
    print("The first success wins; each attempt is a ~10 token call.\n")

    scp_blocked = set()
    for api in ("runtime", "mantle"):
        for model in candidates:
            ok, detail = try_call(region, api, model)
            status = "OK  " if ok else "fail"
            print(f"  [{api:<7}] {status} {model}\n            {detail}")
            if ok:
                print("\nPASS  Bedrock answered.")
                print("      Put these in backend/.env:")
                print(f"        BEDROCK_REGION={region}")
                print(f"        BEDROCK_API={api}")
                print(f"        BEDROCK_MODEL_ID={model}")
                print("\n      Then continue at Step 5 (DynamoDB tables) in")
                print("      docs/AWS_DEPLOYMENT.md, creating them in this same")
                print("      region or the Lambda will not find them.")
                return 0
            if "service closed" in detail:
                # Only a service-level deny justifies abandoning the surface.
                # A per-model deny must not stop the loop: the id that works
                # may well sort after the one that does not.
                scp_blocked.add(api)
                break

    print("\nFAIL  Nothing answered.")
    if scp_blocked:
        print(f"      Surface(s) blocked by service control policy: "
              f"{', '.join(sorted(scp_blocked))}.")
        print("      That deny is set above your account by the organisers, so")
        print("      it is not something you can grant yourself. If every")
        print("      surface is blocked, ask them to permit bedrock:InvokeModel")
        print("      (and InvokeModelWithResponseStream) for the hackathon role.")
    print("\n      Meanwhile the app still runs: LLM_PROVIDER=null keeps every")
    print("      deterministic lane working, which is the documented fallback.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
