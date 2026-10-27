"""Progress reporting from the pipeline to a CrawlRun row.

The pipeline calls a progress object at each step; `NullProgress` ignores it
(tests, shell runs), `RunProgress` keeps counters in memory and writes them to
the CrawlRun at most once per FLUSH_SECONDS. Each write also checks whether a
stop was requested and raises CrawlCancelled if so.

A separate heartbeat thread touches `heartbeat_at` every HEARTBEAT_SECONDS
while the run is alive, even when the pipeline is blocked on a slow site. If
the process dies (e.g. runserver reloading), the heartbeat stops and the run
is marked interrupted shortly after (see runs.STALE_AFTER).
"""
from __future__ import annotations

import threading
import time

import structlog
from django.utils import timezone

log = structlog.get_logger(__name__)

# Outcomes returned by pipeline._process_article, plus "error".
SUPPRESSED = "suppressed"
STORED = "stored"
MATCHED = "matched"
PUBLISHED = "published"
UNPUBLISHED = "unpublished"  # matched and stored, but the LLM queue hand-off failed
ERROR = "error"

MAX_EVENTS = 50


class CrawlCancelled(Exception):
    """Raised inside the pipeline when the user stopped the run."""


class PublishFailed(Exception):
    """A matched article was stored but could not be handed to the LLM queue."""


class NullProgress:
    def begin(self, fetched_at) -> None:
        pass

    def stage(self, text: str, percent: float | None = None) -> None:
        pass

    def segment(self, start: float, end: float, count: int) -> None:
        pass

    def article(self, outcome: str) -> None:
        pass

    def skip(self, count: int = 1) -> None:
        pass

    def full_text(self) -> None:
        pass

    def event(self, level: str, message: str) -> None:
        pass


class RunProgress(NullProgress):
    FLUSH_SECONDS = 1.0
    HEARTBEAT_SECONDS = 10.0

    def __init__(self, run) -> None:
        self.run = run
        # (start %, end %, expected articles, processed count at segment start)
        self._segment: tuple[float, float, int, int] | None = None
        self._last_flush = 0.0
        self._heartbeat_stop = threading.Event()
        self._heartbeat: threading.Thread | None = None

    # --- Heartbeat ------------------------------------------------------------

    def start_heartbeat(self) -> None:
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop, name=f"crawl-run-{self.run.pk}-heartbeat", daemon=True,
        )
        self._heartbeat.start()

    def stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()

    def beat(self) -> None:
        from crawler.models import CrawlRun

        CrawlRun.objects.filter(pk=self.run.pk).update(heartbeat_at=timezone.now())

    def _heartbeat_loop(self) -> None:
        from django.db import connection

        try:
            while not self._heartbeat_stop.wait(self.HEARTBEAT_SECONDS):
                try:
                    self.beat()
                except Exception as exc:  # a missed beat must never kill the loop
                    log.warning("run.heartbeat_failed", run_id=self.run.pk, error=str(exc))
        finally:
            connection.close()

    # --- Pipeline hooks -------------------------------------------------------

    def begin(self, fetched_at) -> None:
        self.run.fetched_at = fetched_at
        self.flush(force=True)

    def stage(self, text: str, percent: float | None = None) -> None:
        if text != self.run.stage:
            self.event("info", text)
        self.run.stage = text[:255]
        if percent is not None:
            self.run.percent = max(0.0, min(100.0, percent))
        self.flush(force=True)

    def segment(self, start: float, end: float, count: int) -> None:
        """The next `count` articles move the progress bar from `start` to `end` %."""
        self._segment = (start, end, count, self.run.processed)
        self.run.percent = start if count else end

    def article(self, outcome: str) -> None:
        run = self.run
        run.processed += 1
        if outcome == SUPPRESSED:
            run.duplicates += 1
        elif outcome == ERROR:
            run.errors += 1
        else:
            run.new += 1
            if outcome in (MATCHED, PUBLISHED, UNPUBLISHED):
                run.matched += 1
            if outcome == PUBLISHED:
                run.published += 1
        if self._segment:
            start, end, count, base = self._segment
            done = min(1.0, (run.processed - base) / count) if count else 1.0
            run.percent = start + (end - start) * done
        self.flush()

    def skip(self, count: int = 1) -> None:
        """Items the source had already stored — not reprocessed."""
        self.run.skipped += count
        self.flush()  # long already-stored stretches still report and honour Stop

    def full_text(self) -> None:
        """An article got its full body (original page or EIOS fullText)."""
        self.run.full_text += 1

    def event(self, level: str, message: str) -> None:
        events = self.run.events = list(self.run.events or [])
        events.append({"t": timezone.now().isoformat(), "level": level, "msg": message[:500]})
        del events[:-MAX_EVENTS]

    # --- Persistence ----------------------------------------------------------

    def flush(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_flush < self.FLUSH_SECONDS:
            return
        self._last_flush = now
        from crawler.models import CrawlRun

        run = self.run
        CrawlRun.objects.filter(pk=run.pk).update(
            stage=run.stage, percent=run.percent, fetched_at=run.fetched_at,
            processed=run.processed, new=run.new, matched=run.matched,
            published=run.published, duplicates=run.duplicates, errors=run.errors,
            skipped=run.skipped, full_text=run.full_text, events=run.events,
            heartbeat_at=timezone.now(),
        )
        if CrawlRun.objects.filter(pk=run.pk, cancel_requested=True).exists():
            raise CrawlCancelled

    def finish(self, status: str, *, stage: str, error: str = "") -> None:
        from crawler.models import CrawlRun

        self.stop_heartbeat()
        run = self.run
        run.status = status
        run.stage = stage
        run.error = error
        run.finished_at = timezone.now()
        if status == CrawlRun.Status.SUCCEEDED:
            run.percent = 100.0
        self.event("error" if error else "info", error or stage)
        run.save()
