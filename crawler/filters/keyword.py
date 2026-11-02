"""Keyword filter against the active disease list.

Matches extracted article text against DiseaseKeyword records. Returns the
list of canonical disease names that matched. Empty list = no match.

Per-source overrides: when a Source has keyword_filter_overrides set, those
keywords are used instead of the global DB table. Each override keyword is
treated as both the keyword to match and the disease name to return.

Multilingual support: keywords with language="" match articles in
any language; language-specific keywords (e.g. language="hi") match only
articles detected in that language.
"""
from __future__ import annotations

from django.db.models import Q


def matches_active_diseases(
    text: str,
    language: str | None = None,
    keyword_overrides: list[str] | None = None,
) -> list[str]:
    """Return canonical disease names matched in `text`. Empty list = no match."""
    if not text:
        return []

    text_lower = text.lower()

    if keyword_overrides is not None:
        return sorted({kw for kw in keyword_overrides if kw.lower() in text_lower})

    from disease_keywords.models import DiseaseKeyword

    rows = (
        DiseaseKeyword.objects
        .filter(is_active=True)
        .filter(Q(language="") | Q(language=language or ""))
        .values("disease_name", "keyword")
    )

    matched: set[str] = set()
    for row in rows:
        if row["keyword"].lower() in text_lower:
            matched.add(row["disease_name"])

    return sorted(matched)
