"""Resolving destination phrases into countries the database actually holds.

The 34 countries come from ``universities.country`` and are stored uppercase.
Aliases exist only for names the database spells differently from everyday
speech ("TURKIYE", "KOREA, REPUBLIC OF"), and each one is verified against the
database before it can be used.

The trap this module exists to avoid: a region word must not be treated as a
literal country. "Somewhere in Europe" expands to the represented European
countries; it must never silently narrow a shortlist to one fuzzy match.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from data.coursefinder_db import all_countries

# Regions, continents and "anywhere". These are real preferences, but they are
# not country filters, so they resolve to None and the shortlist stays open.
REGION_WORDS = frozenset(
    {
        "anywhere",
        "any",
        "asia",
        "asian",
        "europe",
        "european",
        "eu",
        "scandinavia",
        "scandinavian",
        "nordics",
        "nordic",
        "america",
        "americas",
        "north america",
        "south america",
        "latin america",
        "africa",
        "oceania",
        "middle east",
        "abroad",
        "overseas",
        "somewhere",
        "world",
        "worldwide",
        "global",
    }
)

# Region membership is applied only to countries that Coursefinder actually
# contains. The database remains the source of truth for the final filter;
# this table supplies the geographic meaning of a student's preference.
_REGION_COUNTRIES: dict[str, frozenset[str]] = {
    "europe": frozenset(
        {
            "AUSTRIA", "BELGIUM", "CZECHIA", "DENMARK", "FINLAND", "FRANCE",
            "GERMANY", "HUNGARY", "IRELAND", "ITALY", "LUXEMBOURG", "NETHERLANDS",
            "NORWAY", "POLAND", "SPAIN", "SWEDEN", "SWITZERLAND", "TURKIYE",
            "UNITED KINGDOM",
        }
    ),
    "asia": frozenset(
        {
            "CHINA", "HONG KONG", "INDONESIA", "JAPAN", "KOREA, REPUBLIC OF",
            "MACAO", "SINGAPORE", "TAIWAN", "THAILAND", "VIETNAM",
        }
    ),
    "north america": frozenset({"CANADA", "UNITED STATES OF AMERICA"}),
    "americas": frozenset({"CANADA", "UNITED STATES OF AMERICA"}),
    "south america": frozenset(),
    "latin america": frozenset(),
    "oceania": frozenset({"AUSTRALIA", "NEW ZEALAND"}),
    "africa": frozenset(),
    "middle east": frozenset(),
    "scandinavia": frozenset({"DENMARK", "NORWAY", "SWEDEN"}),
    "nordics": frozenset({"DENMARK", "FINLAND", "NORWAY", "SWEDEN"}),
}

_REGION_ALIASES = {
    "european": "europe",
    "eu": "europe",
    "asian": "asia",
    "scandinavian": "scandinavia",
    "nordic": "nordics",
}

# Everyday names for countries the database spells differently.
_ALIASES: dict[str, str] = {
    "usa": "UNITED STATES OF AMERICA",
    "us": "UNITED STATES OF AMERICA",
    "u s": "UNITED STATES OF AMERICA",
    "united states": "UNITED STATES OF AMERICA",
    "the states": "UNITED STATES OF AMERICA",
    "uk": "UNITED KINGDOM",
    "britain": "UNITED KINGDOM",
    "great britain": "UNITED KINGDOM",
    "england": "UNITED KINGDOM",
    "scotland": "UNITED KINGDOM",
    "wales": "UNITED KINGDOM",
    "korea": "KOREA, REPUBLIC OF",
    "south korea": "KOREA, REPUBLIC OF",
    "republic of korea": "KOREA, REPUBLIC OF",
    "turkey": "TURKIYE",
    "czech republic": "CZECHIA",
    "czech": "CZECHIA",
    "holland": "NETHERLANDS",
    "the netherlands": "NETHERLANDS",
    "hong kong sar": "HONG KONG",
    "hk": "HONG KONG",
    "macau": "MACAO",
    "brunei darussalam": "BRUNEI",
    "nz": "NEW ZEALAND",
    "aus": "AUSTRALIA",
    "sg": "SINGAPORE",
}


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# Country names are deliberately separate from the partner-country index.
# Coursefinder contains only destinations with NTU mappings, but intake still
# needs to remember a real country that has no mapping so the next turn can
# return an honest zero-result answer instead of broadening to every partner.
_COUNTRY_QUERY_NAMES = frozenset(
    {
        "afghanistan", "albania", "algeria", "andorra", "angola",
        "antigua and barbuda", "argentina", "armenia", "australia", "austria",
        "azerbaijan", "bahamas", "bahrain", "bangladesh", "barbados", "belarus",
        "belgium", "belize", "benin", "bhutan", "bolivia", "bosnia and herzegovina",
        "botswana", "brazil", "brunei", "bulgaria", "burkina faso", "burundi",
        "cabo verde", "cambodia", "cameroon", "canada", "central african republic",
        "chad", "chile", "china", "colombia", "comoros", "congo",
        "costa rica", "cote d'ivoire", "croatia", "cuba", "cyprus", "czechia",
        "democratic republic of the congo", "denmark", "djibouti", "dominica",
        "dominican republic", "ecuador", "egypt", "el salvador",
        "equatorial guinea", "eritrea", "estonia", "eswatini", "ethiopia",
        "fiji", "finland", "france", "gabon", "gambia", "georgia", "germany",
        "ghana", "greece", "grenada", "guatemala", "guinea", "guinea-bissau",
        "guyana", "haiti", "honduras", "hungary", "iceland", "india",
        "indonesia", "iran", "iraq", "ireland", "israel", "italy", "jamaica",
        "japan", "jordan", "kazakhstan", "kenya", "kiribati", "kuwait",
        "kyrgyzstan", "laos", "latvia", "lebanon", "lesotho", "liberia",
        "libya", "liechtenstein", "lithuania", "madagascar", "malawi",
        "malaysia", "maldives", "mali", "malta", "marshall islands",
        "mauritania", "mauritius", "mexico", "micronesia", "moldova", "monaco",
        "mongolia", "montenegro", "morocco", "mozambique", "myanmar", "namibia",
        "nauru", "nepal", "netherlands", "new zealand", "nicaragua", "niger",
        "nigeria", "north korea", "north macedonia", "norway", "oman",
        "pakistan", "palau", "palestine", "panama", "papua new guinea",
        "paraguay", "peru", "philippines", "poland", "portugal", "qatar",
        "romania", "russia", "rwanda", "saint kitts and nevis", "saint lucia",
        "saint vincent and the grenadines", "samoa", "san marino",
        "sao tome and principe", "saudi arabia", "senegal", "serbia",
        "seychelles", "sierra leone", "slovakia", "slovenia", "solomon islands",
        "somalia", "south africa", "south korea", "south sudan", "spain",
        "sri lanka", "sudan", "suriname", "sweden", "switzerland", "syria",
        "taiwan", "tajikistan", "tanzania", "thailand", "timor-leste", "togo",
        "tonga", "trinidad and tobago", "tunisia", "turkey", "turkiye",
        "turkmenistan", "tuvalu", "uganda", "ukraine", "united arab emirates",
        "united kingdom", "united states", "uruguay", "uzbekistan", "vanuatu",
        "vatican city", "venezuela", "vietnam", "yemen", "zambia", "zimbabwe",
        "burma", "czech republic", "ivory coast", "kosovo", "macau", "uae",
        "uk", "usa",
    }
)


def is_recognized_country_name(text: str) -> bool:
    """Whether text is a real country name, including common variants."""
    cleaned = _normalise(text)
    return any(cleaned == _normalise(name) for name in _COUNTRY_QUERY_NAMES)


@lru_cache(maxsize=4)
def _index(db_path: str | None = None) -> tuple[tuple[str, ...], dict[str, str]]:
    countries = tuple(all_countries(Path(db_path) if db_path else None))
    lookup = {_normalise(c): c for c in countries}
    return countries, lookup


def countries(db_path: str | None = None) -> list[str]:
    """Every country with at least one partner university, as stored."""
    return list(_index(db_path)[0])


def _valid_aliases(db_path: str | None = None) -> dict[str, str]:
    _, lookup = _index(db_path)
    known = set(lookup.values())
    return {phrase: country for phrase, country in _ALIASES.items() if country in known}


def is_region(text: str) -> bool:
    """True when the phrase names a region rather than a country."""
    return _normalise(text) in REGION_WORDS


def resolve_region(text: str, db_path: str | None = None) -> tuple[str, list[str]] | None:
    """Resolve a region-shaped preference to ``(label, known countries)``.

    It accepts a region in a sentence (``universities somewhere in Europe``)
    and returns only countries present in this installation's database. A
    region with no represented countries is still returned so callers can
    explain the empty result instead of silently dropping the preference.
    """
    cleaned = _normalise(text)
    if not cleaned:
        return None

    region: str | None = None
    for phrase, canonical in sorted(_REGION_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", cleaned):
            region = canonical
            break
    if region is None:
        for candidate in sorted(_REGION_COUNTRIES, key=len, reverse=True):
            if re.search(rf"\b{re.escape(candidate)}\b", cleaned):
                region = candidate
                break
    if region is None:
        return None

    known = set(_index(db_path)[0])
    return region.title(), sorted(_REGION_COUNTRIES.get(region, frozenset()) & known)


def resolve_country(text: str, db_path: str | None = None) -> str | None:
    """The country this phrase names, or ``None``.

    ``None`` means "no country filter", which is the correct outcome for a
    region word, an unknown country, and an empty string alike.
    """
    cleaned = _normalise(text)
    if not cleaned or cleaned in REGION_WORDS:
        return None

    _, lookup = _index(db_path)

    # 1. The country exactly as the database stores it.
    if cleaned in lookup:
        return lookup[cleaned]

    # 2. A known everyday name.
    aliases = _valid_aliases(db_path)
    if cleaned in aliases:
        return aliases[cleaned]

    # 3. Either named inside a longer sentence. Longest phrase wins so
    #    "united states" is not beaten by "us".
    candidates: list[tuple[int, str]] = []
    for phrase, country in list(lookup.items()) + list(aliases.items()):
        if re.search(rf"\b{re.escape(phrase)}\b", cleaned):
            candidates.append((len(phrase), country))
    if candidates:
        return max(candidates)[1]

    # A sentence that only ever named a region carries no country filter.
    if any(re.search(rf"\b{re.escape(word)}\b", cleaned) for word in REGION_WORDS):
        return None

    # 4. Fuzzy, only for a near-spelling of a real country name.
    return _fuzzy(cleaned, lookup)


def _fuzzy(cleaned: str, lookup: dict[str, str]) -> str | None:
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover - rapidfuzz is a hard requirement
        return None
    best_score, best_country = 0.0, None
    for name, country in lookup.items():
        score = float(fuzz.ratio(cleaned, name))
        if score > best_score:
            best_score, best_country = score, country
    return best_country if best_score >= 88 else None
