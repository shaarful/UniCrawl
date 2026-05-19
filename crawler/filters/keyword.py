"""Keyword filter against the active disease list (SRS FR-08).

The disease list is TBD-02 in the SRS. Once finalized, this module loads it
(probably from a DB table or YAML) and matches extracted text. Multilingual
match support is required (FR-12).
"""
from __future__ import annotations


def matches_active_diseases(text: str, language: str | None = None) -> list[str]:
    """Return the list of disease names matched in `text`. Empty list = no match."""
    raise NotImplementedError
