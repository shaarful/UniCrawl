"""Read matched articles from the Redis stream and store what Gemini extracts.

Delivery is at-least-once: a message stays pending until its result is committed,
and is reclaimed if that does not happen. An article may therefore arrive twice,
and the refetch path also re-sends articles on purpose, so results are upserted
on article_id and unchanged input is not sent to Gemini again.

Failures are handled by whose fault they are:
- Bad message (malformed, or its article no longer exists): dead-lettered at once.
- ArticleError: retried, then dead-lettered after LLM_MAX_ATTEMPTS.
- ServiceError, Redis or database outage: the whole worker backs off and nothing is
  counted against the article, so an outage cannot empty the queue.

Finished messages are deleted from the stream, because the crawler does not trim
it and the article is safely in the database. This assumes this worker's consumer
group is the only reader of the stream.
"""
from __future__ import annotations

import os
import socket
import time
from collections import Counter

import redis
import structlog
from django.conf import settings
from django.db import InterfaceError, OperationalError, close_old_connections, transaction

from llm.gemini import ArticleError, ServiceError
from llm.models import ArticleExtraction
from llm.services import compute_input_hash, save_extraction
from raw_content.models import RawArticle

log = structlog.get_logger(__name__)

_BLOCK_MS = 5000
_BACKOFF_START_SECONDS = 5
_BACKOFF_MAX_SECONDS = 300
_OUTAGE_ERRORS = (ServiceError, redis.RedisError, OperationalError, InterfaceError)


def _stream_position(message_id: str) -> tuple[int, int]:
    milliseconds, _, sequence = message_id.partition("-")
    return int(milliseconds), int(sequence or 0)


class ExtractionWorker:
    def __init__(self, client: redis.Redis, extractor, *, once: bool = False) -> None:
        self.client = client
        self.extractor = extractor
        self.once = once
        self.stopped = False
        self.stats: Counter = Counter()
        self.consumer = f"{socket.gethostname()}-{os.getpid()}"
        self.stream = settings.LLM_QUEUE_NAME
        self.group = settings.LLM_CONSUMER_GROUP

    def stop(self, *_args) -> None:
        """Finish the current article, then exit. Usable as a signal handler."""
        self.stopped = True

    def run(self) -> None:
        """Process until stopped, or until the stream is empty when once=True."""
        group_ready = False
        backoff = _BACKOFF_START_SECONDS
        while not self.stopped:
            close_old_connections()
            try:
                if not group_ready:
                    self._ensure_group()
                    group_ready = True
                message = self._next_message()
                if message is None:
                    if self.once:
                        return
                    continue
                self._handle(*message)
                backoff = _BACKOFF_START_SECONDS
            except _OUTAGE_ERRORS as exc:
                if self.once:
                    raise
                log.error("llm.outage", error=str(exc), retry_in_seconds=backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
                group_ready = False  # Redis may have restarted without its data

    def _sleep(self, seconds: int) -> None:
        # In short steps so a stop request is noticed during a long back-off.
        for _ in range(seconds):
            if self.stopped:
                return
            time.sleep(1)

    def _ensure_group(self) -> None:
        # Starts from id 0 so articles queued before the first run are processed.
        try:
            self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def _next_message(self) -> tuple[str, dict] | None:
        # Messages another attempt left unfinished come first, once they have
        # been idle long enough that nobody can still be working on them.
        claimed = self.client.xautoclaim(
            self.stream,
            self.group,
            self.consumer,
            min_idle_time=settings.LLM_CLAIM_IDLE_SECONDS * 1000,
            start_id="0-0",
            count=1,
        )[1]
        if claimed:
            return claimed[0]
        new = self.client.xreadgroup(
            self.group,
            self.consumer,
            {self.stream: ">"},
            count=1,
            block=None if self.once else _BLOCK_MS,
        )
        return new[0][1][0] if new else None

    def _handle(self, message_id: str, fields: dict) -> None:
        try:
            article_id = int(fields["article_id"])
        except (KeyError, TypeError, ValueError):
            self._dead_letter(message_id, fields, "malformed message: no valid article_id")
            return
        title = fields.get("title", "")
        body = fields.get("body", "")
        if not (title.strip() or body.strip()):
            self._dead_letter(message_id, fields, "message has no title or body")
            return
        if not RawArticle.objects.filter(pk=article_id).exists():
            self._dead_letter(message_id, fields, f"article {article_id} not found")
            return

        input_hash = compute_input_hash(self.extractor.model, title, body)
        current = ArticleExtraction.objects.filter(article_id=article_id).first()
        if current is not None and self._already_superseded(current, input_hash, message_id):
            log.info("llm.skipped", article_id=article_id, msg_id=message_id)
            self.stats["skipped"] += 1
            self._finish(message_id)
            return

        try:
            result = self.extractor.extract(title, body, fields.get("published_at", ""))
        except ArticleError as exc:
            self._on_article_error(message_id, fields, article_id, input_hash, exc)
            return

        save_extraction(
            article_id,
            message_id=message_id,
            input_hash=input_hash,
            model=self.extractor.model,
            result=result,
        )
        self._finish(message_id)
        log.info(
            "llm.extracted",
            article_id=article_id,
            is_disease_event=result.extraction.is_disease_event,
            events=len(result.extraction.events),
            prompt_tokens=result.prompt_tokens,
            response_tokens=result.response_tokens,
        )
        self.stats["completed"] += 1

    @staticmethod
    def _already_superseded(
        current: ArticleExtraction, input_hash: str, message_id: str,
    ) -> bool:
        """True if this exact input is done, or a later message already replaced it."""
        if current.status != ArticleExtraction.Status.COMPLETED:
            return False
        return current.input_hash == input_hash or (
            _stream_position(current.stream_message_id) > _stream_position(message_id)
        )

    def _on_article_error(
        self, message_id: str, fields: dict, article_id: int, input_hash: str, exc: ArticleError,
    ) -> None:
        with transaction.atomic():
            record, _ = ArticleExtraction.objects.select_for_update().get_or_create(
                article_id=article_id,
                defaults={"input_hash": input_hash, "status": ArticleExtraction.Status.RETRYING},
            )
            # New text gets a fresh set of attempts.
            attempts = record.attempts + 1 if record.input_hash == input_hash else 1
            exhausted = attempts >= settings.LLM_MAX_ATTEMPTS
            record.input_hash = input_hash
            record.attempts = attempts
            record.error = str(exc)
            record.status = (
                ArticleExtraction.Status.FAILED if exhausted
                else ArticleExtraction.Status.RETRYING
            )
            record.save()
        if exhausted:
            self._dead_letter(message_id, fields, f"gave up after {attempts} attempts: {exc}")
        else:
            # Left pending: it is reclaimed after LLM_CLAIM_IDLE_SECONDS.
            log.warning("llm.article_failed", article_id=article_id, attempts=attempts,
                        error=str(exc))
            self.stats["retrying"] += 1

    def _dead_letter(self, message_id: str, fields: dict, reason: str) -> None:
        self.client.xadd(
            settings.LLM_DEAD_LETTER_QUEUE_NAME,
            {**fields, "failed_message_id": message_id, "failure_reason": reason[:1000]},
        )
        self._finish(message_id)
        log.error("llm.dead_lettered", msg_id=message_id, reason=reason[:1000])
        self.stats["dead_lettered"] += 1

    def _finish(self, message_id: str) -> None:
        pipe = self.client.pipeline(transaction=True)
        pipe.xack(self.stream, self.group, message_id)
        pipe.xdel(self.stream, message_id)
        pipe.execute()
