"""Celery tasks for the crawler — autodiscovered via crawler/tasks.py."""
from __future__ import annotations

import structlog
from celery import shared_task

log = structlog.get_logger(__name__)


@shared_task(name="crawler.tasks.crawl_source")
def crawl_source(source_id: int) -> None:
    """Run the full pipeline for one source; record failure and re-raise on error."""
    log.info("task.start", source_id=source_id)
    try:
        from crawler.pipeline import run_for_source
        run_for_source(source_id)
        log.info("task.success", source_id=source_id)
    except Exception as exc:
        log.exception("task.failure", source_id=source_id, error=str(exc))
        from crawler.scheduler import record_failure
        record_failure(source_id)
        raise


@shared_task(name="crawler.tasks.tick")
def tick() -> int:
    """Celery beat entry point: enqueue crawl tasks for all due sources."""
    from crawler.scheduler import schedule_due_sources
    return schedule_due_sources()
