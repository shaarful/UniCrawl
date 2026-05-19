"""Layer 2 dedup: exact-content hash via sha256 of normalized text.

Normalization collapses runs of whitespace, strips, and lowercases — so two
articles that differ only in whitespace or case hash identically.
"""
from __future__ import annotations

import hashlib
import re

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Return a deterministic normalized form of article text."""
    if not text:
        return ""
    return _WHITESPACE.sub(" ", text).strip().lower()


def content_hash(text: str) -> str:
    """Return a sha256 hex digest of the normalized text (64 chars)."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def find_duplicate(text: str) -> int | None:
    """Return the id of the canonical RawArticle with the same content hash, or None."""
    from raw_content.models import RawArticle

    return (
        RawArticle.objects
        .filter(content_hash=content_hash(text), is_duplicate=False)
        .values_list("id", flat=True)
        .first()
    )
