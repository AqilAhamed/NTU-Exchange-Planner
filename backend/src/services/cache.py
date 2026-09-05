"""A durable TTL cache for everything fetched over the network.

Durable rather than in-process, for three reasons that all matter on demo day:

* it survives a restart, so the demo does not re-hit Terra Dotta or Wise
  every time the server reloads;
* it makes a run reproducible offline, which is what lets the integration
  tests assert behaviour without a network;
* it is the single biggest lever on the graded *token cost per run* metric.

Keys are versioned (``gem:v1:...``) so a parser change can invalidate a whole
namespace by bumping one string rather than by deleting a file.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.app_state_db import now
from services.store import active_store

DAY = 86_400
WEEK = 7 * DAY


def make_key(namespace: str, *parts: Any, version: str = "v1") -> str:
    """Build a versioned cache key: ``namespace:version:part:part``."""
    cleaned = [str(part).strip().lower().replace(":", "_") for part in parts if part is not None]
    return ":".join([namespace, version, *cleaned])


def _expired(fetched_at: str, ttl_seconds: int) -> bool:
    if ttl_seconds == 0:
        return False  # 0 means "never expires"
    if ttl_seconds < 0:
        return True  # a negative TTL is already past
    try:
        stamp = datetime.fromisoformat(fetched_at)
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - stamp > timedelta(seconds=ttl_seconds)


def get(key: str, db_path: Path | str | None = None) -> Any | None:
    """The cached payload, or ``None`` when absent, expired or unreadable."""
    row = active_store(db_path).cache_get(key)
    if row is None or _expired(row["fetched_at"], int(row["ttl_seconds"])):
        return None
    try:
        return json.loads(row["payload_json"])
    except ValueError:
        return None


def set(  # noqa: A001 - the cache verb reads better than set_value here
    key: str,
    payload: Any,
    *,
    namespace: str | None = None,
    ttl_seconds: int = DAY,
    db_path: Path | str | None = None,
) -> None:
    """Store a payload. Replaces any existing entry for the same key."""
    space = namespace or key.split(":", 1)[0]
    ttl = int(ttl_seconds)
    # DynamoDB deletes rows by an epoch-seconds TTL attribute. A ttl of 0 means
    # "never expires" here, so no expiry is written for it and the row stays.
    expires_at = int(time.time()) + ttl if ttl > 0 else None
    active_store(db_path).cache_put(
        key, space, json.dumps(payload, default=str), now(), ttl, expires_at
    )


def get_or_set(
    key: str,
    producer,
    *,
    namespace: str | None = None,
    ttl_seconds: int = DAY,
    db_path: Path | str | None = None,
) -> Any:
    """Return the cached value, otherwise call ``producer()`` and store it.

    A producer that raises is allowed to propagate: caching a failure would
    hide a broken adapter for a whole TTL.
    """
    hit = get(key, db_path=db_path)
    if hit is not None:
        return hit
    value = producer()
    if value is not None:
        set(key, value, namespace=namespace, ttl_seconds=ttl_seconds, db_path=db_path)
    return value


def invalidate(key: str, db_path: Path | str | None = None) -> None:
    active_store(db_path).cache_delete(key)


def clear_namespace(namespace: str, db_path: Path | str | None = None) -> int:
    """Drop every entry in a namespace. Used when a parser version changes."""
    return active_store(db_path).cache_clear_namespace(namespace)


def purge_expired(db_path: Path | str | None = None) -> int:
    """Remove entries whose TTL has passed. Safe to call at startup.

    A no-op on a backend that expires rows itself: DynamoDB deletes them from
    the table TTL attribute at no cost, so scanning to find them would be
    strictly more expensive than doing nothing.
    """
    store = active_store(db_path)
    if store.cache_self_expires():
        return 0
    removed = 0
    for row in store.cache_rows():
        if _expired(row["fetched_at"], int(row["ttl_seconds"])):
            store.cache_delete(row["key"])
            removed += 1
    return removed
