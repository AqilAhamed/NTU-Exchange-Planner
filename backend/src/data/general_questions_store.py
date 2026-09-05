"""ChromaDB access for the NTU-student general-question corpus.

The application reads this module at query time. It deliberately has no PDF
reader and no source-directory fallback: the corpus is an indexed artifact
created by ``tools/ingest_general_questions.py``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from graph.config import general_questions_chroma_path, general_questions_embedding_path

COLLECTION_NAME = "ntu_exchange_general_questions"


def embedding_function():
    """Use Chroma's local ONNX MiniLM embedder, stored inside this project."""
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

    model_path = general_questions_embedding_path()
    model_path.mkdir(parents=True, exist_ok=True)
    # Chroma's default is under the user's home cache, which is unsuitable for
    # deployment. Point it at a project-local, reproducible location instead.
    ONNXMiniLM_L6_V2.DOWNLOAD_PATH = str(model_path)
    return DefaultEmbeddingFunction()


@lru_cache(maxsize=1)
def client():
    import chromadb

    path = general_questions_chroma_path()
    path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(path))


@lru_cache(maxsize=1)
def collection():
    return client().get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine", "embedding_model": "all-MiniLM-L6-v2"},
        embedding_function=embedding_function(),
    )


def reset_collection():
    """Drop and recreate the exact named collection for a fresh ingestion."""
    chroma = client()
    try:
        chroma.delete_collection(COLLECTION_NAME)
    except Exception as exc:
        if exc.__class__.__name__ != "NotFoundError":
            raise
    collection.cache_clear()
    return collection()


def refresh_collection():
    """Refresh cached Chroma handles after an external collection rebuild."""
    collection.cache_clear()
    client.cache_clear()
    return collection()


def source_record(source_id: str) -> dict[str, Any] | None:
    """Return one indexed chunk for the clickable source endpoint."""
    if not source_id:
        return None
    result = collection().get(ids=[source_id], include=["documents", "metadatas"])
    documents = result.get("documents") or []
    metadatas = result.get("metadatas") or []
    if not documents:
        return None
    metadata = metadatas[0] if metadatas else {}
    return {
        "document": str(metadata.get("document") or "NTU intranet reference"),
        "page": int(metadata.get("page") or 0),
        "text": str(documents[0] or ""),
    }


__all__ = [
    "COLLECTION_NAME",
    "client",
    "collection",
    "embedding_function",
    "general_questions_chroma_path",
    "reset_collection",
    "refresh_collection",
    "source_record",
]
