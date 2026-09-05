"""Web search behind a provider seam.

The research lane is the only part of the system allowed to read the open web,
and this is the only door it can read it through. One interface, several
implementations: `Tavily <https://tavily.com>`_ (what the deployed container
runs), a self-hosted or managed
`openserp <https://github.com/karust/openserp>`_ instance, and a Null provider
that makes "no search configured" a first-class supported state rather than a
crash.

Whichever one answers, it returns the same :class:`SearchResult`, and the
research lane's grounding rule is unchanged: a claim citing a URL outside
:attr:`SearchResult.urls` is dropped. Swapping the provider swaps where the
evidence comes from, never whether evidence is required.

Two decisions worth stating.

**The default engine is not Google.** A scraped-Google captcha in the middle of
a demo is the failure you cannot recover from on stage, so the default is
DuckDuckGo and the engine is an environment variable.

**openserp replaces the HTML extractor too.** With ``extract`` set, the service
returns page content inline on each hit, so there is no separate scraper to
write, bound or defend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from graph.config import env

DEFAULT_URL = "http://127.0.0.1:7000"
DEFAULT_ENGINE = "duckduckgo"
VALID_ENGINES = frozenset({"google", "bing", "duckduckgo", "yandex", "baidu", "ecosia"})

# Bounds. Every one of these exists so a single question cannot turn into an
# unbounded crawl.
MAX_RESULTS = 8          # openserp parses only the first SERP page at <= 10
MAX_CONTENT_CHARS = 4000  # per hit, before it reaches a prompt
# How long one search may take. Locally the streaming turn has a 110s deadline
# and a slow extract is worth waiting for, so 35s was right. Behind API Gateway
# the whole request dies at ~30s, and a search that outlives the gateway gives
# the student a timeout instead of an answer - strictly worse than the honest
# refusal it replaces. Both are environment-tunable so the deployed value can
# change without rebuilding a 1.3 GB image.
TIMEOUT_SECONDS = float(env("SEARCH_TIMEOUT_SECONDS", "12") or 12)
AVAILABILITY_TIMEOUT_SECONDS = float(env("SEARCH_AVAILABILITY_TIMEOUT_SECONDS", "4") or 4)


@dataclass
class SearchHit:
    """One result. ``content`` is present only when extraction was requested."""

    title: str
    url: str
    snippet: str = ""
    domain: str = ""
    rank: int = 0
    engine: str = ""
    content: str = ""


@dataclass
class SearchResult:
    """A whole search, including what went wrong if anything did."""

    hits: list[SearchHit] = field(default_factory=list)
    engine: str = ""
    took_ms: int = 0
    engines_failed: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.hits)

    @property
    def urls(self) -> set[str]:
        """The hit set. A claim citing anything outside this is not grounded."""
        return {hit.url for hit in self.hits if hit.url}


@runtime_checkable
class SearchProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def search(self, query: str, *, max_results: int = MAX_RESULTS, site: str | None = None) -> SearchResult: ...


class OpenSerpProvider:
    """HTTP against a local openserp instance."""

    name = "openserp"

    def __init__(self, url: str | None = None, engine: str | None = None):
        self.url = (url or env("OPENSERP_URL", DEFAULT_URL) or DEFAULT_URL).rstrip("/")
        chosen = (engine or env("OPENSERP_ENGINE", DEFAULT_ENGINE) or DEFAULT_ENGINE).lower()
        self.engine = chosen if chosen in VALID_ENGINES else DEFAULT_ENGINE

    def available(self) -> bool:
        """Whether the service answers. Never raises, so a probe is cheap."""
        import httpx

        try:
            with httpx.Client(timeout=AVAILABILITY_TIMEOUT_SECONDS) as client:
                response = client.get(
                    f"{self.url}/{self.engine}/search",
                    params={"text": "ping", "limit": 1, "format": "json"},
                )
            return response.status_code < 500
        except Exception:  # noqa: BLE001
            return False

    def search(
        self, query: str, *, max_results: int = MAX_RESULTS, site: str | None = None
    ) -> SearchResult:
        import httpx

        params: dict[str, object] = {
            "text": query,
            "lang": "EN",
            "limit": max(1, min(int(max_results), 10)),
            "format": "json",
            # One extracted page is enough for grounded findings and is much
            # faster than fetching three full pages for every question.
            "extract": 1,
            "extract_mode": "auto",
        }
        if site:
            params["site"] = site

        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                response = client.get(f"{self.url}/{self.engine}/search", params=params)
        except Exception as exc:  # noqa: BLE001
            return SearchResult(engine=self.engine, error=f"search_unreachable: {exc.__class__.__name__}")

        if response.status_code != 200:
            return SearchResult(engine=self.engine, error=f"search_http_{response.status_code}")

        try:
            payload = response.json()
        except ValueError:
            return SearchResult(engine=self.engine, error="search_unparseable")

        return parse_response(payload, self.engine)


def parse_response(payload: object, engine: str = "") -> SearchResult:
    """Read an openserp payload into hits. Pure, so the tests need no service.

    Tolerates both the bare-list and the wrapped ``{"results": [...]}`` shapes,
    because the service has shipped both.
    """
    rows: list = []
    meta: dict = {}
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        candidate = payload.get("results")
        rows = candidate if isinstance(candidate, list) else []
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}

    hits: list[SearchHit] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        extracted = row.get("extracted")
        content = ""
        if isinstance(extracted, dict):
            content = str(extracted.get("content") or "")[:MAX_CONTENT_CHARS]
        hits.append(
            SearchHit(
                title=str(row.get("title") or "").strip(),
                url=url,
                snippet=str(row.get("snippet") or row.get("description") or "").strip(),
                domain=str(row.get("domain") or "").strip(),
                rank=int(row.get("rank") or 0),
                engine=str(row.get("engine") or engine),
                content=content,
            )
        )

    failed = meta.get("engines_failed")
    return SearchResult(
        hits=hits,
        engine=engine,
        took_ms=int(meta.get("took_ms") or 0),
        engines_failed=[str(f) for f in failed] if isinstance(failed, list) else [],
        error=None if hits else "search_no_results",
    )


CLOUD_DEFAULT_URL = "https://api.openserp.org/v1"


class OpenSerpCloudProvider:
    """HTTP against the managed OpenSERP Cloud API.

    Same response envelope as the self-hosted service (``results`` /
    ``meta`` / ``extracted``), but with the ``/v1/`` path prefix and a
    ``Bearer`` API-key header. Reuses ``parse_response`` unchanged because the
    shapes are identical.
    """

    name = "cloud"

    def __init__(self, url: str | None = None, engine: str | None = None):
        self.url = (url or env("OPENSERP_URL", CLOUD_DEFAULT_URL) or CLOUD_DEFAULT_URL).rstrip("/")
        self.api_key = env("OPENSERP_API_KEY")
        chosen = (engine or env("OPENSERP_ENGINE", DEFAULT_ENGINE) or DEFAULT_ENGINE).lower()
        self.engine = chosen if chosen in VALID_ENGINES else DEFAULT_ENGINE

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def available(self) -> bool:
        """True when a key is set and the account answers. Never raises."""
        if not self.api_key:
            return False
        import httpx

        try:
            with httpx.Client(timeout=AVAILABILITY_TIMEOUT_SECONDS) as client:
                response = client.get(f"{self.url}/me", headers=self._headers())
            return response.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def search(
        self, query: str, *, max_results: int = MAX_RESULTS, site: str | None = None
    ) -> SearchResult:
        if not self.api_key:
            return SearchResult(engine=self.engine, error="research_unavailable: no OPENSERP_API_KEY set")
        import httpx

        params: dict[str, object] = {
            "text": query,
            "lang": "EN",
            "limit": max(1, min(int(max_results), 10)),
        }
        if site:
            params["site"] = site

        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                response = client.get(
                    f"{self.url}/{self.engine}/search", params=params, headers=self._headers()
                )
        except Exception as exc:  # noqa: BLE001
            return SearchResult(engine=self.engine, error=f"search_unreachable: {exc.__class__.__name__}")

        if response.status_code == 401:
            return SearchResult(engine=self.engine, error="research_unavailable: bad or missing OpenSERP API key")
        if response.status_code != 200:
            return SearchResult(engine=self.engine, error=f"search_http_{response.status_code}")

        try:
            payload = response.json()
        except ValueError:
            return SearchResult(engine=self.engine, error="search_unparseable")

        return parse_response(payload, self.engine)


TAVILY_URL = "https://api.tavily.com/search"
TAVILY_DEFAULT_DEPTH = "basic"
TAVILY_VALID_DEPTHS = frozenset({"basic", "advanced"})


class TavilySearchProvider:
    """HTTP against Tavily, a search API built for grounding LLM answers.

    This is what the deployed container runs, and it exists because the two
    alternatives were each measured and found wanting.

    **openserp** works and is still the better answer on a workstation, but it
    drives a headless browser: 253 MB of headless-shell plus a 28 MB binary
    inside an image whose cold start already sits against the API Gateway
    ceiling, and nothing had established that DuckDuckGo answers AWS ranges at
    all.

    **Groq** was tried because a key was already on hand. Its compound systems
    return HTTP 413 the moment a search actually fires, and its ``browser_search``
    tool spent 237,494 tokens on one question - against a 200,000/day free-tier
    cap - by running 22 searches, took 14.6 s, and returned every result with an
    empty ``content`` field. Titles and URLs with no text are nothing for the
    research lane to ground on, so it would decline exactly as it does with no
    provider at all. Notably, Groq's search *is* Tavily underneath; going
    straight to the source removes an LLM hop, the token bill and the latency.

    Tavily returns ``{title, url, content, score}`` with real extracted text,
    which is close enough to :class:`SearchHit` that the mapping is direct.
    """

    name = "tavily"

    def __init__(self, api_key: str | None = None, depth: str | None = None):
        self.api_key = api_key or env("TAVILY_API_KEY")
        chosen = (depth or env("TAVILY_SEARCH_DEPTH", TAVILY_DEFAULT_DEPTH) or "").lower()
        self.depth = chosen if chosen in TAVILY_VALID_DEPTHS else TAVILY_DEFAULT_DEPTH
        # Tavily bills raw page text as extra credits on a 1,000/month free
        # allowance. Its ``content`` field is already the passage its ranker
        # judged relevant, which is what a finding needs, so full text is off
        # by default and available for a demo that wants deeper extracts.
        self.include_raw = (env("TAVILY_INCLUDE_RAW", "") or "").lower() in {"1", "true", "yes"}
        self.engine = "tavily"

    @staticmethod
    def _is_real_key(key: str | None) -> bool:
        """Shape check, mirroring ``providers.GroqProvider._is_real_key``.

        Only Tavily can say whether a well-formed key is valid. This exists so
        a template value left in ``.env`` reports the lane as unavailable -
        an honest decline - instead of failing on every call.
        """
        candidate = (key or "").strip()
        return candidate.startswith("tvly-") and len(candidate) > 12

    def available(self) -> bool:
        """Whether a usable key is set.

        Deliberately offline. Every Tavily request spends a credit, and this is
        called on turns that may never search, so a network probe here would
        bill the free allowance for asking a question rather than answering one.
        """
        return self._is_real_key(self.api_key)

    def search(
        self, query: str, *, max_results: int = MAX_RESULTS, site: str | None = None
    ) -> SearchResult:
        if not self.available():
            return SearchResult(
                engine=self.engine, error="research_unavailable: no usable TAVILY_API_KEY set"
            )
        import httpx

        payload: dict[str, object] = {
            "query": query,
            "max_results": max(1, min(int(max_results), 20)),
            "search_depth": self.depth,
            "include_raw_content": self.include_raw,
        }
        if site:
            payload["include_domains"] = [site]

        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                response = client.post(
                    TAVILY_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except Exception as exc:  # noqa: BLE001
            return SearchResult(engine=self.engine, error=f"search_unreachable: {exc.__class__.__name__}")

        if response.status_code in (401, 403):
            return SearchResult(engine=self.engine, error="research_unavailable: bad or missing Tavily API key")
        if response.status_code == 429:
            return SearchResult(engine=self.engine, error="search_rate_limited")
        if response.status_code == 432:
            # Tavily's own code for a spent allowance, distinct from throttling.
            return SearchResult(engine=self.engine, error="research_unavailable: Tavily credits exhausted")
        if response.status_code != 200:
            return SearchResult(engine=self.engine, error=f"search_http_{response.status_code}")

        try:
            body = response.json()
        except ValueError:
            return SearchResult(engine=self.engine, error="search_unparseable")

        return parse_tavily_response(body, self.engine)


_NAV_LINE_CHARS = 40


def strip_leading_chrome(text: str) -> str:
    """Drop a page's navigation preamble from an extracted passage.

    Tavily returns extracted page text, not a search engine's curated
    description, so a hit sometimes opens with the site's chrome - "Skip to
    content", a language switcher, the organisation name - before any prose.
    That is harmless in a prompt but not on screen: when the LLM is
    unavailable the research lane falls back to showing the snippet verbatim
    as the claim, and a finding that reads "Skip to content de en" is worse
    than useless in front of a student.

    Leading lines shorter than :data:`_NAV_LINE_CHARS` are dropped until a
    substantial one appears; if none does, the text is returned unchanged
    rather than emptied.
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    for index, line in enumerate(lines):
        if len(line) >= _NAV_LINE_CHARS:
            kept = " ".join(part for part in lines[index:] if part)
            return kept.strip()
    return " ".join(part for part in lines if part).strip()


