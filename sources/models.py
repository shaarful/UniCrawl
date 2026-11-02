"""Source registry models."""
from __future__ import annotations

from django.conf import settings
from django.db import models


class Source(models.Model):
    """An administrator-configured ingestable URL with per-source crawl settings.

    is_active defaults to False — a test-crawl must succeed before
    the source can be activated. The scheduler only processes active sources.

    Back-off state: after each failure consecutive_failures is incremented.
    After 3 failures is_temporarily_unavailable is set and unavailable_until is
    set to now + back-off[consecutive_failures] minutes. Resets on next success.
    """

    class ContentType(models.TextChoices):
        HTML = "html", "News / HTML bulletin"
        RSS = "rss", "RSS / Atom feed"
        EIOS = "eios", "WHO-EIOS authenticated feed"

    # --- Identity ---
    name = models.CharField(max_length=255, unique=True)
    url = models.URLField(max_length=2048, unique=True)
    content_type = models.CharField(
        max_length=10,
        choices=ContentType.choices,
        default=ContentType.HTML,
    )
    language_hint = models.CharField(
        max_length=10,
        blank=True,
        help_text="BCP-47 language tag (e.g. 'en', 'hi', 'bn'). "
                  "Leave blank to auto-detect.",
    )

    # --- Crawl configuration ---
    crawl_interval_seconds = models.PositiveIntegerField(
        default=3600,
        help_text="How often to crawl this source. "
                  "Defaults to CRAWLER_DEFAULT_INTERVAL_SECONDS.",
    )
    crawl_depth = models.PositiveSmallIntegerField(
        default=1,
        help_text="Link-follow depth. 1 = landing page only.",
    )
    keyword_filter_overrides = models.JSONField(
        null=True,
        blank=True,
        help_text="List of disease keywords specific to this source. "
                  "Null = use the global active disease list (TBD-02).",
    )

    # --- Lifecycle ---
    is_active = models.BooleanField(
        default=False,
        help_text="Only active sources are scheduled. "
                  "Set to True after a successful test-crawl.",
    )
    notes = models.TextField(blank=True)

    # --- Crawl state ---
    last_crawled_at = models.DateTimeField(null=True, blank=True)
    next_crawl_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Computed by scheduler after each run.",
    )

    # --- Back-off state (retry at 1, 4, 16 min; then mark unavailable) ---
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    is_temporarily_unavailable = models.BooleanField(default=False)
    unavailable_until = models.DateTimeField(null=True, blank=True)

    # --- Audit ---
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="added_sources",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "sources"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_content_type_display()})"

