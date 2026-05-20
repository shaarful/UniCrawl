"""Active disease keyword registry — drives the keyword filter (SRS FR-08, TBD-02)."""
from __future__ import annotations

from django.db import models


class DiseaseKeyword(models.Model):
    """One keyword associated with a canonical disease name.

    A disease may have many keywords (e.g. "dengue", "dengue fever", "breakbone fever").
    Keywords with language="" are language-agnostic and match articles in any language.
    Language-specific keywords (e.g. language="hi") match only articles in that language.
    """

    disease_name = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Canonical disease name returned when this keyword matches.",
    )
    keyword = models.CharField(
        max_length=255,
        help_text="Case-insensitive substring matched against article body text.",
    )
    language = models.CharField(
        max_length=10,
        blank=True,
        db_index=True,
        help_text="BCP-47 tag (e.g. 'en', 'hi'). Blank = language-agnostic.",
    )
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        app_label = "disease_keywords"
        unique_together = [("keyword", "language")]
        ordering = ["disease_name", "keyword"]

    def __str__(self) -> str:
        lang = f" [{self.language}]" if self.language else ""
        return f"{self.disease_name}: {self.keyword}{lang}"
