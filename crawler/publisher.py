"""Outbound publisher to the LLM extraction queue (SRS FR-08 hand-off).

Pushes keyword-matched RawArticle records onto a Redis Streams / Kafka topic for
the downstream LLM Processing Service. At-least-once delivery (NFR §5.3).
"""
from __future__ import annotations


def publish_matched(raw_article_id: int) -> None:
    """Enqueue a matched article for LLM processing. To be implemented."""
    raise NotImplementedError
