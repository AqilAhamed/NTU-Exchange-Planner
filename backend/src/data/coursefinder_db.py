"""Read-only access to ``coursefinder.db``.

Three rules hold everywhere in this module:

1. The connection is opened with ``mode=ro``, so a bug cannot corrupt the 187 MB
   database that nothing else can regenerate.
2. Every value that reaches SQL is a bound parameter. No SQL is ever generated
   by a language model, and no user text is interpolated into a statement.
3. De-duplication happens *inside* SQL, before ``LIMIT``/``OFFSET``. Filtering
   rows out in Python after the database has already paginated makes pages
   under-fill and offsets drift, so "show more" starts skipping rows.

Connections are per-call rather than one shared handle: FastAPI runs sync
endpoints on a threadpool, and a single sqlite connection shared across those
threads is a race waiting to happen.
"""

import re
import sqlite3
import time
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from graph.config import coursefinder_db_path
from graph.domain import MappingRow, Programme, UniversityCard

APPROVED = "Approved"
MAX_LIMIT = 50
DEFAULT_PAGE = 6
DEFAULT_PREVIEW = 5


class CoursefinderUnavailable(RuntimeError):
    """Raised when the database file is missing or cannot be opened read-only."""


@contextmanager
def connect(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Open a short-lived read-only connection.

    ``db_path`` is injectable so tests can point at a fixture database.
    """
    path = Path(db_path) if db_path is not None else coursefinder_db_path()
    if not path.exists():
        raise CoursefinderUnavailable(f"Coursefinder database not found at {path}")
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


_STAT_TABLES = ("universities", "school_programmes", "mappings", "submissions", "submission_fields")


# Five full-table counts over ~700k rows, so a health probe that scans on every
# call is its own outage. But a probe that caches forever is worse: it keeps
# answering "ok" long after the database has gone away, which is exactly the
# moment the answer matters. Hence a short TTL rather than lru_cache.
DB_STATS_TTL_SECONDS = 30.0
_stats_cache: dict[str | None, tuple[float, dict[str, object]]] = {}


def db_stats(db_path: str | None = None) -> dict[str, object]:
    """Row counts plus the resolved path, for the health endpoint."""
    cached = _stats_cache.get(db_path)
    if cached is not None and (time.monotonic() - cached[0]) < DB_STATS_TTL_SECONDS:
        return cached[1]

    path = Path(db_path) if db_path else coursefinder_db_path()
    stats: dict[str, object] = {"coursefinder_db": str(path), "available": False}
    try:
        with connect(path) as conn:
            for table in _STAT_TABLES:
                row = conn.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()
                stats[table] = int(row["n"])
            stats["available"] = True
    except (CoursefinderUnavailable, sqlite3.Error) as exc:
        stats["error"] = str(exc)
    _stats_cache[db_path] = (time.monotonic(), stats)
    return stats


# --- helpers --------------------------------------------------------------


def clamp_limit(limit: int | None, default: int = DEFAULT_PAGE) -> int:
    """Bound a caller-supplied page size. A URL cannot ask for the whole table."""
    if limit is None:
        return default
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_LIMIT))


def clamp_offset(offset: int | None) -> int:
    try:
        value = int(offset or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def coerce_credits(raw: Any) -> float:
    """Turn ``mappings.au`` (TEXT) into ``MappingRow.credits`` (number).

    463 rows hold an empty string and the column is free text, so this must
    never raise: an unreadable value means "not published", which is 0, not a
    500 for the whole page.
    """
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(",", ".")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    # Salvage a leading number from things like "3 AU" or "4.0 (approx)".
    digits = ""
    for char in text:
        if char.isdigit() or (char == "." and "." not in digits):
            digits += char
        elif digits:
            break
    try:
        return float(digits) if digits else 0.0
    except ValueError:
        return 0.0


# The order a student reads a shortlist in: degree requirements first, then
# electives that count toward the major, then broadening electives.
_TYPE_RANK_SQL = """
    CASE UPPER(TRIM(COALESCE(m.ntu_module_type, '')))
        WHEN 'CORE' THEN 0
        WHEN 'GER-CORE' THEN 0
        WHEN 'MAJOR-PE' THEN 1
        WHEN '2ND SPEC-PE' THEN 1
        WHEN 'BDE' THEN 2
        WHEN 'UE' THEN 2
        ELSE 3
    END
"""

# One physical mapping can appear in many academic years. Keep the newest.
_DEDUPE_SQL = """
    ROW_NUMBER() OVER (
        PARTITION BY m.university_id,
                     UPPER(TRIM(COALESCE(m.ntu_module, ''))),
                     UPPER(TRIM(COALESCE(m.host_module, '')))
        ORDER BY m.year DESC, m.sem DESC, m.mapping_id DESC
    )
"""


def _mapping_row(row: sqlite3.Row) -> MappingRow:
    return MappingRow(
        mapping_id=int(row["mapping_id"]),
        host_module_code=(row["host_module"] or "").strip(),
        host_module_title=(row["host_module_title"] or "").strip(),
        ntu_module_code=(row["ntu_module"] or "").strip(),
        ntu_module_title=(row["ntu_module_title"] or "").strip(),
        ntu_module_type=(row["ntu_module_type"] or "").strip(),
        credits=coerce_credits(row["au"]),
        year=(row["year"] or "").strip(),
        sem=(row["sem"] or "").strip(),
        has_details=bool(row["has_details"]),
    )


def _placeholders(values: Sequence[Any]) -> str:
    """Build ``?, ?, ?`` for an IN clause.

    Only question marks are generated; the values themselves stay bound.
    """
    return ", ".join("?" for _ in values)


def _mapping_filters(
    module_codes: Sequence[str] | None = None,
    exclude_module_types: Sequence[str] | None = None,
    include_module_types: Sequence[str] | None = None,
    restored_module_types: Sequence[str] | None = None,
) -> tuple[str, list[str]]:
    """Build safe, reusable filters for a student's mapping constraints.

    ``include_module_types`` is a type whitelist and therefore remains an AND
    constraint when combined with module codes. ``restored_module_types`` is a
    different operation: it represents a type brought back by a follow-up such
    as ``show BDEs`` after it was excluded. Those mappings are additive to an
    active module-code filter and must be joined with OR. Without a module-code
    filter, restoring a type only removes the exclusion; an explicit whitelist
    remains the positive constraint.
    """
    clauses: list[str] = []
    params: list[str] = []
    codes = [str(value).strip().upper().replace(" ", "") for value in (module_codes or []) if str(value).strip()]
    excluded = [str(value).strip().upper() for value in (exclude_module_types or []) if str(value).strip()]
    included = [str(value).strip().upper() for value in (include_module_types or []) if str(value).strip()]
    restored = [str(value).strip().upper() for value in (restored_module_types or []) if str(value).strip()]

    code_expression = ""
    if codes:
        code_expression = (
            "UPPER(REPLACE(TRIM(COALESCE(m.ntu_module, '')), ' ', '')) "
            f"IN ({_placeholders(codes)})"
        )

    restored_expressions: list[str] = []
    restored_params: list[str] = []
    for value in restored:
        pattern = f"{value.replace('%', '%%')}%"
        restored_expressions.append(
            "(UPPER(TRIM(COALESCE(m.ntu_module_type, ''))) LIKE ? "
            "OR UPPER(TRIM(COALESCE(m.ntu_module, ''))) LIKE ?)"
        )
        restored_params.extend([pattern, pattern])

    if code_expression and restored_expressions:
        clauses.append(f"AND ({code_expression} OR {' OR '.join(restored_expressions)})")
        params.extend(codes)
        params.extend(restored_params)
    elif code_expression:
        clauses.append(f"AND {code_expression}")
        params.extend(codes)
    if excluded:
        # Coursefinder contains both the bare token ("BDE") and occasional
        # decorated labels (for example "BDE - Broadening Elective"). A
        # prefix comparison keeps an exclusion absolute across those shapes,
        # while still using bound parameters.
        for value in excluded:
            pattern = f"{value.replace('%', '%%')}%"
            clauses.append(
                "AND UPPER(TRIM(COALESCE(m.ntu_module_type, ''))) NOT LIKE ? "
                "AND UPPER(TRIM(COALESCE(m.ntu_module, ''))) NOT LIKE ?"
            )
            params.extend([pattern, pattern])
    if included:
        clauses.append(
            f"AND (UPPER(TRIM(COALESCE(m.ntu_module_type, ''))) IN ({_placeholders(included)}) "
            f"OR UPPER(TRIM(COALESCE(m.ntu_module, ''))) IN ({_placeholders(included)}))"
        )
        params.extend([*included, *included])
    return "\n              ".join(clauses), params


# --- programmes -----------------------------------------------------------


def all_programmes(db_path: Path | str | None = None) -> list[Programme]:
    """Every NTU programme in the database, alphabetical by name."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT code, name FROM school_programmes ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [Programme(code=r["code"].strip(), name=r["name"].strip()) for r in rows]


def all_countries(db_path: Path | str | None = None) -> list[str]:
    """Every country that has at least one partner university."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT country FROM universities "
            "WHERE TRIM(country) <> '' ORDER BY country"
        ).fetchall()
    return [r["country"].strip() for r in rows]


def programme_types(db_path: Path | str | None = None) -> list[str]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT programme_type FROM mappings ORDER BY programme_type"
        ).fetchall()
    return [r["programme_type"] for r in rows]


# --- shortlist ------------------------------------------------------------


def eligible_universities(
    school_code: str,
    programme_type: str = "GEMX",
    country: str | None = None,
    countries: Sequence[str] | None = None,
    offset: int = 0,
    limit: int = DEFAULT_PAGE,
    preview: int = DEFAULT_PREVIEW,
    module_codes: Sequence[str] | None = None,
    exclude_module_types: Sequence[str] | None = None,
    include_module_types: Sequence[str] | None = None,
    restored_module_types: Sequence[str] | None = None,
    db_path: Path | str | None = None,
) -> tuple[list[UniversityCard], int, bool]:
    """Partner universities with *approved* mappings for this programme.

    Returns ``(cards, total, has_more)``. ``approved_count`` is the
    de-duplicated count, so it agrees with what paging through
    :func:`approved_mappings` actually yields — a card promising 118 mappings
    that runs out at 60 is worse than no number at all.
    """
    limit = clamp_limit(limit)
    offset = clamp_offset(offset)
    preview = clamp_limit(preview, DEFAULT_PREVIEW)
    country_filter = (country or "").strip().upper() or None
    country_filters = [str(value).strip().upper() for value in (countries or []) if str(value).strip()]

    mapping_filter, mapping_params = _mapping_filters(
        module_codes, exclude_module_types, include_module_types, restored_module_types
    )
    params: list[Any] = [school_code, APPROVED, programme_type, *mapping_params]
    country_clause = ""
    if country_filters:
        country_clause = f"AND UPPER(u.country) IN ({_placeholders(country_filters)})"
        params.extend(country_filters)
    elif country_filter:
        country_clause = "AND UPPER(u.country) = ?"
        params.append(country_filter)

    distinct_cte = f"""
        WITH approved AS (
            SELECT m.university_id AS university_id
            FROM mappings m
            WHERE m.school_code_queried = ?
              AND m.status = ?
              AND m.programme_type = ?
              {mapping_filter}
            GROUP BY m.university_id,
                     UPPER(TRIM(COALESCE(m.ntu_module, ''))),
                     UPPER(TRIM(COALESCE(m.host_module, '')))
        ),
        totals AS (
            SELECT u.university_id, u.name, u.country, u.city_state, COUNT(*) AS approved_count
            FROM approved a
            JOIN universities u ON u.university_id = a.university_id
            WHERE 1 = 1 {country_clause}
            GROUP BY u.university_id, u.name, u.country, u.city_state
        )
    """

    with connect(db_path) as conn:
        total = int(conn.execute(distinct_cte + "SELECT COUNT(*) AS n FROM totals", params).fetchone()["n"])
        rows = conn.execute(
            distinct_cte
            + "SELECT * FROM totals ORDER BY approved_count DESC, name COLLATE NOCASE LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()

        previews = _previews_for(
            conn,
            [int(r["university_id"]) for r in rows],
            school_code=school_code,
            programme_type=programme_type,
            per_university=preview,
            module_codes=module_codes,
            exclude_module_types=exclude_module_types,
            include_module_types=include_module_types,
            restored_module_types=restored_module_types,
        )

    cards = [
        UniversityCard(
            university_id=int(r["university_id"]),
            name=(r["name"] or "").strip(),
            country=(r["country"] or "").strip(),
            city_state=(r["city_state"] or "").strip() or None,
            approved_count=int(r["approved_count"]),
            programme_type=programme_type,
            mappings_preview=previews.get(int(r["university_id"]), []),
            preview_shown=len(previews.get(int(r["university_id"]), [])),
        )
        for r in rows
    ]
    return cards, total, offset + len(cards) < total


def _previews_for(
    conn: sqlite3.Connection,
    university_ids: Sequence[int],
    *,
    school_code: str,
    programme_type: str,
    per_university: int,
    module_codes: Sequence[str] | None = None,
    exclude_module_types: Sequence[str] | None = None,
    include_module_types: Sequence[str] | None = None,
    restored_module_types: Sequence[str] | None = None,
) -> dict[int, list[MappingRow]]:
    """Fetch every card's preview in one query rather than one query per card."""
    if not university_ids:
        return {}
    mapping_filter, mapping_params = _mapping_filters(
        module_codes, exclude_module_types, include_module_types, restored_module_types
    )
    sql = f"""
        WITH deduped AS (
            SELECT m.*,
                   {_DEDUPE_SQL} AS dedupe_rank,
                   {_TYPE_RANK_SQL} AS type_rank,
                   EXISTS (SELECT 1 FROM submissions s WHERE s.mapping_id = m.mapping_id) AS has_details
            FROM mappings m
            WHERE m.university_id IN ({_placeholders(university_ids)})
              AND m.school_code_queried = ?
              AND m.status = ?
              AND m.programme_type = ?
              {mapping_filter}
        ),
        ordered AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY university_id
                       ORDER BY type_rank, year DESC, sem DESC, ntu_module, host_module
                   ) AS row_in_university
            FROM deduped
            WHERE dedupe_rank = 1
        )
        SELECT * FROM ordered WHERE row_in_university <= ?
    """
    params = [*university_ids, school_code, APPROVED, programme_type, *mapping_params, per_university]
    grouped: dict[int, list[MappingRow]] = {}
    for row in conn.execute(sql, params):
        grouped.setdefault(int(row["university_id"]), []).append(_mapping_row(row))
    return grouped


def approved_mappings(
    university_id: int,
    school_code: str,
    programme_type: str = "GEMX",
    offset: int = 0,
    limit: int = 12,
    module_codes: Sequence[str] | None = None,
    exclude_module_types: Sequence[str] | None = None,
    include_module_types: Sequence[str] | None = None,
    restored_module_types: Sequence[str] | None = None,
    db_path: Path | str | None = None,
) -> list[MappingRow]:
    """One page of a university's approved mappings, ranked by module type.

    The window functions de-duplicate first and paginate second, so page 2
    continues exactly where page 1 stopped.
    """
    limit = clamp_limit(limit, 12)
    offset = clamp_offset(offset)
    mapping_filter, mapping_params = _mapping_filters(
        module_codes, exclude_module_types, include_module_types, restored_module_types
    )
    sql = f"""
        WITH deduped AS (
            SELECT m.*,
                   {_DEDUPE_SQL} AS dedupe_rank,
                   {_TYPE_RANK_SQL} AS type_rank,
                   EXISTS (SELECT 1 FROM submissions s WHERE s.mapping_id = m.mapping_id) AS has_details
            FROM mappings m
            WHERE m.university_id = ?
              AND m.school_code_queried = ?
              AND m.status = ?
              AND m.programme_type = ?
              {mapping_filter}
        )
        SELECT * FROM deduped
        WHERE dedupe_rank = 1
        ORDER BY type_rank, year DESC, sem DESC, ntu_module, host_module
        LIMIT ? OFFSET ?
    """
    params = [university_id, school_code, APPROVED, programme_type, *mapping_params, limit, offset]
    with connect(db_path) as conn:
        return [_mapping_row(row) for row in conn.execute(sql, params)]


def approved_mapping_count(
    university_id: int,
    school_code: str,
    programme_type: str = "GEMX",
    module_codes: Sequence[str] | None = None,
    exclude_module_types: Sequence[str] | None = None,
    include_module_types: Sequence[str] | None = None,
    restored_module_types: Sequence[str] | None = None,
    db_path: Path | str | None = None,
) -> int:
    """How many de-duplicated mappings paging will actually produce."""
    mapping_filter, mapping_params = _mapping_filters(
        module_codes, exclude_module_types, include_module_types, restored_module_types
    )
    sql = f"""
        SELECT COUNT(*) AS n FROM (
            SELECT 1 FROM mappings m
            WHERE m.university_id = ?
              AND m.school_code_queried = ?
              AND m.status = ?
              AND m.programme_type = ?
              {mapping_filter}
            GROUP BY UPPER(TRIM(COALESCE(m.ntu_module, ''))),
                     UPPER(TRIM(COALESCE(m.host_module, '')))
        )
    """
    with connect(db_path) as conn:
        row = conn.execute(
            sql, (university_id, school_code, APPROVED, programme_type, *mapping_params)
        ).fetchone()
    return int(row["n"])


def mapping_details(
    mapping_id: int, db_path: Path | str | None = None
) -> dict[str, str]:
    """The most recent student submission for a mapping, as ``{label: value}``.

    Later submissions supersede earlier ones, so only the highest
    ``submission_number`` is returned; merging them would present one student's
    syllabus notes alongside another's assessment breakdown as if they agreed.
    """
    with connect(db_path) as conn:
        submission = conn.execute(
            "SELECT submission_id FROM submissions WHERE mapping_id = ? "
            "ORDER BY submission_number DESC, submission_id DESC LIMIT 1",
            (mapping_id,),
        ).fetchone()
        if submission is None:
            return {}
        rows = conn.execute(
            "SELECT label, value FROM submission_fields WHERE submission_id = ?",
            (int(submission["submission_id"]),),
        ).fetchall()

    details: dict[str, str] = {}
    for row in rows:
        value = (row["value"] or "").strip()
        if value:
            details[(row["label"] or "").strip()] = value
    return details


def university(university_id: int, db_path: Path | str | None = None) -> dict[str, str] | None:
    """Name, country and enriched city/state for one university."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT university_id, name, country, city_state FROM universities WHERE university_id = ?",
            (university_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "university_id": int(row["university_id"]),
        "name": (row["name"] or "").strip(),
        "country": (row["country"] or "").strip(),
        "city_state": (row["city_state"] or "").strip(),
    }


def university_by_name(
    name: str, country: str | None = None, db_path: Path | str | None = None
) -> dict[str, str] | None:
    """Resolve a named university to its database location when unambiguous."""
    clauses = ["UPPER(TRIM(name)) = UPPER(TRIM(?))"]
    params: list[str] = [name]
    if country:
        clauses.append("UPPER(TRIM(country)) = UPPER(TRIM(?))")
        params.append(country)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT university_id, name, country, city_state FROM universities WHERE "
            + " AND ".join(clauses) + " ORDER BY university_id LIMIT 2",
            params,
        ).fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    return {
        "university_id": int(row["university_id"]),
        "name": (row["name"] or "").strip(),
        "country": (row["country"] or "").strip(),
        "city_state": (row["city_state"] or "").strip(),
    }


@lru_cache(maxsize=4)
def all_university_names(db_path: str | None = None) -> tuple[tuple[int, str, str], ...]:
    """Every partner university as ``(id, name, country)``.

    Cached because the intake fallback fuzzy-matches every incoming message
    against all 558 names, and re-reading the table per keystroke-length
    message would be the slowest thing in the request.
    """
    with connect(Path(db_path) if db_path else None) as conn:
        rows = conn.execute(
            "SELECT university_id, name, country FROM universities ORDER BY name"
        ).fetchall()
    return tuple(
        (int(r["university_id"]), (r["name"] or "").strip(), (r["country"] or "").strip())
        for r in rows
    )


_UNIVERSITY_QUERY_STOPWORDS = frozenset(
    {
        "a", "about", "another", "at", "can", "cost", "does", "for", "from",
        "how", "i", "in", "is", "just", "know", "living", "me", "monthly",
        "more", "much", "my", "of", "on", "per", "please", "tell", "the", "to",
        "what", "which", "where", "with", "would", "uni", "unis", "univ",
        "university", "universities",
    }
)
_UNIVERSITY_STRUCTURAL_WORDS = frozenset(
    {
        "academy", "college", "institute", "national", "polytechnic", "school",
        "state", "technology", "the", "university",
    }
)


def _university_search_tokens(value: str) -> tuple[str, ...]:
    """Accent-insensitive tokens used for conversational university lookup."""
    folded = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(ch for ch in folded if not unicodedata.combining(ch))
    tokens = re.findall(r"[a-z0-9]+", ascii_value.casefold())
    aliases = {"uni": "university", "unis": "universities", "univ": "university"}
    return tuple(aliases.get(token, token) for token in tokens)


def _university_display_key(name: str) -> str:
    """Collapse faculty/campus rows to one institution for conversational choices."""
    base = re.split(r"\s+-\s+", name or "", maxsplit=1)[0].strip()
    tokens = list(_university_search_tokens(base))
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    return " ".join(tokens)


def match_universities(query: str, db_path: str | None = None) -> list[dict[str, str | int]]:
    """Return database universities named by a natural-language fragment.

    This intentionally returns *all* institutions sharing a meaningful part
    of the name. Callers may safely select a unique result, but must ask the
    student to choose when the list has more than one institution.
    """
    query_tokens = set(_university_search_tokens(query))
    meaningful = {
        token
        for token in query_tokens
        if len(token) >= 3
        and token not in _UNIVERSITY_QUERY_STOPWORDS
        and token not in _UNIVERSITY_STRUCTURAL_WORDS
    }
    if not meaningful:
        return []

    rows = all_university_names(db_path)
    normalised_query = " ".join(_university_search_tokens(query))
    matched: dict[str, tuple[int, str, str]] = {}
    for university_id, name, country in rows:
        base_key = _university_display_key(name)
        full_key = " ".join(_university_search_tokens(name))
        base_name = re.split(r"\s+-\s+", name or "", maxsplit=1)[0].strip()
        base_name = re.sub(r"\s*\([^)]*\)\s*$", "", base_name).strip()
        name_tokens = set(_university_search_tokens(base_name))
        distinctive = name_tokens - _UNIVERSITY_STRUCTURAL_WORDS

        # Full names and the base institution name win over fragment matching.
        direct = full_key in normalised_query or (base_key and base_key in normalised_query)
        # Parenthesised abbreviations such as UC3M/EPFL are explicit aliases.
        acronym = any(
            re.sub(r"[^a-z0-9]", "", value.casefold()) in meaningful
            for value in re.findall(r"\(([^)]{2,14})\)", name)
        )
        partial = bool(meaningful & distinctive)
        if not (direct or acronym or partial):
            continue

        key = _university_display_key(name)
        previous = matched.get(key)
        candidate = (university_id, name, country)
        # Prefer the shortest row when Coursefinder has multiple faculty rows
        # for the same institution; keep the full canonical text otherwise.
        if previous is None or len(name) < len(previous[1]):
            matched[key] = candidate

    return [
        {"university_id": university_id, "name": name, "country": country}
        for university_id, name, country in sorted(matched.values(), key=lambda row: row[1].casefold())
    ]
