"""Layer 3: semantic-similarity dedup.

Threshold defaults to 0.85 (SRS FR-23) and is overridable via
settings.CRAWLER_SEMANTIC_DEDUP_THRESHOLD.

Implementation TBD — likely sentence-transformer embeddings + pgvector / ANN
index. Keep it pluggable behind this module's interface.
"""
from __future__ import annotations


def similarity_score(text: str, other: str) -> float:
    """Return cosine similarity in [0.0, 1.0]."""
    raise NotImplementedError


def is_duplicate(text: str, threshold: float = 0.85) -> str | None:
    """Return matching RawArticle id (as str) if similarity ≥ threshold against any stored article."""
    raise NotImplementedError
