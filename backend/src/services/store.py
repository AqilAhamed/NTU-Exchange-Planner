"""Where sessions, chats and the response cache actually live.

SQLite locally, DynamoDB on Lambda, chosen by ``STORAGE_BACKEND``. Both sit
behind one narrow interface so :mod:`services.session_store` and
:mod:`services.cache` never learn which one they are talking to.

Two backends rather than a migration to DynamoDB, deliberately. The whole
product is built to run with no credentials and no network - that is how it was
developed, and it is the fallback the demo depends on. Replacing SQLite
outright would have made the local loop require an AWS account or a container,
trading away the thing that makes this app testable for nothing the deployment
needs.

The interface is deliberately dumb: it moves rows. Every decision about what a
row *means* - TTL expiry, chat-title rules, deletion tombstones - stays in the
caller that already owns it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from graph.config import env
from services.app_state_db import connect

# DynamoDB table names, overridable so a second stack can coexist in one
# account without colliding.
SESSIONS_TABLE_VAR = "NEX_SESSIONS_TABLE"
CHATS_TABLE_VAR = "NEX_CHATS_TABLE"
CACHE_TABLE_VAR = "NEX_CACHE_TABLE"

DEFAULT_SESSIONS_TABLE = "nex-sessions"
DEFAULT_CHATS_TABLE = "nex-chats"
DEFAULT_CACHE_TABLE = "nex-cache"


class Store(Protocol):
    """Row movement for the three things this app persists."""

    def session_get(self, session_id: str) -> str | None: ...
    def session_put(self, session_id: str, state_json: str, stamp: str) -> None: ...
    def session_delete(self, session_id: str) -> None: ...

    def chat_get(self, session_id: str) -> dict[str, Any] | None: ...
    def chat_create(self, session_id: str, title: str, stamp: str) -> None: ...
    def chat_set_title(self, session_id: str, title: str, stamp: str) -> bool: ...
    def chat_touch(self, session_id: str, stamp: str) -> None: ...
    def chat_list(self, limit: int) -> list[dict[str, Any]]: ...
    def chat_delete(self, session_id: str) -> bool: ...

    def cache_get(self, key: str) -> dict[str, Any] | None: ...
    def cache_put(
        self,
        key: str,
        namespace: str,
        payload_json: str,
        fetched_at: str,
        ttl_seconds: int,
        expires_at: int | None = None,
    ) -> None: ...
    def cache_delete(self, key: str) -> None: ...
    def cache_clear_namespace(self, namespace: str) -> int: ...
    def cache_rows(self) -> list[dict[str, Any]]: ...
    def cache_self_expires(self) -> bool: ...


class SqliteStore:
    """The local backend. Same SQL as before, moved behind the interface."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = db_path

    def cache_self_expires(self) -> bool:
        return False

    # --- sessions ---

    def session_get(self, session_id: str) -> str | None:
        with connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT state_json FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return row["state_json"] if row else None

    def session_put(self, session_id: str, state_json: str, stamp: str) -> None:
        with connect(self._db_path) as conn:
            cursor = conn.execute(
                "UPDATE sessions SET state_json = ?, updated_at = ? WHERE session_id = ?",
                (state_json, stamp, session_id),
            )
            if cursor.rowcount == 0:
                conn.execute(
                    "INSERT INTO sessions (session_id, state_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, state_json, stamp, stamp),
                )

    def session_delete(self, session_id: str) -> None:
        with connect(self._db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    # --- chats ---

    def chat_get(self, session_id: str) -> dict[str, Any] | None:
        with connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT session_id, title, folder, created_at, updated_at FROM chats "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def chat_create(self, session_id: str, title: str, stamp: str) -> None:
        with connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO chats (session_id, title, folder, created_at, updated_at) "
                "VALUES (?, ?, NULL, ?, ?)",
                (session_id, title, stamp, stamp),
            )

    def chat_set_title(self, session_id: str, title: str, stamp: str) -> bool:
        with connect(self._db_path) as conn:
            changed = conn.execute(
                "UPDATE chats SET title = ?, updated_at = ? WHERE session_id = ?",
                (title, stamp, session_id),
            ).rowcount
        return changed > 0

    def chat_touch(self, session_id: str, stamp: str) -> None:
        with connect(self._db_path) as conn:
            conn.execute(
                "UPDATE chats SET updated_at = ? WHERE session_id = ?", (stamp, session_id)
            )

    def chat_list(self, limit: int) -> list[dict[str, Any]]:
        with connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT session_id, title, folder, created_at, updated_at FROM chats "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def chat_delete(self, session_id: str) -> bool:
        with connect(self._db_path) as conn:
            deleted = conn.execute(
                "DELETE FROM chats WHERE session_id = ?", (session_id,)
            ).rowcount
        return deleted > 0

    # --- cache ---

    def cache_get(self, key: str) -> dict[str, Any] | None:
        with connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT payload_json, fetched_at, ttl_seconds FROM cache WHERE key = ?",
                (key,),
            ).fetchone()
        return dict(row) if row else None

    def cache_put(
        self,
        key: str,
        namespace: str,
        payload_json: str,
        fetched_at: str,
        ttl_seconds: int,
        expires_at: int | None = None,
    ) -> None:
        with connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO cache (key, namespace, payload_json, fetched_at, ttl_seconds) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "namespace=excluded.namespace, payload_json=excluded.payload_json, "
                "fetched_at=excluded.fetched_at, ttl_seconds=excluded.ttl_seconds",
                (key, namespace, payload_json, fetched_at, int(ttl_seconds)),
            )

    def cache_delete(self, key: str) -> None:
        with connect(self._db_path) as conn:
            conn.execute("DELETE FROM cache WHERE key = ?", (key,))

    def cache_clear_namespace(self, namespace: str) -> int:
        with connect(self._db_path) as conn:
            return conn.execute(
                "DELETE FROM cache WHERE namespace = ?", (namespace,)
            ).rowcount

    def cache_rows(self) -> list[dict[str, Any]]:
        with connect(self._db_path) as conn:
            rows = conn.execute("SELECT key, fetched_at, ttl_seconds FROM cache").fetchall()
        return [dict(row) for row in rows]


