"""End-to-end crawl pipeline for a single source (SRS FR-08, FR-11, FR-12, FR-13, FR-23).

Stage order:
    1. fetch          — pull content via the appropriate fetcher.
    2. extract        — extract main text body (HTML parsing / RSS field mapping).
    3. dedup          — URL fingerprint → content hash → semantic similarity (≥ 0.85
                        configurable). Suppress and link to original if matched.
    4. detect language — record source language; do not drop non-English content (FR-12).
    5. keyword filter — match against the active disease list.
    6. persist raw    — write RawArticle with provenance (FR-13).
    7. publish        — push matched records onto the LLM-handoff queue (FR-08
                        end-to-end SLA: ≤ 60 seconds).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import structlog
from django.conf import settings
from django.utils import timezone

from crawler.dedup import content_hash as _ch
from crawler.dedup import url_fingerprint as _uf
from sources.models import Source

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Fetcher selection
# ---------------------------------------------------------------------------

def _fetcher_for(source: Source):
    from crawler.fetchers.eios import EIOSFetcher
    from crawler.fetchers.html import HTMLFetcher
    from crawler.fetchers.rss import RSSFetcher
    from crawler.fetchers.social import SocialFetcher

    return {
        Source.ContentType.HTML: HTMLFetcher,
        Source.ContentType.RSS: RSSFetcher,
        Source.ContentType.EIOS: EIOSFetcher,
        Source.ContentType.SOCIAL: SocialFetcher,
    }[source.content_type]()


# ---------------------------------------------------------------------------
# Unified article shape (internal)
# ---------------------------------------------------------------------------

@dataclass
class _Article:
    url: str
    final_url: str
    http_status: int
    title: str
    body: str
    language: str
    published_at: str | None


def _extract_candidates(source: Source, result) -> list[_Article]:
    from crawler.extractors.base import extract_feed, extract_html

    if source.content_type == Source.ContentType.RSS:
        return [
            _Article(
                url=item.url,
                final_url=item.url,
                http_status=result.status_code,
                title=item.content.title or "",
                body=item.content.body,
                language=item.content.language or source.language_hint or "",
                published_at=item.content.published_at,
            )
            for item in extract_feed(result.body)
        ]

    content = extract_html(result.body, url=result.final_url)
    return [
        _Article(
            url=result.url,
            final_url=result.final_url,
            http_status=result.status_code,
            title=content.title or "",
            body=content.body,
            language=source.language_hint or "",
            published_at=content.published_at,
        )
    ]


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

def _detect_language(text: str, hint: str) -> str:
    if not text:
        return hint
    try:
        from langdetect import detect
        return detect(text) or hint
    except Exception:
        return hint


# ---------------------------------------------------------------------------
# Dedup helpers
# ---------------------------------------------------------------------------

def _suppress(
    source: Source,
    article: _Article,
    fetched_at,
    dup_id: int,
    reason: str,
) -> None:
    from raw_content.models import RawArticle

    RawArticle.objects.create(
        source=source,
        url=article.url,
        final_url=article.final_url,
        http_status=article.http_status,
        fetched_at=fetched_at,
        title=article.title,
        body=article.body,
        language=article.language,
        url_fingerprint=_uf.fingerprint(article.url),
        content_hash=_ch.content_hash(article.body),
        is_duplicate=True,
        duplicate_of_id=dup_id,
        suppression_reason=reason,
        processing_status=RawArticle.ProcessingStatus.SUPPRESSED,
    )
    log.info(
        "article.suppressed",
        url=article.url,
        reason=reason,
        original_id=dup_id,
        source=source.name,
    )


# ---------------------------------------------------------------------------
# Per-article processing
# ---------------------------------------------------------------------------

def _process_article(source: Source, article: _Article, fetched_at) -> None:
    from raw_content.models import RawArticle

    # Layer 1: URL fingerprint dedup
    dup_id = _uf.find_duplicate(article.url)
    if dup_id is not None:
        _suppress(source, article, fetched_at, dup_id, RawArticle.SuppressionReason.URL)
        return

    # Layer 2: content hash dedup (skip empty bodies — they would all hash identically)
    dup_id = _ch.find_duplicate(article.body) if article.body else None
    if dup_id is not None:
        _suppress(source, article, fetched_at, dup_id, RawArticle.SuppressionReason.CONTENT)
        return

    # Layer 3: semantic dedup (FR-23) — enabled via CRAWLER_SEMANTIC_DEDUP_ENABLED
    embedding: list[float] | None = None
    if settings.CRAWLER_SEMANTIC_DEDUP_ENABLED and article.body:
        from crawler.dedup.semantic import compute_embedding
        from crawler.dedup.semantic import find_duplicate as semantic_find
        embedding = compute_embedding(article.body)
        if embedding is not None:
            dup_id = semantic_find(embedding)
            if dup_id is not None:
                _suppress(source, article, fetched_at, dup_id, RawArticle.SuppressionReason.SEMANTIC)
                return

    # Language detection (FR-12)
    lang = _detect_language(article.body, article.language)

    # Keyword filter (FR-08)
    from crawler.filters.keyword import matches_active_diseases
    matched_diseases = matches_active_diseases(
        article.body,
        language=lang,
        keyword_overrides=source.keyword_filter_overrides,
    )
    is_matched = bool(matched_diseases)

    # Persist raw record (FR-13)
    status = (
        RawArticle.ProcessingStatus.MATCHED
        if is_matched
        else RawArticle.ProcessingStatus.PENDING
    )
    record = RawArticle.objects.create(
        source=source,
        url=article.url,
        final_url=article.final_url,
        http_status=article.http_status,
        fetched_at=fetched_at,
        title=article.title,
        body=article.body,
        language=lang,
        url_fingerprint=_uf.fingerprint(article.url),
        content_hash=_ch.content_hash(article.body),
        is_keyword_matched=is_matched,
        matched_diseases=matched_diseases or None,
        embedding=embedding,
        processing_status=status,
    )
    log.info(
        "article.persisted",
        id=record.id,
        url=article.url,
        is_matched=is_matched,
        language=lang,
        source=source.name,
    )

    # Publish to LLM queue (FR-08 ≤60s SLA)
    if is_matched:
        try:
            from crawler.publisher import publish_matched
            publish_matched(record.id)
            record.processing_status = RawArticle.ProcessingStatus.PUBLISHED
            record.save(update_fields=["processing_status"])
            log.info("article.published", id=record.id, source=source.name)
        except NotImplementedError:
            # Publisher not yet wired — article stays MATCHED for retry
            log.warning("publisher.not_implemented", id=record.id, source=source.name)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_for_source(source_id: int) -> None:
    """Execute the full pipeline for a single source."""
    source = Source.objects.get(pk=source_id)
    fetched_at = timezone.now()
    bound = log.bind(source=source.name, source_id=source_id)
    bound.info("pipeline.start", content_type=source.content_type)

    fetcher = _fetcher_for(source)
    result = fetcher.fetch(source.url)  # network errors propagate to caller (scheduler)

    if result.status_code >= 400:
        bound.warning("pipeline.fetch_error", status=result.status_code)
        raise RuntimeError(f"HTTP {result.status_code} fetching {source.url}")

    candidates = _extract_candidates(source, result)
    bound.info("pipeline.candidates", count=len(candidates))

    for article in candidates:
        try:
            _process_article(source, article, fetched_at)
        except Exception as exc:
            bound.exception("pipeline.article_error", url=article.url, error=str(exc))

    # Mark source successful and schedule next crawl (FR-10)
    source.last_crawled_at = fetched_at
    source.next_crawl_at = fetched_at + timedelta(seconds=source.crawl_interval_seconds)
    source.consecutive_failures = 0
    source.is_temporarily_unavailable = False
    source.unavailable_until = None
    source.save(update_fields=[
        "last_crawled_at",
        "next_crawl_at",
        "consecutive_failures",
        "is_temporarily_unavailable",
        "unavailable_until",
    ])
    bound.info("pipeline.done", candidates=len(candidates))
