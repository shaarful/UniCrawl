"""Layer 3 dedup: semantic similarity via sentence-transformer embeddings.

Threshold defaults to 0.85, configurable via settings.CRAWLER_SEMANTIC_DEDUP_THRESHOLD.

Embeddings are 384-dimensional vectors produced by a multilingual MiniLM model and
stored as JSON lists in RawArticle.embedding. Duplicate detection queries the most
recent CRAWLER_SEMANTIC_MAX_CANDIDATES non-duplicate articles in Python.

Production upgrade path: once the PostgreSQL instance has the pgvector extension,
replace the Python cosine loop in find_duplicate() with a pgvector CosineDistance
query and add an ivfflat/hnsw index on RawArticle.embedding for ANN search.

Requires: pipenv install sentence-transformers  (pulls in PyTorch, ~2 GB on Windows).
Enable via: CRAWLER_SEMANTIC_DEDUP_ENABLED=True in .env.
"""
from __future__ import annotations

import structlog
from django.conf import settings

log = structlog.get_logger(__name__)

_model = None


def _get_model():
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers is required for semantic dedup. "
                "Run: pipenv install sentence-transformers"
            ) from exc
        model_name = settings.CRAWLER_SEMANTIC_MODEL
        log.info("semantic.model_loading", model=model_name)
        _model = SentenceTransformer(model_name)
        log.info("semantic.model_ready", model=model_name)
    return _model


def compute_embedding(text: str) -> list[float] | None:
    """Return the sentence embedding for `text`, or None if text is empty."""
    if not text or not text.strip():
        return None
    vector = _get_model().encode(text, convert_to_numpy=True)
    return vector.tolist()


def similarity_score(text: str, other: str) -> float:
    """Return cosine similarity in [-1.0, 1.0] between two texts."""
    import numpy as np

    a = np.array(compute_embedding(text) or [], dtype=float)
    b = np.array(compute_embedding(other) or [], dtype=float)
    if a.size == 0 or b.size == 0:
        return 0.0
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def find_duplicate(
    embedding: list[float],
    threshold: float | None = None,
) -> int | None:
    """Return the id of a non-duplicate RawArticle with cosine similarity ≥ threshold.

    Scans the most recent CRAWLER_SEMANTIC_MAX_CANDIDATES stored embeddings.
    Returns None if no match is found or the candidate pool is empty.
    """
    import numpy as np
    from raw_content.models import RawArticle

    t = threshold if threshold is not None else settings.CRAWLER_SEMANTIC_DEDUP_THRESHOLD
    max_candidates = settings.CRAWLER_SEMANTIC_MAX_CANDIDATES

    candidates = (
        RawArticle.objects
        .filter(is_duplicate=False, embedding__isnull=False)
        .order_by("-created_at")
        [:max_candidates]
        .values("id", "embedding")
    )

    query_vec = np.array(embedding, dtype=float)
    query_norm = np.linalg.norm(query_vec)
    if query_norm == 0:
        return None

    for row in candidates:
        stored_vec = np.array(row["embedding"], dtype=float)
        stored_norm = np.linalg.norm(stored_vec)
        if stored_norm == 0:
            continue
        cosine = float(np.dot(query_vec, stored_vec) / (query_norm * stored_norm))
        if cosine >= t:
            log.info(
                "semantic.duplicate_found",
                original_id=row["id"],
                similarity=round(cosine, 4),
                threshold=t,
            )
            return row["id"]

    return None
