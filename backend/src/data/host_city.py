"""Working out which city a partner university sits in.

A cost-of-living lookup needs a city, and Coursefinder stores only a name and a
country. The obvious shortcut — ask a language model — is exactly what this
module exists to avoid: a model will answer "Aalborg University is in Aalborg"
correctly and "Akita International University is in Akita" correctly and then,
with identical confidence, place a university in the wrong city entirely. A
wrong city produces a wrong budget that looks perfectly reasonable.

So the resolution is a cascade of *evidence*, each step cited, and it stops at
``None`` rather than guessing:

1. explicit place hints embedded in the institution name, such as "De Barcelona";
2. OpenStreetMap Nominatim, which geocodes the institution itself and is
   filtered to educational features whose name and country both match;
3. Wikipedia's summary for the institution;
4. Wikidata's structured location facts for the institution;
5. configured web search snippets/content for the institution location;
6. ``None`` — country-level only, and the caller says so.

Every result carries the step that produced it, so a briefing can show where
the city came from.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from services import cache

TIMEOUT_SECONDS = 6.0
CITY_TTL = 30 * cache.DAY
USER_AGENT = "NTU-Exchange-Planner/1.0 (student exchange planning; contact via NTU)"

NOMINATIM = "https://nominatim.openstreetmap.org/search"
WIKIPEDIA_SEARCH = "https://en.wikipedia.org/w/api.php"
WIKIDATA_SEARCH = "https://www.wikidata.org/w/api.php"

# Words in a university name that are never a city.
_NOT_A_PLACE = frozenset(
    {
        "university", "universite", "universidad", "universita", "universitat", "universiteit",
        "college", "institute", "institut", "school", "faculty", "academy", "polytechnic",
        "technology", "technological", "science", "sciences", "engineering", "business",
        "management", "national", "international", "state", "central", "royal", "federal",
        "metropolitan", "of", "the", "and", "for", "at", "in", "campus", "main", "city",
        "arts", "applied", "studies", "health", "medicine", "law", "economics", "social",
        "autonomous", "autonoma", "autonome",
    }
)

_NAME_PLACE_PREPOSITIONS = re.compile(
    r"\b(?:de|of|in|at)\s+([A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){0,2})",
    re.IGNORECASE,
)


def _searchable_university_name(university_name: str) -> str:
    """Strip Coursefinder audience/school suffixes before public lookups."""
    name = re.split(r"\s+-\s+", university_name or "", maxsplit=1)[0]
    name = re.sub(r"\([^)]*\)", "", name)
    return re.sub(r"\s+", " ", name).strip() or (university_name or "").strip()


@dataclass(frozen=True)
class City:
    """A resolved host city, and the evidence that produced it."""

    name: str
    country: str | None
    method: str
    source_url: str | None = None
    region: str | None = None

    @property
    def slug(self) -> str:
        """Legacy slug form for a city name.

        Diacritics are stripped because the site indexes ASCII slugs: the page
        for Lulea University's home city is /Lulea, not /Luleå.
        """
        folded = unicodedata.normalize("NFKD", self.name.strip())
        ascii_name = "".join(ch for ch in folded if not unicodedata.combining(ch))
        return ascii_name.replace(" ", "-")


def _place_hint_from_name(university_name: str, country: str | None) -> City | None:
    """Fast fallback for names that explicitly carry the place.

    Coursefinder names such as "Universitat Autonoma De Barcelona" and
    "Universidad Carlos Iii De Madrid (Uc3m)" already state the city. Use that
    before paying for a broad web search or falling to country-level Wise.
    """
    cleaned = _searchable_university_name(university_name)
    candidates: list[str] = []
    for match in _NAME_PLACE_PREPOSITIONS.finditer(cleaned):
        candidates.append(match.group(1))

    for candidate in candidates:
        place = _clean_location_piece(candidate)
        if place:
            return City(
                name=place,
                country=_normalise_country(country) or country,
                method="name_hint",
                source_url=None,
                region=None,
            )
    return None


def _http_json(url: str, params: dict) -> object | None:
    import httpx

    try:
        with httpx.Client(
            timeout=TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            response = client.get(url, params=params)
            if response.status_code != 200:
                return None
            return response.json()
    except Exception:  # noqa: BLE001 - an unreachable geocoder is a normal state
        return None


EDUCATIONAL_TYPES = frozenset({"university", "college", "school", "educational_institution"})
NAME_MATCH_THRESHOLD = 70.0

# Administrative suffixes that are part of a place's legal name but not of the
# name a cost-of-living site indexes: Suwon-si is Suwon.
_ADMIN_SUFFIX = re.compile(r"[-\s](?:si|shi|gun|gu|ku|shk|city|municipality)$", re.IGNORECASE)


def _tidy_city(name: str) -> str:
    return _ADMIN_SUFFIX.sub("", (name or "").strip()).strip()


def _countries_agree(wanted: str | None, published: str | None) -> bool | None:
    """Whether two spellings of a country name refer to the same place.

    None means one side is unknown, which is neither agreement nor conflict.
    """
    if not wanted or not published:
        return None
    stop = {"the", "of", "and", "republic"}
    left = {w for w in re.findall(r"[a-z]{3,}", wanted.lower()) if w not in stop}
    right = {w for w in re.findall(r"[a-z]{3,}", published.lower()) if w not in stop}
    if not left or not right:
        return None
    return bool(left & right)


def _normalise_country(country: str | None) -> str | None:
    if not country:
        return None
    aliases = {
        "KOREA, REPUBLIC OF": "South Korea",
        "UNITED KINGDOM": "United Kingdom",
        "UNITED STATES": "United States",
    }
    return aliases.get(country.strip().upper(), country.strip().title())


def _city_from_address(address: dict, country: str | None) -> str | None:
    for key in ("city", "town", "municipality", "village", "county", "state"):
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            city = value.strip()
            if _is_broad_country(city, country):
                return None
            return city
    return None


def _is_broad_country(place: str | None, country: str | None) -> bool:
    if not place or not country:
        return False
    normalised_place = re.sub(r"[^a-z]+", "", place.lower())
    normalised_country = re.sub(r"[^a-z]+", "", (_normalise_country(country) or country).lower())
    return bool(normalised_place and normalised_place == normalised_country)


def _region_from_address(address: dict) -> str | None:
    for key in ("state", "province", "region", "county"):
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return _tidy_city(value)
    return None


def _from_nominatim(university_name: str, country: str | None) -> City | None:
    """Geocode the institution itself and read the city off the result.

    Two guards, both learned from being wrong. A search for "Queens University,
    Canada" returns the Jackman Law Building in Toronto — an OSM feature typed
    ``university`` that is not the university asked for — and answering with
    Toronto instead of Kingston produces a budget that is confidently wrong.
    So a result must both *be* an educational feature and *carry a name that
    matches* what was asked for.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover
        return None

    search_name = _searchable_university_name(university_name)
    queries = [search_name]
    if country:
        # The country first, but a database spelling like "KOREA, REPUBLIC OF"
        # confuses free-text geocoding, so the bare name is always retried.
        queries.insert(0, f"{search_name}, {country.title()}")

    best: tuple[float, float, City] | None = None
    for query in queries:
        payload = _http_json(
            NOMINATIM,
            {"q": query, "format": "jsonv2", "limit": 8, "addressdetails": 1,
             "accept-language": "en"},
        )
        if not isinstance(payload, list):
            continue
        for item in payload:
            if (item.get("type") or "").lower() not in EDUCATIONAL_TYPES:
                continue
            name = (item.get("name") or "").strip()
            if not name:
                continue
            score = float(fuzz.token_set_ratio(search_name, name))
            if score < NAME_MATCH_THRESHOLD:
                continue

            address = item.get("address") or {}
            # A country mismatch is decisive. "Queens University" matches
            # "Queens University of Charlotte" in the United States almost as
            # well as the Canadian one, and answering with Charlotte instead of
            # Kingston produces a budget that is confidently wrong.
            if _countries_agree(country, address.get("country")) is False:
                continue

            city = _city_from_address(address, country)
            if not city:
                continue
            # Importance ranks the main campus above an outlying facility that
            # happens to share the institution's name.
            importance = float(item.get("importance") or 0.0)
            candidate = (score, importance, City(
                name=_tidy_city(city),
                country=(address.get("country") or country or None),
                method="nominatim",
                source_url="https://www.openstreetmap.org/",
                region=_region_from_address(address),
            ))
            if best is None or candidate[:2] > best[:2]:
                best = candidate
        if best is not None:
            return best[2]
    return None


