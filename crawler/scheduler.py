"""Per-source crawl scheduler.

Responsibilities:
- Fire a crawl job for each active source on its configured interval.
- Honor per-source `crawl_interval_seconds`; do not crawl before the interval elapses.
- Respect exponential back-off after failures: retries at 1, 4, 16 minutes
  (configurable via CRAWLER_RETRY_BACKOFF_MINUTES); after the 3rd failure mark
  the source temporarily unavailable.

Implementation uses Celery beat with a dynamic schedule sourced from the
Source table.
"""
from __future__ import annotations

from datetime import timedelta

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from sources.models import Source

log = structlog.get_logger(__name__)


def schedule_due_sources() -> int:
    """Enqueue crawl tasks for every active source whose next-due time has arrived.

    Returns the number of tasks enqueued. Called by the Celery beat tick task.
    """
    from crawler.tasks import crawl_source

    now = timezone.now()
    due = (
        Source.objects.filter(
            is_active=True,
            is_temporarily_unavailable=False,
        )
        .filter(Q(next_crawl_at__isnull=True) | Q(next_crawl_at__lte=now))
        .only("id", "name")
    )

    count = 0
    for source in due:
        crawl_source.delay(source.id)
        count += 1
        log.info("scheduler.enqueued", source=source.name, source_id=source.id)

    return count


def record_failure(source_id: int) -> None:
    """Increment failure count and compute the next retry time per the back-off schedule.

    Intervals from settings.CRAWLER_RETRY_BACKOFF_MINUTES (default [1, 4, 16]).
    After all retries are exhausted the source is marked temporarily unavailable
    until an admin resets it via the Django admin.
    """
    backoff = settings.CRAWLER_RETRY_BACKOFF_MINUTES
    max_retries = len(backoff)

    with transaction.atomic():
        source = Source.objects.select_for_update().get(pk=source_id)
        source.consecutive_failures += 1
        failures = source.consecutive_failures

        if failures <= max_retries:
            delay = timedelta(minutes=backoff[failures - 1])
            source.next_crawl_at = timezone.now() + delay
            log.warning(
                "scheduler.retry_scheduled",
                source=source.name,
                source_id=source_id,
                attempt=failures,
                retry_in_minutes=backoff[failures - 1],
            )
        else:
            source.is_temporarily_unavailable = True
            source.unavailable_until = None
            log.error(
                "scheduler.source_unavailable",
                source=source.name,
                source_id=source_id,
                consecutive_failures=failures,
            )

        source.save(update_fields=[
            "consecutive_failures",
            "next_crawl_at",
            "is_temporarily_unavailable",
            "unavailable_until",
        ])
