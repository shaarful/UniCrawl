"""Crawl run tracking — one row per pipeline execution, for live progress in the UI."""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from sources.models import Source


class CrawlRun(models.Model):
    """A single execution of the pipeline for one source.

    The pipeline reports progress into this row (throttled to ~1 write/s), and
    the dashboard polls it. Articles stored by the run share its `fetched_at`.
    """

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Stopped"
        INTERRUPTED = "interrupted", "Interrupted"

    class Trigger(models.TextChoices):
        MANUAL = "manual", "Manual"
        SCHEDULED = "scheduled", "Scheduled"

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="crawl_runs")
    trigger = models.CharField(max_length=10, choices=Trigger.choices, default=Trigger.MANUAL)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="crawl_runs",
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.RUNNING, db_index=True,
    )

    # --- Progress ---
    stage = models.CharField(max_length=255, blank=True)
    percent = models.FloatField(
        null=True, blank=True, help_text="0–100; null while the total is unknown.",
    )
    processed = models.PositiveIntegerField(default=0)
    new = models.PositiveIntegerField(default=0)
    matched = models.PositiveIntegerField(default=0)
    published = models.PositiveIntegerField(default=0)
    duplicates = models.PositiveIntegerField(default=0)
    errors = models.PositiveIntegerField(default=0)
    events = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    cancel_requested = models.BooleanField(default=False)

    # --- Timing ---
    fetched_at = models.DateTimeField(
        null=True, blank=True, help_text="fetched_at of the RawArticles this run stored.",
    )
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "crawler"
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["source", "status"])]

    def __str__(self) -> str:
        return f"Run #{self.pk} {self.source.name} [{self.status}]"

    @property
    def is_running(self) -> bool:
        return self.status == self.Status.RUNNING
