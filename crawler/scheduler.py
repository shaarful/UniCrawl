"""Per-source crawl scheduler (SRS FR-10, FR-14).

Responsibilities:
- Fire a crawl job for each active source on its configured interval.
- Honor per-source `crawl_interval_seconds`; do not crawl before the interval elapses.
- Respect exponential back-off after failures: retries at 1, 4, 16 minutes
  (configurable via CRAWLER_RETRY_BACKOFF_MINUTES); after the 3rd failure mark
  the source temporarily unavailable.

Implementation will use Celery beat with a dynamic schedule sourced from the
Source table.
"""
from __future__ import annotations


def schedule_due_sources() -> None:
    """Enqueue crawl jobs for every source whose next-due time has arrived."""
    raise NotImplementedError


def record_failure(source_id: int) -> None:
    """Increment failure count and compute the next retry time per back-off schedule."""
    raise NotImplementedError
