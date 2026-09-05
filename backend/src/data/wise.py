"""Wise cost-of-living pages and their small, stable public data surface.

Wise publishes the headline monthly estimate and an optional six-part
``Distribution of Expenses`` on server-rendered cost-of-living pages.  This
adapter deliberately reads only those fields.  It does not depend on the
autocomplete widget or on a Wise API, which keeps it usable from AWS Lambda.

The site displays GBP on ``/gb`` pages.  The product uses a fixed GBP->SGD
rate so the result is deterministic and clearly labelled as an estimate.
Override ``WISE_GBP_TO_SGD`` when the project owner wants to refresh the
planning rate without changing code.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import quote

from graph.config import env
from services import cache

BASE_URL = "https://wise.com/gb/cost-of-living"
# How long one Wise page may take. 20s suited a workstation, where the app's own
# turn deadline is 110s and waiting beats an unnecessary refusal. Behind API
# Gateway the whole request dies at ~30s, and a fetch that outlives the gateway
# hands the student a timeout instead of an answer - strictly worse than the
# honest "unavailable" it replaces. fetch() also tries several city slugs in
# sequence, so the worst case is this value times the number of candidates.
# Environment-tunable so the deployed value can change without rebuilding a
# 1.3 GB image, exactly as the search timeouts in data/search.py already are.
TIMEOUT_SECONDS = float(env("WISE_TIMEOUT_SECONDS", "8") or 8)
GBP_TO_SGD = float(env("WISE_GBP_TO_SGD", "1.72") or "1.72")
WISE_TTL = int(env("WISE_TTL_SECONDS", str(24 * cache.DAY)) or 24 * cache.DAY)
USER_AGENT = "Mozilla/5.0 (compatible; NTU-Exchange-Planner/2.0)"

COUNTRY_ALIASES = {
    "KOREA": "south-korea",
    "KOREA, REPUBLIC OF": "south-korea",
    "REPUBLIC OF KOREA": "south-korea",
    "SOUTH KOREA": "south-korea",
    "UNITED KINGDOM": "united-kingdom",
    "UK": "united-kingdom",
    "GREAT BRITAIN": "united-kingdom",
    "UNITED STATES": "united-states",
    "UNITED STATES OF AMERICA": "united-states",
    "USA": "united-states",
    "US": "united-states",
    "CZECHIA": "czech-republic",
    "CZECH REPUBLIC": "czech-republic",
    "HONG KONG": "hong-kong",
    "MACAO": "macao-china",
    "MACAU": "macao-china",
    "TURKIYE": "turkey",
    "TÜRKIYE": "turkey",
    "TURKEY": "turkey",
}

# Wise's US city slugs use postal abbreviations, e.g. Cambridge, MA.
US_STATE_ABBREVIATIONS = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy", "district of columbia": "dc",
}

_MONEY = re.compile(r"£\s*([\d,]+(?:\.\d+)?)")
_DISTRIBUTION = (
    "Property", "Groceries", "Eating Out", "Transportation", "Utilities", "Leisure"
)


@dataclass
class Expense:
    label: str
    percentage: int
    amount_sgd: float


@dataclass
class CostOfLiving:
    location: str
    country: str
    url: str
    monthly_gbp: float | None = None
    monthly_sgd: float | None = None
    expenses: list[Expense] = field(default_factory=list)
    limited_data: bool = False
    level: str = "city"
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.monthly_sgd is not None and self.monthly_sgd > 0


def _text(page: str) -> str:
    page = re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", page or "", flags=re.IGNORECASE | re.DOTALL)
    page = re.sub(r"<[^>]+>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(page)).strip()


def _parse_money(value: str) -> float | None:
    match = _MONEY.search(value or "")
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse(page: str, location: str, country: str, url: str, level: str = "city") -> CostOfLiving:
    """Parse a Wise page without requiring a browser or JavaScript."""
    text = _text(page)
    result = CostOfLiving(location=location, country=country, url=url, level=level)
    result.limited_data = "limited data for this location" in text.lower()

    total_match = re.search(r"(£\s*[\d,]+(?:\.\d+)?)\s+Average cost per month", text, re.IGNORECASE)
    if not total_match:
        result.error = (
            "Wise has limited data for this location."
            if result.limited_data
            else "Wise published no monthly cost for this location."
        )
        return result

    result.monthly_gbp = _parse_money(total_match.group(1))
    if result.monthly_gbp is None:
        result.error = "Wise published an unreadable monthly cost for this location."
        return result
    result.monthly_sgd = round(result.monthly_gbp * GBP_TO_SGD, 2)

    # Use the first occurrence of each label, which is the summary distribution
    # before Wise repeats it in the responsive/mobile markup.
    seen: set[str] = set()
    for label in _DISTRIBUTION:
        match = re.search(rf"\b{re.escape(label)}\b\s+(\d{{1,3}})\s*%", text, re.IGNORECASE)
        if not match or label in seen:
            continue
        percentage = int(match.group(1))
        result.expenses.append(
            Expense(label=label, percentage=percentage, amount_sgd=round(result.monthly_sgd * percentage / 100, 2))
        )
        seen.add(label)
    if result.limited_data:
        # A page can contain partial rent/salary rows despite lacking a usable
        # aggregate. It must still trigger the country fallback.
        result.monthly_gbp = result.monthly_sgd = None
        result.expenses = []
    return result


def country_slug(country: str | None) -> str:
    raw = (country or "").strip()
    if not raw:
        return ""
    return COUNTRY_ALIASES.get(raw.upper(), _slug(raw))


def _slug(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.strip().lower())
    ascii_value = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")


def location_slugs(location: str, country: str | None) -> list[str]:
    """Generate Wise candidates in autocomplete-equivalent priority order.

    A stored ``Cambridge, Massachusetts`` therefore tries ``cambridge-ma``
    before ``cambridge``. The country path disambiguates identical city names
    such as Cambridge in Canada, the UK, and the US.
    """
    raw = re.sub(r"\s+", " ", (location or "").strip())
    if not raw:
        return []
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    city = parts[0] if parts else raw
    region = parts[1] if len(parts) > 1 else ""
    candidates: list[str] = []
    if region and country_slug(country) == "united-states":
        abbreviation = US_STATE_ABBREVIATIONS.get(region.lower(), region.lower())
        candidates.append(_slug(f"{city}-{abbreviation}"))
    candidates.append(_slug(raw))
    candidates.append(_slug(city))
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _cache_payload(result: CostOfLiving) -> dict:
    return {
        "location": result.location, "country": result.country, "url": result.url,
        "monthly_gbp": result.monthly_gbp, "monthly_sgd": result.monthly_sgd,
        "expenses": [expense.__dict__ for expense in result.expenses],
        "limited_data": result.limited_data, "level": result.level, "error": result.error,
    }


def _from_payload(payload: dict) -> CostOfLiving:
    return CostOfLiving(
        location=payload["location"], country=payload["country"], url=payload["url"],
        monthly_gbp=payload.get("monthly_gbp"), monthly_sgd=payload.get("monthly_sgd"),
        expenses=[Expense(**item) for item in payload.get("expenses", [])],
        limited_data=bool(payload.get("limited_data")), level=payload.get("level", "city"),
        error=payload.get("error"),
    )


def fetch(location: str, country: str | None, db_path=None) -> CostOfLiving:
    """Fetch the best city page for a location, in the supplied country."""
    name = (location or "").strip()
    country_name = (country or "").strip()
    country_path = country_slug(country_name)
    slugs = location_slugs(name, country_name)
    url = f"{BASE_URL}/{country_path}/{slugs[0] if slugs else ''}"
    if not name or not country_path or not slugs:
        return CostOfLiving(name, country_name, url, error="No searchable Wise location was provided.")
    key = cache.make_key("wise_col", country_path, "|".join(slugs), GBP_TO_SGD)
    hit = cache.get(key, db_path=db_path)
    if hit:
        try:
            return _from_payload(hit)
        except (KeyError, TypeError):
            pass

    import httpx

    result: CostOfLiving | None = None
    last_exception: Exception | None = None
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            for slug in slugs:
                candidate_url = f"{BASE_URL}/{country_path}/{quote(slug)}"
                try:
                    response = client.get(candidate_url)
                except Exception as exc:  # noqa: BLE001 - try the next Wise candidate
                    last_exception = exc
                    continue
                if response.status_code != 200:
                    result = CostOfLiving(name, country_name, candidate_url, error=f"Wise returned HTTP {response.status_code}.")
                    continue
                parsed = parse(response.text, name, country_name, candidate_url)
                if parsed.usable:
                    result = parsed
                    break
                result = parsed
    except Exception as exc:  # noqa: BLE001 - upstream failure is a normal lane state
        last_exception = exc

    result = result or CostOfLiving(
        name,
        country_name,
        url,
        error=(
            f"Wise was unreachable ({last_exception.__class__.__name__})."
            if last_exception is not None
            else "Wise returned no usable data."
        ),
    )
    if result.usable or result.limited_data:
        cache.set(key, _cache_payload(result), namespace="wise_col", ttl_seconds=WISE_TTL, db_path=db_path)
    return result


def fetch_country(country: str | None, db_path=None) -> CostOfLiving:
    """Fetch the aggregate Wise page used when a city is incomplete/unavailable."""
    name = (country or "").strip()
    path = country_slug(name)
    url = f"{BASE_URL}/{path}" if path else BASE_URL
    if not path:
        return CostOfLiving("", name, url, level="country", error="No country was provided.")
    key = cache.make_key("wise_country", path, GBP_TO_SGD)
    hit = cache.get(key, db_path=db_path)
    if hit:
        try:
            return _from_payload(hit)
        except (KeyError, TypeError):
            pass
    import httpx
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(url)
        if response.status_code != 200:
            return CostOfLiving(name, name, url, level="country", error=f"Wise returned HTTP {response.status_code}.")
        result = parse(response.text, name, name, url, level="country")
    except Exception as exc:  # noqa: BLE001
        return CostOfLiving(name, name, url, level="country", error=f"Wise was unreachable ({exc.__class__.__name__}).")
    if result.usable:
        cache.set(key, _cache_payload(result), namespace="wise_country", ttl_seconds=WISE_TTL, db_path=db_path)
    return result
