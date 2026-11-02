"""Raw ingested content store.

Every successful fetch persists a RawArticle BEFORE any downstream processing.
Records are immutable after creation — update only processing_status and the
dedup/match fields as the pipeline progresses. The one exception is an explicit
refetch from the original link (crawler.refetch), which may replace the body
and fill in missing source-stated fields; it is recorded in metadata.
"""
from __future__ import annotations

from django.db import models

from sources.models import Source


class RawArticle(models.Model):
    """Immutable raw ingestion record with provenance and pipeline state.

    Dedup pipeline writes url_fingerprint and content_hash on
    creation, then queries existing rows before deciding to suppress.

    processing_status tracks where the article is in the pipeline:
        pending    → fetched, waiting for dedup + keyword filter
        matched    → keyword matched, waiting to be published to LLM queue
        suppressed → duplicate detected; see suppression_reason + duplicate_of
        published  → handed off to LLM queue (≤60s SLA)
        failed     → unrecoverable pipeline error; see the structured error log
    """

    class ProcessingStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        MATCHED = "matched", "Keyword matched"
        SUPPRESSED = "suppressed", "Suppressed (duplicate)"
        PUBLISHED = "published", "Published to LLM queue"
        FAILED = "failed", "Failed"

    class SuppressionReason(models.TextChoices):
        URL = "url", "URL fingerprint match"
        CONTENT = "content", "Content hash match"
        SEMANTIC = "semantic", "Semantic similarity match"

    # --- Provenance ---
    source = models.ForeignKey(
        Source,
        on_delete=models.PROTECT,
        related_name="raw_articles",
    )
    url = models.URLField(
        max_length=2048,
        help_text="Original URL as requested from the source.",
    )
    final_url = models.URLField(
        max_length=2048,
        blank=True,
        help_text="URL after redirects; may differ from url.",
    )
    http_status = models.PositiveSmallIntegerField()
    fetched_at = models.DateTimeField()

    # --- Content ---
    title = models.CharField(max_length=1024, blank=True)
    body = models.TextField()
    language = models.CharField(
        max_length=10,
        blank=True,
        help_text="BCP-47 language tag detected by langdetect.",
    )
    metadata = models.JSONField(
        null=True,
        blank=True,
        help_text="Source-specific provenance (e.g. EIOS id, countries, categories) "
                  "preserved for the LLM extraction service (TBD-03).",
    )

    # --- Source-stated article fields (crawler-extracted; the LLM service
    # extracts the fields that need reading the text) ---
    published_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Publication / reporting date stated by the source "
                  "(page metadata, feed entry or EIOS pubDate).",
    )
    author = models.CharField(
        max_length=512,
        blank=True,
        help_text="Reporter / author byline from page or feed metadata.",
    )
    source_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Publisher of the article (EIOS source name, feed <source>, "
                  "else the configured source name).",
    )
    source_type = models.CharField(
        max_length=64,
        blank=True,
        help_text="Channel type: the source's content type (html, rss) "
                  "or, for EIOS, the publisher type (webnews, wire, socialmedia-…).",
    )

    # --- Dedup fingerprints ---
    # Indexed for fast lookup; uniqueness is enforced by the dedup pipeline,
    # not a DB constraint, to avoid race conditions on concurrent crawl workers.
    url_fingerprint = models.CharField(max_length=64, db_index=True)
    content_hash = models.CharField(max_length=64, db_index=True)

    # --- Duplicate tracking ---
    is_duplicate = models.BooleanField(default=False)
    duplicate_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="duplicates",
        help_text="Points to the original article when this one is suppressed.",
    )
    suppression_reason = models.CharField(
        max_length=10,
        choices=SuppressionReason.choices,
        blank=True,
    )

    # --- Semantic dedup embedding (Layer 3) ---
    # Stored as JSON list[float] for DB portability; migrate to pgvector VectorField
    # once the production PostgreSQL has the pgvector extension installed.
    embedding = models.JSONField(
        null=True,
        blank=True,
        help_text="Sentence-transformer embedding vector (384 dims). Used for semantic dedup.",
    )

    # --- Keyword match ---
    is_keyword_matched = models.BooleanField(
        default=False,
        db_index=True,
    )
    matched_diseases = models.JSONField(
        null=True,
        blank=True,
        help_text="List of disease names from the active keyword list that matched.",
    )

    # --- Pipeline state ---
    processing_status = models.CharField(
        max_length=10,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.PENDING,
        db_index=True,
    )

    # --- Audit ---
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        app_label = "raw_content"
        ordering = ["-fetched_at"]
        indexes = [
            models.Index(fields=["source", "processing_status"]),
            models.Index(fields=["is_keyword_matched", "processing_status"]),
        ]

    def __str__(self) -> str:
        return f"[{self.processing_status}] {self.url[:80]}"

