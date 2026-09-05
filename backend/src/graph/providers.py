"""LLM provider boundary.

One interface, three implementations. Groq runs local development, Bedrock is a
declared seam for deployment that deliberately does not work yet, and Null makes
"no API key configured" a first-class supported state rather than a crash.

Nothing above this module imports a vendor SDK, so swapping Groq for Bedrock is
a configuration change rather than a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable

from graph.config import env

DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"

# The briefing is explicit: read model ids from a constant, never build them.
#
# The id shape follows the API surface, and this account is on the classic
# ``bedrock-runtime`` path (see ``_bedrock_client``), which wants the full
# dated id or a cross-region inference profile such as
# ``us.anthropic.claude-haiku-4-5-20251001-v1:0``. The bare
# ``anthropic.claude-haiku-4-5`` form belongs to the Mantle path, which the
# organisation's SCP denies.
#
# Do not take this constant on faith - ``tools/verify_bedrock.py`` enumerates
# what the account actually exposes and calls each candidate until one answers,
# then prints the ``BEDROCK_MODEL_ID`` line to put in ``backend/.env``.
BEDROCK_DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# The access guide's troubleshooting page is explicit that this account lives in
# us-east-1; several "Access denied" bars at once is the documented symptom of
# being in the wrong region. Only a default — BEDROCK_REGION or AWS_REGION
# override it — and `tools/verify_bedrock.py` confirms it against the account.
BEDROCK_DEFAULT_REGION = "us-east-1"

# Every caller here wants a short routing decision or a few sentences of
# synthesis, not an essay. Anthropic requires max_tokens, and a low ceiling is
# also a hard cap on the most expensive half of a $20 budget.
BEDROCK_MAX_TOKENS = 2048


@dataclass(frozen=True)
class LLMRequest:
    """A single completion request.

    ``messages`` is a list of ``(role, content)`` pairs where role is one of
    ``system`` / ``human`` / ``assistant``.
    """

    messages: tuple[tuple[str, str], ...]
    model: str | None = None
    temperature: float = 0.0
    json_mode: bool = False


@dataclass(frozen=True)
class TokenUsage:
    """Usage for one call. Feeds the graded 'token cost per run' metric."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True)
class LLMResponse:
    content: str
    provider: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def available(self) -> bool:
        """True when this provider is configured well enough to be called."""

    def invoke(self, request: LLMRequest) -> LLMResponse:
        """Run one completion. Raises on transport or configuration failure."""


def message_content(raw: Any) -> str:
    """Normalise LangChain's several content shapes down to a plain string."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for chunk in raw:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict):
                text = chunk.get("text") or chunk.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(raw)


def _usage_from_response(response: Any) -> TokenUsage:
    """Pull token counts out of a LangChain response, tolerating any shape."""
    meta = getattr(response, "usage_metadata", None)
    if isinstance(meta, dict) and meta:
        details = meta.get("input_token_details") or {}
        return TokenUsage(
            input_tokens=int(meta.get("input_tokens") or 0),
            output_tokens=int(meta.get("output_tokens") or 0),
            cache_read_tokens=int(details.get("cache_read") or 0),
            cache_write_tokens=int(details.get("cache_creation") or 0),
        )
    response_meta = getattr(response, "response_metadata", None) or {}
    usage = response_meta.get("token_usage") or response_meta.get("usage") or {}
    if isinstance(usage, dict) and usage:
        return TokenUsage(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )
    return TokenUsage()


def _anthropic_text(response: Any) -> str:
    """Concatenate the text blocks of an Anthropic response.

    ``content`` is a list of typed blocks, not a string. Thinking and tool_use
    blocks are skipped rather than stringified, so a model that thinks before
    answering does not smuggle its reasoning into a JSON payload the router
    then tries to parse.
    """
    parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) != "text":
            continue
        text = getattr(block, "text", "")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _usage_from_anthropic(response: Any) -> TokenUsage:
    """Token counts from an Anthropic response, tolerating a missing field."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )


