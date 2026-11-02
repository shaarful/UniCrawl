"""Structured data extracted from crawled articles by the LLM worker.

An article has at most one ArticleExtraction, replaced whenever its text is
re-extracted (e.g. after a refetch changes the body). The RawArticle itself is
never modified: this app depends on raw_content, never the other way round.
"""
from __future__ import annotations

from django.db import models

from raw_content.models import RawArticle


class ArticleExtraction(models.Model):
    """Outcome of extracting one article, plus what is needed to audit it."""

    class Status(models.TextChoices):
        RETRYING = "retrying", "Retrying"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed (dead-lettered)"

    article = models.OneToOneField(
        RawArticle,
        on_delete=models.CASCADE,
        related_name="extraction",
    )
    status = models.CharField(max_length=10, choices=Status.choices, db_index=True)
    is_disease_event = models.BooleanField(
        null=True,
        blank=True,
        db_index=True,
        help_text="False when the article reports no disease occurrence; "
                  "empty until an extraction has completed.",
    )

    # --- Redelivery handling ---
    input_hash = models.CharField(
        max_length=64,
        help_text="Hash of the model, prompt version and article text extracted.",
    )
    stream_message_id = models.CharField(
        max_length=32,
        blank=True,
        help_text="Redis message that produced the stored events.",
    )
    attempts = models.PositiveSmallIntegerField(
        default=0,
        help_text="Article-specific failures for this input; outages are not counted.",
    )
    error = models.TextField(blank=True)

    # --- Audit ---
    llm_model = models.CharField(max_length=100, blank=True)
    prompt_version = models.CharField(max_length=20, blank=True)
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    response_tokens = models.PositiveIntegerField(null=True, blank=True)
    input_truncated = models.BooleanField(default=False)
    raw_response = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"[{self.status}] article {self.article_id}"


class ExtractedEvent(models.Model):
    """One disease occurrence reported by an article (fields per TBD-03)."""

    extraction = models.ForeignKey(
        ArticleExtraction,
        on_delete=models.CASCADE,
        related_name="events",
    )
    disease = models.CharField(max_length=255, db_index=True)
    country = models.CharField(max_length=255, blank=True, db_index=True)
    location = models.CharField(max_length=255, blank=True)
    number_of_cases = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        place = self.location or self.country or "unknown location"
        return f"{self.disease} in {place}"
