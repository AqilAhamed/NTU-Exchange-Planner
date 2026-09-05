"""Path and environment resolution.

Every other module asks this one where things live, so there is exactly one
place that knows the repository layout. Relative paths in the environment are
resolved against the repository root, not the current working directory, so the
app behaves the same whether it is started from ``backend/`` or from the root.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# backend/src/graph/config.py -> backend/src/graph -> backend/src -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"


@lru_cache(maxsize=1)
def load_env() -> None:
    """Load ``backend/.env`` once, without overriding real environment values.

    python-dotenv is optional: the deterministic half of the app must run even
    if it is missing, so an ImportError is not fatal.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - exercised only in stripped installs
        return
    load_dotenv(BACKEND_ROOT / ".env", override=False)


def env(name: str, default: str = "") -> str:
    """Read an environment variable, ensuring ``.env`` has been loaded first."""
    load_env()
    value = os.environ.get(name)
    return default if value is None else value.strip()


def _resolve(var_name: str, default_relative: str) -> Path:
    raw = env(var_name, default_relative) or default_relative
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate
    return (REPO_ROOT / candidate).resolve()


def coursefinder_db_path() -> Path:
    """The read-only Coursefinder database (187 MB, never written to)."""
    return _resolve("COURSEFINDER_DB", "coursefinder.db")


def general_questions_dataset_path() -> Path:
    """The NTU-student intranet PDFs used by the general-questions RAG lane."""
    raw = env("GENERAL_QUESTIONS_DATASET_DIR", "")
    if raw:
        candidate = Path(raw).expanduser()
        return candidate if candidate.is_absolute() else (REPO_ROOT / candidate).resolve()
    # The supplied dataset lives beside this project in the SAM Agentic AI
    # workspace. Keeping this as a default makes local setup zero-config while
    # the environment variable supports deployment or a copied dataset.
    return (REPO_ROOT.parent / "Dataset for General Questions Agent").resolve()


def general_questions_chroma_path() -> Path:
    """Persistent ChromaDB directory for the NTU-student RAG corpus."""
    return _resolve("GENERAL_QUESTIONS_CHROMA_DIR", "data/general_questions_chroma")


def general_questions_embedding_path() -> Path:
    """Project-local cache for the default embedding model used by Chroma."""
    return _resolve("GENERAL_QUESTIONS_EMBEDDING_DIR", "data/general_questions_embedding")


def app_state_db_path() -> Path:
    """Sessions, chats and the durable response cache."""
    return _resolve("APP_STATE_DB", "data/app_state.db")


# The UI proxies through next.config.ts, so the browser only ever calls its own
# origin. A wildcard default therefore buys nothing and, on a deployed
# instance, lets any page on the internet drive these endpoints — including the
# chat store, which has no per-user ownership. Loopback by default; set
# CORS_ALLOW_ORIGINS explicitly for anything else.
DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


def cors_allow_origins() -> list[str]:
    raw = env("CORS_ALLOW_ORIGINS", "")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return origins or list(DEFAULT_CORS_ORIGINS)