@lru_cache(maxsize=8)
def _groq_client(model: str, temperature: float, json_mode: bool) -> Any:
    """Build (and reuse) a ChatGroq client.

    The cache is keyed only on hashable call parameters, deliberately: caching on
    a provider *instance* silently never hits, because a fresh instance is
    constructed per call and instances compare by identity.
    """
    from langchain_groq import ChatGroq

    kwargs: dict[str, Any] = {"model": model, "temperature": temperature}
    if json_mode:
        kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
    try:
        return ChatGroq(**kwargs)
    except TypeError:
        # Older langchain-groq builds reject model_kwargs; JSON mode then relies
        # on the prompt alone, which the callers already tolerate.
        kwargs.pop("model_kwargs", None)
        return ChatGroq(**kwargs)


# A Groq key is a `gsk_`-prefixed token. Anything else in the variable is a
# placeholder left in from .env.example, and treating it as configured is worse
# than treating it as absent: every LLM call then fails at runtime on a system
# that has already reported itself ready.
_GROQ_KEY_PREFIX = "gsk_"
_PLACEHOLDER_MARKERS = ("your_", "_here", "xxx", "<", "changeme", "placeholder")


class GroqProvider:
    name = "groq"

    def available(self) -> bool:
        key = env("GROQ_API_KEY")
        if not key or not self._is_real_key(key):
            return False
        try:
            import langchain_groq  # noqa: F401
        except ImportError:
            return False
        return True

    @staticmethod
    def _is_real_key(key: str) -> bool:
        """Reject an obvious placeholder rather than reporting it as configured.

        This is a shape check, not authentication: only Groq can say whether a
        well-formed key is valid. It exists because leaving the template value
        in ``.env`` used to make the health endpoint answer ``llm_configured:
        true`` while every call failed.
        """
        lowered = key.lower()
        if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
            return False
        return lowered.startswith(_GROQ_KEY_PREFIX) and len(key) > len(_GROQ_KEY_PREFIX) + 8

    def default_model(self) -> str:
        return env("GROQ_MODEL", DEFAULT_GROQ_MODEL) or DEFAULT_GROQ_MODEL

    def invoke(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self.default_model()
        client = _groq_client(model, request.temperature, request.json_mode)
        response = client.invoke(list(request.messages))
        return LLMResponse(
            content=message_content(getattr(response, "content", "")),
            provider=self.name,
            model=model,
            usage=_usage_from_response(response),
        )


def split_system(
    messages: tuple[tuple[str, str], ...]
) -> tuple[str, list[dict[str, str]]]:
    """Split LangChain-shaped ``(role, content)`` pairs for the Anthropic SDK.

    The rest of this codebase speaks the LangChain dialect, where ``system`` is
    just another role in the message list. The Anthropic SDK takes ``system`` as
    a **top-level parameter** and rejects it as a message role, so the two have
    to be separated here rather than at every call site. Multiple system turns
    are joined; ``human`` maps to ``user``.
    """
    system_parts: list[str] = []
    turns: list[dict[str, str]] = []
    for role, content in messages:
        text = content or ""
        if role == "system":
            if text.strip():
                system_parts.append(text)
            continue
        turns.append({"role": "user" if role == "human" else role, "content": text})
    return "\n\n".join(system_parts), turns


@lru_cache(maxsize=4)
def _bedrock_client(region: str, api: str = "runtime") -> Any:
    """Build (and reuse) a Bedrock client.

    Two API surfaces, because the account decides which one is reachable:

    ``runtime``
        The classic ``bedrock-runtime`` InvokeModel path. The default, and the
        one the hackathon account actually permits.
    ``mantle``
        The newer Messages-API endpoint. Preferred by current SDK guidance for
        new code, but the hackathon organisation's service control policy
        carries an **explicit deny** on ``bedrock-mantle:CreateInference``.
        An SCP deny cannot be granted around from inside the account, so this
        is not a permissions bug to fix — it is a closed door.

    Cached on hashable parameters, for the same reason the Groq client is:
    caching on a provider instance never hits, because a fresh instance is
    built per call and instances compare by identity. On Lambda this also means
    one client per warm container rather than one per request.
    """
    if api == "mantle":
        from anthropic import AnthropicBedrockMantle

        return AnthropicBedrockMantle(aws_region=region)

    from anthropic import AnthropicBedrock

    return AnthropicBedrock(aws_region=region)


class BedrockProvider:
    """Claude on Amazon Bedrock, reached with the account's IAM role.

    There is no API key here by design: on Lambda the execution role carries
    ``bedrock:InvokeModel`` and the SDK resolves credentials from the
    environment, so nothing secret is ever baked into the image.

    Calls the ``bedrock-runtime`` InvokeModel surface, not the newer Mantle
    Messages API. That is not a preference: this organisation's service control
    policy carries an explicit deny on ``bedrock-mantle:CreateInference``, and
    an SCP cannot be granted around from inside the account. ``BEDROCK_API``
    selects between them and defaults to ``runtime`` for that reason; see
    ``_bedrock_client`` for the measurement behind it.

    The runtime surface also dictates the shape of the model id: it needs a
    full id or an inference profile, which is why the constant above carries
    the ``us.`` cross-region prefix and a date suffix.
    """

    name = "bedrock"

    def region(self) -> str:
        return (
            env("BEDROCK_REGION")
            or env("AWS_REGION")
            or env("AWS_DEFAULT_REGION")
            or BEDROCK_DEFAULT_REGION
        )

    def api(self) -> str:
        """Which Bedrock surface to call: ``runtime`` (default) or ``mantle``."""
        return (env("BEDROCK_API", "runtime") or "runtime").strip().lower()

    def available(self) -> bool:
        """True when the SDK is installed, a region resolves, and creds exist.

        The credential check is the same honesty rule ``GroqProvider`` follows:
        reporting "configured" when every call will fail is worse than
        reporting "not configured", because the health endpoint then lies and
        each lane pays a failed call to find out.

        It resolves the local credential chain only — env vars, profile, or the
        Lambda container role — and never calls AWS. Model access is not
        checked: only the account can answer that, and a probe would spend the
        budget this check exists to protect. That failure surfaces at
        ``invoke``, where every lane already degrades to its deterministic path.
        """
        try:
            import anthropic  # noqa: F401
            import botocore.session
        except ImportError:
            return False
        if not self.region():
            return False
        try:
            return botocore.session.get_session().get_credentials() is not None
        except Exception:  # noqa: BLE001 - an unreadable chain is "not configured"
            return False

    def default_model(self) -> str:
        return env("BEDROCK_MODEL_ID", BEDROCK_DEFAULT_MODEL_ID) or BEDROCK_DEFAULT_MODEL_ID

    def invoke(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self.default_model()
        system, turns = split_system(request.messages)

        # ``request.temperature`` is deliberately not forwarded. Sampling
        # parameters were removed from the Anthropic SDK — ``messages.create()``
        # accepts no temperature/top_p/top_k at all — so passing the 0.0 this
        # codebase uses everywhere raises TypeError rather than being ignored.
        # Groq still honours it; on Bedrock, determinism rests on the model
        # default and on the prompts, which are already written to be strict.
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": BEDROCK_MAX_TOKENS,
            "messages": turns,
        }
        if system:
            # Cache the system prompt. It is byte-stable across a turn's lanes
            # and is the single biggest lever on token cost; Bedrock supports
            # 5-minute prompt caching.
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        if request.json_mode:
            # Bedrock has no JSON mode flag. Every caller here already parses
            # defensively and falls back when parsing fails, so reinforcing the
            # instruction is enough and keeps the request shape portable.
            kwargs["messages"] = turns + [
                {"role": "user", "content": "Respond with only the JSON object, no prose."}
            ]

        response = _bedrock_client(self.region(), self.api()).messages.create(**kwargs)
        return LLMResponse(
            content=_anthropic_text(response),
            provider=self.name,
            model=model,
            usage=_usage_from_anthropic(response),
        )


class NullProvider:
    """No LLM configured. Deterministic lanes still work; generative ones degrade."""

    name = "null"

    def available(self) -> bool:
        return False

    def default_model(self) -> str:
        return "none"

    def invoke(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError(
            "No LLM provider is configured. Set LLM_PROVIDER and the matching "
            "credentials, or rely on the deterministic path."
        )


def usage_counters(response: "LLMResponse") -> dict[str, int]:
    """Token counters for the metrics bus, in the additive shape state expects."""
    usage = getattr(response, "usage", None) or TokenUsage()
    return {
        "llm_calls": 1,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
    }


def provider_from_env() -> LLMProvider:
    """Select a provider from ``LLM_PROVIDER``. Unknown values disable the LLM."""
    choice = (env("LLM_PROVIDER", "groq") or "groq").lower()
    if choice == "groq":
        return GroqProvider()
    if choice == "bedrock":
        return BedrockProvider()
    return NullProvider()


def llm_available() -> bool:
    return provider_from_env().available()