class DynamoStore:
    """The deployed backend. On-demand tables, never provisioned capacity.

    Credentials come from the Lambda execution role, so nothing here holds a
    key or reads one from the environment.
    """

    def __init__(self) -> None:
        import boto3

        # Same resolution order as the Bedrock provider, so one BEDROCK_REGION
        # cannot leave the tables in one region and inference in another.
        from graph.providers import BEDROCK_DEFAULT_REGION

        region = (
            env("BEDROCK_REGION")
            or env("AWS_REGION")
            or env("AWS_DEFAULT_REGION")
            or BEDROCK_DEFAULT_REGION
        )
        resource = boto3.resource("dynamodb", region_name=region)
        self._sessions = resource.Table(env(SESSIONS_TABLE_VAR, DEFAULT_SESSIONS_TABLE))
        self._chats = resource.Table(env(CHATS_TABLE_VAR, DEFAULT_CHATS_TABLE))
        self._cache = resource.Table(env(CACHE_TABLE_VAR, DEFAULT_CACHE_TABLE))

    def cache_self_expires(self) -> bool:
        # The table's TTL attribute deletes expired rows for free, which is what
        # replaces purge_expired() in the deployed build.
        return True

    # --- sessions ---

    def session_get(self, session_id: str) -> str | None:
        item = self._sessions.get_item(Key={"session_id": session_id}).get("Item")
        return item.get("state_json") if item else None

    def session_put(self, session_id: str, state_json: str, stamp: str) -> None:
        # UpdateItem, not PutItem: PutItem replaces the whole item and would
        # silently drop created_at on every save.
        self._sessions.update_item(
            Key={"session_id": session_id},
            UpdateExpression=(
                "SET state_json = :s, updated_at = :u, "
                "created_at = if_not_exists(created_at, :u)"
            ),
            ExpressionAttributeValues={":s": state_json, ":u": stamp},
        )

    def session_delete(self, session_id: str) -> None:
        self._sessions.delete_item(Key={"session_id": session_id})

    # --- chats ---

    def chat_get(self, session_id: str) -> dict[str, Any] | None:
        return self._chats.get_item(Key={"session_id": session_id}).get("Item")

    def chat_create(self, session_id: str, title: str, stamp: str) -> None:
        self._chats.put_item(
            Item={
                "session_id": session_id,
                "title": title,
                "folder": None,
                "created_at": stamp,
                "updated_at": stamp,
            }
        )

    def chat_set_title(self, session_id: str, title: str, stamp: str) -> bool:
        self._chats.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET title = :t, updated_at = :u",
            ExpressionAttributeValues={":t": title, ":u": stamp},
        )
        return True

    def chat_touch(self, session_id: str, stamp: str) -> None:
        self._chats.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET updated_at = :u",
            ExpressionAttributeValues={":u": stamp},
        )

    def chat_list(self, limit: int) -> list[dict[str, Any]]:
        # A Scan sorted in Python. At demo scale the chat list is tens of rows
        # and one Scan is far cheaper than carrying a GSI. If this ever holds
        # thousands of chats, add a GSI on a constant partition key sorted by
        # updated_at rather than raising the Scan limit.
        items = self._chats.scan(Limit=max(limit, 100)).get("Items", [])
        items.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
        return items[:limit]

    def chat_delete(self, session_id: str) -> bool:
        previous = self._chats.delete_item(
            Key={"session_id": session_id}, ReturnValues="ALL_OLD"
        ).get("Attributes")
        return bool(previous)

    # --- cache ---

    def cache_get(self, key: str) -> dict[str, Any] | None:
        item = self._cache.get_item(Key={"key": key}).get("Item")
        if not item:
            return None
        return {
            "payload_json": item.get("payload_json", ""),
            "fetched_at": item.get("fetched_at", ""),
            "ttl_seconds": int(item.get("ttl_seconds", 0)),
        }

    def cache_put(
        self,
        key: str,
        namespace: str,
        payload_json: str,
        fetched_at: str,
        ttl_seconds: int,
        expires_at: int | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "key": key,
            "namespace": namespace,
            "payload_json": payload_json,
            "fetched_at": fetched_at,
            "ttl_seconds": int(ttl_seconds),
        }
        if expires_at is not None:
            # The table's TTL attribute. Omitted when ttl_seconds is 0, which
            # this app defines as "never expires".
            item["expires_at"] = int(expires_at)
        self._cache.put_item(Item=item)

    def cache_delete(self, key: str) -> None:
        self._cache.delete_item(Key={"key": key})

    def cache_clear_namespace(self, namespace: str) -> int:
        removed = 0
        scan_kwargs: dict[str, Any] = {
            "FilterExpression": "#ns = :ns",
            # Both "key" and "namespace" are DynamoDB reserved words, so every
            # reference to them has to go through an expression-attribute name.
            "ExpressionAttributeNames": {"#ns": "namespace", "#k": "key"},
            "ExpressionAttributeValues": {":ns": namespace},
            "ProjectionExpression": "#k",
        }
        while True:
            page = self._cache.scan(**scan_kwargs)
            with self._cache.batch_writer() as batch:
                for item in page.get("Items", []):
                    batch.delete_item(Key={"key": item["key"]})
                    removed += 1
            cursor = page.get("LastEvaluatedKey")
            if not cursor:
                return removed
            scan_kwargs["ExclusiveStartKey"] = cursor

    def cache_rows(self) -> list[dict[str, Any]]:
        # Only purge_expired() reads this, and DynamoDB TTL makes that a no-op.
        return []


_DYNAMO: DynamoStore | None = None


def _dynamo_store() -> DynamoStore:
    """One client per warm Lambda container, not one per request."""
    global _DYNAMO
    if _DYNAMO is None:
        _DYNAMO = DynamoStore()
    return _DYNAMO


def use_dynamodb() -> bool:
    return (env("STORAGE_BACKEND", "sqlite") or "sqlite").strip().lower() == "dynamodb"


def active_store(db_path: Path | str | None = None) -> Store:
    """The backend for this process.

    ``db_path`` is honoured only by SQLite; it is what lets a caller point at a
    scratch database. DynamoDB ignores it, because there is nothing to point at.
    """
    if use_dynamodb():
        return _dynamo_store()
    return SqliteStore(db_path)


def backend_name() -> str:
    return "dynamodb" if use_dynamodb() else "sqlite"
