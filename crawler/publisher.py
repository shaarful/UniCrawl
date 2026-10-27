"""Outbound publisher to the LLM extraction queue (SRS FR-08 hand-off).

Pushes keyword-matched RawArticle records onto a Redis Streams topic for
the downstream LLM Processing Service. At-least-once delivery (NFR §5.3):
messages persist in the stream until the consumer acknowledges them via XACK.
"""
from __future__ import annotations

import json

import redis
import structlog
from django.conf import settings
from django.utils import timezone

log = structlog.get_logger(__name__)

_redis: redis.Redis | None = None


def _get_client() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def publish_matched(raw_article_id: int) -> None:
    """Push a keyword-matched article onto the LLM handoff stream (FR-08).

    Raises redis.RedisError on connection or command failure — the pipeline
    leaves the article in MATCHED status so it can be retried.
    """
    from raw_content.models import RawArticle

    article = RawArticle.objects.select_related("source").get(pk=raw_article_id)

    fields = {
        "article_id": str(article.id),
        "source_id": str(article.source_id),
        "url": article.url,
        "final_url": article.final_url,
        "title": article.title,
        "body": article.body,
        "language": article.language or "",
        "matched_diseases": json.dumps(article.matched_diseases or []),
        # Source-specific provenance, e.g. the complete EIOS record (countries,
        # locations, categories, translations) and which body was used.
        "metadata": json.dumps(article.metadata or {}, ensure_ascii=False),
        "fetched_at": article.fetched_at.isoformat(),
        "published_at": timezone.now().isoformat(),
    }

    msg_id = _get_client().xadd(settings.LLM_QUEUE_NAME, fields)
    log.info(
        "publisher.sent",
        article_id=raw_article_id,
        stream=settings.LLM_QUEUE_NAME,
        msg_id=msg_id,
    )
