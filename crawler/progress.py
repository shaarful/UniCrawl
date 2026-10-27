"""Progress reporting from the pipeline to a CrawlRun row.

The pipeline calls a progress object at each step; `NullProgress` ignores it
(tests, shell runs), `RunProgress` keeps counters in memory and writes them to
the CrawlRun at most once per FLUSH_SECONDS. Each write also checks whether a
stop was requested and raises CrawlCancelled if so.
"""
from __future__ import annotations

import time

from django.utils import timezone

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

    def __init__(self, run) -> None:
        self.run = run
        # (start %, end %, expected articles, processed count at segment start)
        self._segment: tuple[float, float, int, int] | None = None
        self._last_flush = 0.0

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

        run = self.run
        run.status = status
        run.stage = stage
        run.error = error
        run.finished_at = timezone.now()
        if status == CrawlRun.Status.SUCCEEDED:
            run.percent = 100.0
        self.event("error" if error else "info", error or stage)
        run.save()