def _from_wikipedia(university_name: str, country: str | None) -> City | None:
    """Read the city out of Wikipedia's one-paragraph summary."""
    search = _http_json(
        WIKIPEDIA_SEARCH,
        {
            "action": "query",
            "list": "search",
            "srsearch": _searchable_university_name(university_name),
            "format": "json",
            "srlimit": 1,
        },
    )
    try:
        title = search["query"]["search"][0]["title"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return None

    summary = _http_json(
        WIKIPEDIA_SEARCH,
        {"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
         "titles": title, "format": "json"},
    )
    try:
        pages = summary["query"]["pages"]  # type: ignore[index]
        extract = next(iter(pages.values())).get("extract", "")  # type: ignore[union-attr]
    except (KeyError, StopIteration, TypeError, AttributeError):
        return None

    city, region = _location_from_text(extract or "")
    if not city:
        return None
    if _is_broad_country(city, country):
        return None
    return City(
        name=city,
        country=country,
        method="wikipedia",
        source_url=f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
        region=region,
    )


_LOCATION_PATTERNS = (
    re.compile(
        r"\b(?:located|based|situated)\s+in\s+(?:the\s+(?:city\s+)?centre\s+of\s+)?"
        r"([A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){0,2})"
        r"(?:,\s*([A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){0,2}))?",
    ),
    re.compile(
        r"\b(?:campus|campuses)\s+in\s+([A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){0,2})"
        r"(?:,\s*([A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){0,2}))?",
    ),
)


def _clean_location_piece(value: str | None) -> str | None:
    text = re.sub(r"\s+", " ", value or "").strip(" .,:;()[]")
    if not text or text.lower() in _NOT_A_PLACE:
        return None
    words = [w for w in text.split() if w.lower() not in {"the", "a", "an"}]
    # Wikipedia snippets sometimes match trailing institution words such as
    # "Applied Sciences" or "Technology Sydney" as if they were a place.
    # Reject any candidate containing a known institution-only token; a
    # country-level Wise lookup is safer than a plausible false city.
    if not words or len(words) > 3 or any(w.lower() in _NOT_A_PLACE for w in words):
        return None
    return _tidy_city(" ".join(words))


def _location_from_text(text: str) -> tuple[str | None, str | None]:
    for pattern in _LOCATION_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        city = _clean_location_piece(match.group(1))
        region = _clean_location_piece(match.group(2)) if match.lastindex and match.lastindex > 1 else None
        if city:
            return city, region
    return None, None


def _wikidata_entity_ids(claims: dict, prop: str) -> list[str]:
    ids: list[str] = []
    for claim in claims.get(prop, []):
        try:
            numeric_id = claim["mainsnak"]["datavalue"]["value"]["numeric-id"]
        except (KeyError, TypeError):
            continue
        ids.append(f"Q{numeric_id}")
    return ids


def _wikidata_label(entity_id: str) -> str | None:
    payload = _http_json(
        WIKIDATA_SEARCH,
        {
            "action": "wbgetentities",
            "ids": entity_id,
            "props": "labels",
            "languages": "en",
            "format": "json",
        },
    )
    try:
        return payload["entities"][entity_id]["labels"]["en"]["value"]  # type: ignore[index]
    except (KeyError, TypeError):
        return None


def _from_wikidata(university_name: str, country: str | None) -> City | None:
    """Read structured host-location facts from Wikidata."""
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover
        return None

    country_label = _normalise_country(country)
    search = _http_json(
        WIKIDATA_SEARCH,
        {
            "action": "wbsearchentities",
            "search": _searchable_university_name(university_name),
            "language": "en",
            "format": "json",
            "limit": 5,
        },
    )
    if not isinstance(search, dict):
        return None

    for hit in search.get("search", []):
        entity_id = hit.get("id")
        label = hit.get("label") or ""
        description = hit.get("description") or ""
        haystack = f"{label} {description}"
        if not entity_id or fuzz.token_set_ratio(_searchable_university_name(university_name), label) < NAME_MATCH_THRESHOLD:
            continue
        if country_label and country_label.lower() not in haystack.lower():
            city, region = _location_from_text(description)
            if city and not _is_broad_country(city, country):
                return City(city, country_label, "wikidata", f"https://www.wikidata.org/wiki/{entity_id}", region)

        entity = _http_json(
            WIKIDATA_SEARCH,
            {
                "action": "wbgetentities",
                "ids": entity_id,
                "props": "claims",
                "format": "json",
            },
        )
        try:
            claims = entity["entities"][entity_id]["claims"]  # type: ignore[index]
        except (KeyError, TypeError):
            claims = {}

        published_country = None
        country_ids = _wikidata_entity_ids(claims, "P17")
        if country_ids:
            published_country = _wikidata_label(country_ids[0])
        if _countries_agree(country, published_country) is False:
            continue

        for prop in ("P159", "P276", "P131"):
            for place_id in _wikidata_entity_ids(claims, prop):
                place = _wikidata_label(place_id)
                if place and place.lower() not in _NOT_A_PLACE and not _is_broad_country(place, country):
                    return City(
                        name=_tidy_city(place),
                        country=published_country or country_label or country,
                        method="wikidata",
                        source_url=f"https://www.wikidata.org/wiki/{entity_id}",
                        region=None,
                    )

        city, region = _location_from_text(description)
        if city and not _is_broad_country(city, country):
            return City(city, country_label or country, "wikidata", f"https://www.wikidata.org/wiki/{entity_id}", region)
    return None


def _from_web_search(university_name: str, country: str | None) -> City | None:
    """Last-resort location lookup through the configured search provider."""
    try:
        from data import search as search_module
    except Exception:  # pragma: no cover - import itself should be boring
        return None

    provider = search_module.provider_from_env()
    if not provider.available():
        return None

    country_label = _normalise_country(country)
    search_name = _searchable_university_name(university_name)
    query = f'"{search_name}" location city'
    if country_label:
        query += f" {country_label}"
    result = provider.search(query, max_results=5)
    if not result.ok:
        return None

    for hit in result.hits:
        haystack = " ".join(part for part in (hit.title, hit.snippet, hit.content) if part)
        if search_name.split()[0].lower() not in haystack.lower():
            continue
        if country_label and country_label.lower() not in haystack.lower():
            continue
        city, region = _location_from_text(haystack)
        if city and not _is_broad_country(city, country):
            return City(
                name=city,
                country=country_label or country,
                method="web_search",
                source_url=hit.url,
                region=region,
            )
    return None


def resolve(university_name: str, country: str | None = None, db_path=None) -> City | None:
    """The city this university sits in, or ``None``.

    ``None`` is a real answer, not a failure: the caller then reports
    country-level information only, rather than costing a city it guessed.
    """
    name = (university_name or "").strip()
    if not name:
        return None

    name_hint = _place_hint_from_name(name, country)
    if name_hint is not None:
        return name_hint

    key = cache.make_key("city:v3", name, country or "")
    hit = cache.get(key, db_path=db_path)
    if hit:
        try:
            cached = City(**hit)
            if not _is_broad_country(cached.name, cached.country or country):
                return cached
        except TypeError:
            pass

    resolved = _from_nominatim(name, country)
    if resolved is None:
        resolved = _from_wikipedia(name, country)
    if resolved is None:
        resolved = _from_wikidata(name, country)
    if resolved is None:
        resolved = _from_web_search(name, country)
    # There is deliberately no name-derived fallback. Reading a city out of the
    # institution's name produces "Queens" for Queen's University, which is not
    # a city at all, and "Macalester" for a college in Saint Paul. A plausible
    # wrong city is worse than no city: the caller can say "country-level only",
    # but it cannot detect a budget costed against a place that does not exist.

    if resolved is not None:
        cache.set(
            key,
            {
                "name": resolved.name,
                "country": resolved.country,
                "method": resolved.method,
                "source_url": resolved.source_url,
                "region": resolved.region,
            },
            namespace="city",
            ttl_seconds=CITY_TTL,
            db_path=db_path,
        )
    return resolved
