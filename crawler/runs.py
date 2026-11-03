"""Start, execute and stop tracked crawl runs (used by the crawl dashboard and Celery).

Manual runs from the dashboard execute in a background thread of the web
process, so `runserver` alone is enough to crawl and watch progress. Scheduled
runs (crawler.tasks.crawl_source) execute in the Celery worker and are tracked
the same way, so they show up live in the dashboard too.
"""
from __future__ import annotations

import threading
from datetime import timedelta

import structlog
from django.db import close_old_connections, connections, transaction
from django.utils import timezone

from crawler.models import CrawlRun
from crawler.progress import CrawlCancelled, RunProgress

log = structlog.get_logger(__name__)

# Live runs touch heartbeat_at every RunProgress.HEARTBEAT_SECONDS (10 s), so a
# running run silent for this long lost its process (e.g. runserver reloaded
# after a code change, or the server was stopped mid-crawl).
STALE_AFTER = timedelta(seconds=60)


class AlreadyRunning(Exception):
    def __init__(self, run: CrawlRun) -> None:
        super().__init__(f"{run.source.name} is already being crawled (run #{run.pk}).")
        self.run = run


def mark_stale_runs() -> int:
    return CrawlRun.objects.filter(
        status=CrawlRun.Status.RUNNING,
        heartbeat_at__lt=timezone.now() - STALE_AFTER,
    ).update(
        status=CrawlRun.Status.INTERRUPTED,
        stage="Interrupted — the crawl process stopped (server restarted or reloaded?)",
        finished_at=timezone.now(),
    )


def create_run(source_id: int, *, trigger: str = CrawlRun.Trigger.MANUAL, user=None) -> CrawlRun:
    """Create a running CrawlRun, refusing if the source is already being crawled."""
    mark_stale_runs()
    with transaction.atomic():
        active = (
            CrawlRun.objects.select_related("source")
            .filter(source_id=source_id, status=CrawlRun.Status.RUNNING)
            .first()
        )
        if active is not None:
            raise AlreadyRunning(active)
        return CrawlRun.objects.create(
            source_id=source_id,
            trigger=trigger,
            started_by=user if user and user.is_authenticated else None,
            stage="Starting…",
        )


def start_run(source_id: int, *, user=None) -> CrawlRun:
    """Create a manual run and execute it in the background."""
    run = create_run(source_id, trigger=CrawlRun.Trigger.MANUAL, user=user)
    _launch(run.pk)
    return run


def delete_articles_and_recrawl(source_id: int, *, user=None) -> tuple[CrawlRun, int]:
    """Delete a source's articles and crawl it again; returns the run and the number deleted.

    The run is created first, inside the same transaction, so this is refused while the
    source is being crawled and nothing is deleted if any step fails. Forgetting the last
    crawl makes an EIOS source read its whole backfill window again.
    """
    from django.contrib.admin.models import CHANGE, LogEntry

    from raw_content.models import RawArticle
    from sources.models import Source

    with transaction.atomic():
        run = create_run(source_id, trigger=CrawlRun.Trigger.MANUAL, user=user)
        _, per_model = RawArticle.objects.filter(source_id=source_id).delete()
        deleted = per_model.get(RawArticle._meta.label, 0)
        Source.objects.filter(pk=source_id).update(last_crawled_at=None)
        if user is not None and user.is_authenticated:
            LogEntry.objects.log_actions(
                user.pk,
                Source.objects.filter(pk=source_id),
                CHANGE,
                change_message=f"Articles deleted: {deleted}. Started a new crawl.",
            )
    log.warning(
        "source.articles_deleted",
        source_id=source_id,
        run_id=run.pk,
        user=user.get_username() if user is not None else "",
        per_model=per_model,
    )
    _launch(run.pk)
    return run, deleted


def request_stop(run: CrawlRun) -> None:
    """Ask the crawl to stop. A run whose process is already gone has nobody
    to obey the request, so it is marked interrupted right away."""
    CrawlRun.objects.filter(pk=run.pk, status=CrawlRun.Status.RUNNING).update(
        cancel_requested=True,
    )
    mark_stale_runs()


def execute_run(run_id: int) -> None:
    """Run the pipeline for a CrawlRun, recording progress and the outcome.

    Pipeline errors are recorded on the run and re-raised so callers (Celery)
    can apply back-off.
    """
    from crawler.pipeline import run_for_source

    run = CrawlRun.objects.select_related("source").get(pk=run_id)
    progress = RunProgress(run)
    log.info("run.start", run_id=run_id, source=run.source.name, trigger=run.trigger)
    progress.start_heartbeat()
    try:
        run_for_source(run.source_id, progress=progress)
    except CrawlCancelled:
        progress.finish(CrawlRun.Status.CANCELLED, stage="Stopped by user")
        log.info("run.cancelled", run_id=run_id)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        progress.finish(CrawlRun.Status.FAILED, stage="Failed", error=error)
        log.exception("run.failed", run_id=run_id)
        raise
    else:
        progress.finish(CrawlRun.Status.SUCCEEDED, stage="Done")
        log.info("run.done", run_id=run_id, processed=run.processed, matched=run.matched)
    finally:
        progress.stop_heartbeat()


def _run_in_thread(run_id: int) -> None:
    close_old_connections()
    try:
        execute_run(run_id)
    except Exception:
        pass  # already recorded on the run and logged
    finally:
        connections.close_all()


def _launch(run_id: int) -> None:
    threading.Thread(
        target=_run_in_thread, args=(run_id,), name=f"crawl-run-{run_id}", daemon=True,
    ).start()
