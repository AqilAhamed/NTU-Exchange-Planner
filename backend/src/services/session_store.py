"""Chat sessions and their persisted state.

A session holds two things: the message list the UI renders, and the profile
carried between turns so "how much would that cost?" resolves against the
university discussed a moment ago instead of re-asking for a programme.

The whole payload of each assistant turn is stored alongside its message. The
previous build persisted only the profile and the prose, so reopening a chat
produced an answer with its shortlist and citations stripped out.
"""

from __future__ import annotations

import json
import asyncio
import threading
import uuid
from pathlib import Path
from typing import Any

from services.app_state_db import now
from services.store import active_store

DEFAULT_TITLE = "New chat"
MAX_TITLE = 60

# Process-local lifecycle markers prevent a turn that was already in flight
# when DELETE arrived from recreating the chat. These are not conversation
# memory and are bounded to avoid an unbounded memory leak.
MAX_DELETED_TOMBSTONES = 4096
_lifecycle_lock = threading.RLock()
_deleted_sessions: dict[str, None] = {}
_active_turns: dict[str, asyncio.Task | None] = {}


def begin_turn(session_id: str, task: asyncio.Task | None = None) -> bool:
    """Register an in-flight turn unless the chat has been deleted."""
    with _lifecycle_lock:
        if session_id in _deleted_sessions:
            return False
        if session_id in _active_turns:
            return False
        _active_turns[session_id] = task
        return True


def end_turn(session_id: str, task: asyncio.Task | None = None) -> None:
    """Remove an in-flight marker without touching persisted chat state."""
    with _lifecycle_lock:
        current = _active_turns.get(session_id)
        if current is task or task is None:
            _active_turns.pop(session_id, None)


def is_deleted(session_id: str) -> bool:
    """Whether this process has tombstoned a chat after deletion."""
    with _lifecycle_lock:
        return session_id in _deleted_sessions


def is_active(session_id: str) -> bool:
    """Whether a turn is currently running for this chat."""
    with _lifecycle_lock:
        return session_id in _active_turns


def cancel_turn(session_id: str) -> asyncio.Task | None:
    """Cancel the current turn without deleting the chat or its history."""
    with _lifecycle_lock:
        task = _active_turns.get(session_id)
    _cancel_task(task)
    return task


def _remember_deleted(session_id: str) -> asyncio.Task | None:
    with _lifecycle_lock:
        _deleted_sessions.pop(session_id, None)
        _deleted_sessions[session_id] = None
        while len(_deleted_sessions) > MAX_DELETED_TOMBSTONES:
            oldest = next(iter(_deleted_sessions))
            _deleted_sessions.pop(oldest, None)
        return _active_turns.get(session_id)


def _cancel_task(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return
    try:
        task.get_loop().call_soon_threadsafe(task.cancel)
    except RuntimeError:
        # The loop may already be closing. The tombstone still prevents the
        # late save from recreating the deleted chat.
        return


def new_session_id() -> str:
    return uuid.uuid4().hex


def _empty_state() -> dict[str, Any]:
    return {"messages": [], "profile": None, "context": {}}


def _load_state(raw: str) -> dict[str, Any]:
    """Parse stored state, tolerating a row written by an older build."""
    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        return _empty_state()
    if not isinstance(state, dict):
        return _empty_state()
    state.setdefault("messages", [])
    state.setdefault("profile", None)
    state.setdefault("context", {})
    if not isinstance(state["messages"], list):
        state["messages"] = []
    return state


def create_session(
    title: str = DEFAULT_TITLE, db_path: Path | str | None = None
) -> str:
    """Create an empty session and its chat-list entry."""
    session_id = new_session_id()
    stamp = now()
    store = active_store(db_path)
    store.session_put(session_id, json.dumps(_empty_state()), stamp)
    store.chat_create(session_id, title[:MAX_TITLE] or DEFAULT_TITLE, stamp)
    return session_id


def get_state(session_id: str, db_path: Path | str | None = None) -> dict[str, Any] | None:
    """The stored state for a session, or ``None`` when it does not exist."""
    raw = active_store(db_path).session_get(session_id)
    return _load_state(raw) if raw is not None else None


def save_state(
    session_id: str,
    state: dict[str, Any],
    *,
    title: str | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Write a session's state, creating the row if this is its first turn."""
    stamp = now()
    payload = json.dumps(state, default=str)
    # Hold the same lock as delete_chat across the check and transaction. This
    # closes the check-then-delete-then-late-insert race.
    with _lifecycle_lock:
        if session_id in _deleted_sessions:
            return
        store = active_store(db_path)
        store.session_put(session_id, payload, stamp)
        chat = store.chat_get(session_id)
        clean_title = (title or "").strip()[:MAX_TITLE]
        if chat is None:
            store.chat_create(session_id, clean_title or DEFAULT_TITLE, stamp)
        elif clean_title and chat.get("title") in ("", DEFAULT_TITLE):
            # The first real message names the chat; later turns must not
            # rename a conversation the student is already navigating by title.
            store.chat_set_title(session_id, clean_title, stamp)
        else:
            store.chat_touch(session_id, stamp)


def list_chats(limit: int = 100, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """The sidebar's chat list, most recently used first."""
    return [
        dict(row)
        for row in active_store(db_path).chat_list(max(1, min(int(limit), 500)))
    ]


def delete_chat(session_id: str, db_path: Path | str | None = None) -> bool:
    """Remove a chat, cancel its active turn, and delete its persisted state."""
    with _lifecycle_lock:
        task = _remember_deleted(session_id)
        store = active_store(db_path)
        deleted = store.chat_delete(session_id)
        store.session_delete(session_id)
    _cancel_task(task)
    return deleted


def rename_chat(session_id: str, title: str, db_path: Path | str | None = None) -> bool:
    clean = (title or "").strip()[:MAX_TITLE]
    if not clean:
        return False
    return active_store(db_path).chat_set_title(session_id, clean, now())


def title_from_message(message: str) -> str:
    """A chat title taken from the student's first message."""
    clean = " ".join((message or "").split())
    if not clean:
        return DEFAULT_TITLE
    return clean[: MAX_TITLE - 1] + "…" if len(clean) > MAX_TITLE else clean
