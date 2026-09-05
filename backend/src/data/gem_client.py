"""GEM Explorer (Terra Dotta) client: OAuth, program search, brochure fetch.

The portal exposes an anonymous OAuth flow, so no credentials are needed and
none are stored. The sequence, verified against the live service:

1. ``GET /index.cfm?FuseAction=Programs.SimpleSearch`` to warm session cookies.
2. ``GET /oauth2/authorize/?client_id=…&response_type=code`` returns ``{"code": …}``.
3. ``GET /oauth2/token/?client_id=…&scope=ProgramBrochureRead`` with the code in
   the ``Authorization`` header returns ``{"access_token": …}``.
   **The token endpoint needs ``client_id`` as well as the header**; without it
   the service answers ``invalid_request`` with a 200, which is easy to mistake
   for success.
4. ``GET /index.cfm?FuseAction=Programs.SearchResults`` lists every programme as
   ``Program_ID=<id>`` anchors — 558 of them.
5. ``GET /models/services/REST/index.cfm?endpoint=/v3/program/{id}/brochure``
   returns the brochure JSON.

Every failure returns a structured result rather than raising. A portal outage
must degrade the workload lane, not take down the shortlist with it.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from typing import Any

from services import cache

HOST = "https://ntu-sa.terradotta.com"
CLIENT_ID = "453A841344B6D32F2220ECC9C247EEAF"
SCOPE = "ProgramBrochureRead"
USER_AGENT = "Mozilla/5.0 (compatible; NTU-Exchange-Planner/1.0)"

# Bounded so a hanging portal cannot hold a request open indefinitely.
TIMEOUT_SECONDS = 25.0
MAX_RETRIES = 1

BROCHURE_TTL = 7 * cache.DAY
INDEX_TTL = cache.DAY

# The public, human-readable page for a programme. This is what a citation
# points at, so a student can open the same brochure and read the same words.
def brochure_url(program_id: int | str) -> str:
    return f"{HOST}/index.cfm?FuseAction=Programs.ViewProgramAngular&id={program_id}"


@dataclass
class GemProgram:
    """One GEM programme as the search index lists it."""

    program_id: int
    name: str

    @property
    def url(self) -> str:
        return brochure_url(self.program_id)


@dataclass
class GemResult:
    """The outcome of a fetch. ``ok`` is false with a reason, never an exception."""

    ok: bool
    data: dict[str, Any] | None = None
    program: GemProgram | None = None
    warnings: list[str] = field(default_factory=list)
    from_cache: bool = False


class GemUnavailable(RuntimeError):
    """Raised only inside this module; callers receive a GemResult instead."""


def _client():
    import httpx

    return httpx.Client(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _access_token(client) -> str:
    """Run the anonymous OAuth dance and return a bearer token."""
    client.get(f"{HOST}/index.cfm", params={"FuseAction": "Programs.SimpleSearch"})

    authorize = client.get(
        f"{HOST}/oauth2/authorize/", params={"client_id": CLIENT_ID, "response_type": "code"}
    )
    payload = authorize.json()
    code = payload.get("code") if isinstance(payload, dict) else None
    if not code:
        raise GemUnavailable(f"GEM Explorer did not issue an authorization code: {payload}")

    token = client.get(
        f"{HOST}/oauth2/token/",
        params={"client_id": CLIENT_ID, "scope": SCOPE},
        headers={"Authorization": code},
    )
    body = token.json()
    # The service answers errors with HTTP 200 and a *list* payload, so the
    # status code alone is not enough to tell success from failure.
    if isinstance(body, list):
        reason = body[0].get("error_description") if body else "unknown error"
        raise GemUnavailable(f"GEM Explorer refused a token: {reason}")
    access_token = body.get("access_token") if isinstance(body, dict) else None
    if not access_token:
        raise GemUnavailable("GEM Explorer returned no access token")
    return access_token


_ANCHOR = re.compile(r"Program_ID=(\d+)[^>]*>(.*?)</a>", re.DOTALL | re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")


def _parse_index(page: str) -> list[dict[str, Any]]:
    """Read ``(program_id, name)`` pairs out of the search results page."""
    found: dict[int, str] = {}
    for raw_id, raw_name in _ANCHOR.findall(page):
        name = html.unescape(_TAGS.sub("", raw_name)).strip()
        name = re.sub(r"\s+", " ", name)
        if name:
            found.setdefault(int(raw_id), name)
    return [{"program_id": pid, "name": name} for pid, name in sorted(found.items())]


def program_index(refresh: bool = False, db_path=None) -> list[GemProgram]:
    """Every programme GEM Explorer lists. Cached for a day.

    Returns an empty list when the portal is unreachable; callers treat that as
    "no workload evidence available", not as an error.
    """
    key = cache.make_key("gemindex", "all")
    if not refresh:
        hit = cache.get(key, db_path=db_path)
        if hit:
            return [GemProgram(**row) for row in hit]

    try:
        with _client() as client:
            _access_token(client)
            page = client.get(
                f"{HOST}/index.cfm",
                params={"FuseAction": "Programs.SearchResults", "Type": "Program"},
            ).text
    except Exception:  # noqa: BLE001 - unreachable portal is a normal state
        return []

    rows = _parse_index(page)
    if rows:
        cache.set(key, rows, namespace="gemindex", ttl_seconds=INDEX_TTL, db_path=db_path)
    return [GemProgram(**row) for row in rows]


_TITLE_PREFIX = re.compile(r"^\s*GEM\s+(?:Explorer|Discoverer[^:]*|[A-Za-z ]*?)\s*:\s*", re.IGNORECASE)
_TITLE_COUNTRY = re.compile(r",\s*([^,]+?)\s*$")

MATCH_THRESHOLD = 90.0
MATCH_MARGIN = 4.0


def _split_title(title: str) -> tuple[str, str]:
    """Split "GEM Explorer: Aalborg University, Denmark" into name and country."""
    body = _TITLE_PREFIX.sub("", title or "").strip()
    country_match = _TITLE_COUNTRY.search(body)
    country = country_match.group(1).strip() if country_match else ""
    name = _TITLE_COUNTRY.sub("", body).strip() if country_match else body
    return name, country


def _country_agrees(wanted: str, published: str) -> bool | None:
    """Whether two spellings of a country refer to the same place.

    Compared on shared words, not equality: Coursefinder stores "KOREA,
    REPUBLIC OF" and "UNITED STATES OF AMERICA" where GEM publishes "Korea
    (Republic of)" and "United States". Demanding an exact match rejects the
    correct programme. ``None`` means one side is unknown, which is neither
    agreement nor conflict.
    """
    if not wanted or not published:
        return None
    # Coursefinder uses its canonical database spelling (for example
    # ``TURKIYE``), while Terra Dotta commonly publishes the everyday spelling
    # (``Turkey``). Resolve both sides through the same country index before
    # comparing words, otherwise an exact university name is penalised as a
    # country mismatch and rejected below the match threshold.
    from data.destinations import resolve_country

    wanted = resolve_country(wanted) or wanted
    published = resolve_country(published) or published
    stop = {"the", "of", "and", "republic"}
    left = {w for w in re.findall(r"[a-z]{3,}", wanted.lower()) if w not in stop}
    right = {w for w in re.findall(r"[a-z]{3,}", published.lower()) if w not in stop}
    if not left or not right:
        return None
    return bool(left & right)


def find_program(
    university_name: str, country: str | None = None, db_path=None
) -> GemProgram | None:
    """Match a Coursefinder university name to a GEM programme.

    GEM titles carry a prefix and a country ("GEM Explorer: Aalborg University,
    Denmark"), so both are stripped before scoring; comparing against the raw
    title lets a substring scorer rank "Queensland University Of Technology"
    top for a search for "Queens University", which would answer a student's
    question with an entirely different university's rules.

    Two guards make a wrong match unlikely rather than merely uncommon:

    * the best candidate must clear :data:`MATCH_THRESHOLD`, **and** beat the
      runner-up by :data:`MATCH_MARGIN` — a near-tie is ambiguity, and
      ambiguity returns ``None``;
    * when both countries are known they must agree.

    Returning ``None`` costs a caveat. Returning the wrong university costs the
    student a wrong answer they have no way to detect.
    """
    programs = program_index(db_path=db_path)
    if not programs:
        return None
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover
        return None

    target = (university_name or "").strip()
    if not target:
        return None
    wanted_country = (country or "").strip().lower()

    # Explorer is the semester-long exchange. Discoverer is a summer programme
    # with different rules, so it is only considered if nothing else matches.
    explorer = [p for p in programs if "gem explorer" in p.name.lower()]
    for pool in (explorer, programs):
        if not pool:
            continue
        scored: list[tuple[float, float, GemProgram]] = []
        for program in pool:
            name, program_country = _split_title(program.name)
            # Case matters to rapidfuzz and must not matter here: Coursefinder
            # stores "Universidad Carlos Iii De Madrid (Uc3m)" where GEM
            # publishes "Universidad Carlos III de Madrid (UC3M)". Compared
            # literally those score 82, below the threshold, and the correct
            # programme is rejected as no match at all.
            score = float(fuzz.token_set_ratio(target, name, processor=str.lower))
            agrees = _country_agrees(wanted_country, program_country)
            if agrees is True:
                score += 5.0
            elif agrees is False:
                score -= 25.0
            # A strict whole-string ratio, kept aside as a tie-breaker only. It
            # is too sensitive to length to lead — Coursefinder's "Aalto
            # University - School Of Science & Technology" scores barely 50
            # against GEM's "Aalto University" — but it is exactly what
            # separates two names that differ by one decisive word.
            scored.append((score, float(fuzz.ratio(target, name, processor=str.lower)), program))
        scored.sort(key=lambda row: row[0], reverse=True)

        best = scored[0]
        if best[0] < MATCH_THRESHOLD:
            continue
        contenders = [row for row in scored if best[0] - row[0] < MATCH_MARGIN]
        if len(contenders) == 1:
            return best[2]

        # A near-tie. "The Chinese University of Hong Kong (Shenzhen)" and "The
        # Chinese University of Hong Kong" are different universities in
        # different cities; the deciding word is the one the loose scorer
        # ignores.
        contenders.sort(key=lambda row: row[1], reverse=True)
        if contenders[0][1] - contenders[1][1] >= MATCH_MARGIN:
            return contenders[0][2]
    return None


def fetch_brochure(
    program_id: int | str, refresh: bool = False, db_path=None
) -> GemResult:
    """One programme's brochure JSON, cached for a week.

    Retries once on 401, because the anonymous token is short-lived and an
    expiry mid-session is expected rather than exceptional.
    """
    key = cache.make_key("gem", program_id)
    if not refresh:
        hit = cache.get(key, db_path=db_path)
        if hit:
            return GemResult(ok=True, data=hit, from_cache=True)

    warnings: list[str] = []
    for attempt in range(MAX_RETRIES + 1):
        try:
            with _client() as client:
                token = _access_token(client)
                response = client.get(
                    f"{HOST}/models/services/REST/index.cfm",
                    params={"endpoint": f"/v3/program/{program_id}/brochure"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if response.status_code == 401 and attempt < MAX_RETRIES:
                    warnings.append("gem_token_expired: retried once with a fresh token")
                    time.sleep(0.5)
                    continue
                if response.status_code != 200:
                    return GemResult(
                        ok=False,
                        warnings=[*warnings, f"gem_http_{response.status_code}"],
                    )
                data = response.json()
        except Exception as exc:  # noqa: BLE001
            return GemResult(ok=False, warnings=[*warnings, f"gem_unreachable: {exc.__class__.__name__}"])

        if not isinstance(data, dict) or "current" not in data:
            return GemResult(ok=False, warnings=[*warnings, "gem_unexpected_payload"])

        cache.set(key, data, namespace="gem", ttl_seconds=BROCHURE_TTL, db_path=db_path)
        return GemResult(ok=True, data=data, warnings=warnings)

    return GemResult(ok=False, warnings=[*warnings, "gem_retries_exhausted"])


def brochure_for(
    university_name: str, country: str | None = None, db_path=None
) -> GemResult:
    """Find a university's GEM programme and fetch its brochure in one step."""
    program = find_program(university_name, country, db_path=db_path)
    if program is None:
        return GemResult(
            ok=False,
            warnings=[f"gem_program_not_found: no GEM listing matched '{university_name}'"],
        )
    result = fetch_brochure(program.program_id, db_path=db_path)
    result.program = program
    return result