def parse_tavily_response(payload: object, engine: str = "tavily") -> SearchResult:
    """Read a Tavily payload into hits. Pure, so the tests need no key.

    ``raw_content`` is preferred over ``content`` when present, because it is
    the full extracted page rather than the ranked passage; both are truncated
    to :data:`MAX_CONTENT_CHARS` before they can reach a prompt.
    """
    if not isinstance(payload, dict):
        return SearchResult(engine=engine, error="search_unparseable")

    rows = payload.get("results")
    if not isinstance(rows, list):
        return SearchResult(engine=engine, error="search_unparseable")

    hits: list[SearchHit] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        snippet = strip_leading_chrome(str(row.get("content") or ""))
        raw = row.get("raw_content")
        content = strip_leading_chrome(str(raw) if raw else snippet)[:MAX_CONTENT_CHARS]
        hits.append(
            SearchHit(
                title=str(row.get("title") or "").strip(),
                url=url,
                snippet=snippet[:300],
                domain=url.split("/")[2] if "://" in url else "",
                # Tavily returns results already ranked, so position is the rank.
                rank=len(hits) + 1,
                engine=engine,
                content=content,
            )
        )

    took = payload.get("response_time")
    try:
        took_ms = int(float(took) * 1000)
    except (TypeError, ValueError):
        took_ms = 0

    return SearchResult(
        hits=hits,
        engine=engine,
        took_ms=took_ms,
        error=None if hits else "search_no_results",
    )


class NullSearchProvider:
    """No search configured. The research lane declines, cleanly and always."""

    name = "null"

    def available(self) -> bool:
        return False

    def search(
        self, query: str, *, max_results: int = MAX_RESULTS, site: str | None = None
    ) -> SearchResult:
        return SearchResult(error="research_unavailable: no search provider is configured")


def provider_from_env() -> SearchProvider:
    """Select a provider from ``SEARCH_PROVIDER``. Unknown values disable search.

    ``tavily`` is the default because it is what the deployed container runs.
    openserp is kept for local use, where a browser is available and a local
    instance costs nothing per query - but it cannot run on Lambda at all:
    Lambda provides no ``/dev/shm`` and Chromium hangs without it, which is
    measured, not assumed. See the note on TavilySearchProvider.
    """
    choice = (env("SEARCH_PROVIDER", "tavily") or "tavily").lower()
    if choice == "tavily":
        return TavilySearchProvider()
    if choice == "cloud":
        return OpenSerpCloudProvider()
    if choice == "openserp":
        return OpenSerpProvider()
    return NullSearchProvider()
