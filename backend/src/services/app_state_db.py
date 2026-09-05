"""The writable application database: sessions, chats and the response cache.

This is the only database the app writes to. ``coursefinder.db`` is opened
read-only everywhere and is never touched from here.

Schema changes use ``CREATE TABLE IF NOT EXISTS`` against the shape the
previous build already wrote, so an existing ``data/app_state.db`` keeps its
chat history instead of being replaced.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from graph.config import app_state_db_path

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id  TEXT PRIMARY KEY,
        state_json  TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chats (
        session_id  TEXT PRIMARY KEY,
        title       TEXT NOT NULL,
        folder      TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cache (
        key          TEXT PRIMARY KEY,
        namespace    TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        fetched_at   TEXT NOT NULL,
        ttl_seconds  INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_cache_namespace ON cache(namespace)",
)


def now() -> str:
    """One timestamp format for every row this app writes."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Open a writable connection, creating the schema on first use.

    Errors are *not* caught here. A persistence failure that is swallowed looks
    exactly like a working app until the student reloads and their chat is
    gone; it must reach the caller.
    """
    path = Path(db_path) if db_path is not None else app_state_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        for statement in SCHEMA:
            conn.execute(statement)
        conn.commit()
        yield conn
        conn.commit()
    finally:
        conn.close()
