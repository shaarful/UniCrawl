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
    summary = models.TextField(
        blank=True,
        help_text="English summary of the article, at most 100 words (FR-10); "
                  "empty until an extraction has completed.",
    )
    reporter_author = models.CharField(
        max_length=512,
        blank=True,
        help_text="Reporter named in the text; the crawler's byline takes precedence.",
    )
    author_affiliation = models.CharField(max_length=512, blank=True)
    publication_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date stated in the text; the crawler's date takes precedence.",
    )
    translated_title = models.TextField(
        blank=True,
        help_text="English translation; empty when the article is already in English.",
    )
    translated_body = models.TextField(
        blank=True,
        help_text="English translation; empty when the article is already in English.",
    )

    # --- Redelivery handling ---
    input_hash = models.CharField(
        max_length=64,
        help_text="Hash of the model, prompt version and article text extracted.",
    )
    stream_message_id = models.CharField(
        max_length=32,
        blank=True,
        help_text="Redis message that produced the stored events, or the same "
                  "time-based form when processed from the admin button.",
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
    """One disease occurrence reported by an article (FR-09).

    Every field except disease is optional: articles rarely state them all, and
    what an article does not state stays empty rather than being guessed.
    """

    class LocationType(models.TextChoices):
        COMMUNITY = "community", "Community"
        FARM = "farm", "Farm"
        MARKET = "market", "Market"
        SLAUGHTERHOUSE = "slaughterhouse", "Slaughterhouse"
        WILDLIFE_HABITAT = "wildlife_habitat", "Wildlife habitat"

    extraction = models.ForeignKey(
        ArticleExtraction,
        on_delete=models.CASCADE,
        related_name="events",
    )
    disease = models.CharField(max_length=255, db_index=True)
    host = models.CharField(max_length=255, blank=True, db_index=True)
    affected_population_size = models.PositiveBigIntegerField(null=True, blank=True)

    country = models.CharField(max_length=255, blank=True, db_index=True)
    division = models.CharField(max_length=255, blank=True)
    district = models.CharField(max_length=255, blank=True)
    village = models.CharField(max_length=255, blank=True)
    latitude = models.FloatField(
        null=True, blank=True, help_text="Only when printed in the article; no geocoding yet.",
    )
    longitude = models.FloatField(
        null=True, blank=True, help_text="Only when printed in the article; no geocoding yet.",
    )
    location_type = models.CharField(max_length=20, choices=LocationType.choices, blank=True)

    event_date = models.DateField(null=True, blank=True, db_index=True)
    confirmed_case_count = models.PositiveBigIntegerField(null=True, blank=True)
    suspected_case_count = models.PositiveBigIntegerField(null=True, blank=True)
    fatality_count = models.PositiveBigIntegerField(null=True, blank=True)

    response_action = models.TextField(blank=True)
    responding_authority = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        place = self.village or self.district or self.division or self.country
        return f"{self.disease} in {place or 'unknown location'}"

    def as_record(self) -> dict:
        """The FR-09 record: LLM fields plus the crawler's, with null where absent.

        Where both have a value, the crawler's wins, because it comes from the
        page's metadata rather than from reading the text.
        """
        extraction = self.extraction
        article = extraction.article
        published = article.published_at.date() if article.published_at else (
            extraction.publication_date
        )
        return {
            "disease_name": self.disease,
            "host": self.host or None,
            "affected_population_size": self.affected_population_size,
            "country": self.country or None,
            "division": self.division or None,
            "district": self.district or None,
            "village": self.village or None,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "location_type": self.location_type or None,
            "event_date": self.event_date.isoformat() if self.event_date else None,
            "publication_date": published.isoformat() if published else None,
            "confirmed_case_count": self.confirmed_case_count,
            "suspected_case_count": self.suspected_case_count,
            "fatality_count": self.fatality_count,
            "source_url": article.final_url or article.url,
            "source_name": article.source_name or None,
            "source_type": article.source_type or None,
            "source_language": article.language or None,
            "reporter_author": article.author or extraction.reporter_author or None,
            "author_affiliation": extraction.author_affiliation or None,
            # Needs the event grouping in FR-17; one article cannot tell.
            "corroborating_sources_count": None,
            "response_action": self.response_action or None,
            "responding_authority": self.responding_authority or None,
        }
