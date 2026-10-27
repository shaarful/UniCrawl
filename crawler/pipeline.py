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

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta

import structlog
from django.conf import settings
from django.utils import timezone

from crawler import progress as _progress
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
    # Extra text the keyword filter sees but is not persisted as body
    # (e.g. EIOS English translations of non-English articles).
    match_text: str | None = None
    # Source-specific provenance persisted alongside the raw record (FR-13).
    metadata: dict | None = None


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
# EIOS candidates (FR-07)
# ---------------------------------------------------------------------------

# Re-read this much before the last crawl: EIOS imports articles with a lag, so
# items can land in a window after it was polled. Re-polled items are skipped.
_EIOS_OVERLAP = timedelta(minutes=15)

# Original links that cannot be fetched anonymously: GPHIN is subscription-only,
# and social posts sit behind anti-bot walls (no circumvention, SRS §2.5).
_EIOS_UNFETCHABLE_SOURCE_IDS = frozenset({"gphin"})
_EIOS_UNFETCHABLE_SOURCE_TYPE_PREFIXES = ("socialmedia",)

# Stop asking EIOS for `fullText` after this many detail responses without it —
# the service account lacks full-text access until WHO grants it.
_EIOS_FULLTEXT_MAX_MISSES = 3

# Concurrent original-link fetches per page of matched articles.
_EIOS_LINK_FETCH_WORKERS = 8

_EIOS_METADATA_FIELDS = (
    "id", "eiosUrl", "pubDate", "processedOnDate", "languageIso", "source",
    "categories", "countries", "locations", "isDuplicate", "hasDuplicates",
    "duplicateIdOf", "translatedTitle", "translatedDescription",
    "abstractiveSummary",
)


def _join_text(*parts: str | None) -> str:
    seen: list[str] = []
    for part in parts:
        part = (part or "").strip()
        if part and part not in seen:
            seen.append(part)
    return "\n\n".join(seen)


def _eios_link_fetchable(item: dict) -> bool:
    src = item.get("source") or {}
    if src.get("id") in _EIOS_UNFETCHABLE_SOURCE_IDS:
        return False
    return not (src.get("type") or "").lower().startswith(_EIOS_UNFETCHABLE_SOURCE_TYPE_PREFIXES)


def _fetch_original_body(link: str) -> str:
    import httpx

    from crawler.extractors.base import extract_html
    from crawler.fetchers.html import HTMLFetcher

    try:
        result = HTMLFetcher().fetch(link)
    except httpx.HTTPError as exc:
        log.warning("eios.link_fetch_failed", url=link, error=str(exc))
        return ""
    if result.status_code >= 400:
        log.warning("eios.link_fetch_failed", url=link, status=result.status_code)
        return ""
    return extract_html(result.body, url=result.final_url).body


class _EIOSFullText:
    """Asks EIOS for `fullText`, giving up for the run after repeated misses."""

    def __init__(self, fetcher, board_url: str) -> None:
        self._fetcher = fetcher
        self._board_url = board_url
        self._misses = 0

    def get(self, item_id: str) -> str:
        if self._misses >= _EIOS_FULLTEXT_MAX_MISSES:
            return ""
        detail = self._fetcher.get_item(self._board_url, item_id)
        if detail and detail.get("fullText"):
            self._misses = 0
            return detail["fullText"]
        self._misses += 1
        if self._misses == _EIOS_FULLTEXT_MAX_MISSES:
            log.info("eios.fulltext_unavailable", board_url=self._board_url)
        return ""


def _eios_window(source: Source, now) -> tuple:
    if source.last_crawled_at:
        return source.last_crawled_at - _EIOS_OVERLAP, now
    return now - timedelta(hours=settings.EIOS_BACKFILL_HOURS), now


def _eios_article(source: Source, item: dict, link: str) -> tuple[_Article, bool]:
    """Snippet-bodied article for an EIOS item, and whether it keyword-matches."""
    from crawler.filters.keyword import matches_active_diseases

    title = item.get("title") or ""
    language = item.get("languageIso") or source.language_hint or ""
    snippet = _join_text(item.get("description"), item.get("abstractiveSummary"))
    translation = _join_text(
        item.get("translatedTitle"),
        item.get("translatedDescription"),
        item.get("translatedAbstractiveSummary"),
    )
    matched = bool(matches_active_diseases(
        _join_text(title, snippet, translation),
        language=language,
        keyword_overrides=source.keyword_filter_overrides,
    ))
    article = _Article(
        url=link,
        final_url=link,
        http_status=200,
        title=title,
        body=snippet,
        language=language,
        published_at=item.get("pubDate"),
        match_text=_join_text(title, translation),
        metadata={
            "eios": {
                k: item[k] for k in _EIOS_METADATA_FIELDS if item.get(k) not in (None, "", [])
            },
            "body_source": "eios_snippet",
        },
    )
    return article, matched


def _eios_candidates(
    source: Source, fetcher, fetched_at, progress: _progress.NullProgress,
) -> Iterator[_Article]:
    """Board articles since the last crawl, with full text for keyword matches.

    The list endpoint only carries a snippet. Fetching the full body for every
    article (~75k/day on a broad board) is impractical, so the snippet plus its
    English translation is keyword-matched first and only matches are enriched:
    EIOS fullText if granted, else the original link, else the snippet.

    Articles are yielded page by page, so matches are persisted and published
    while the rest of the board is still being read (FR-08 ≤60s hand-off).
    """
    from concurrent.futures import ThreadPoolExecutor
    from itertools import batched

    from crawler.fetchers.eios import MAX_PAGE_SIZE
    from raw_content.models import RawArticle

    since, until = _eios_window(source, fetched_at)
    fulltext = _EIOSFullText(fetcher, source.url)
    seen: set[str] = set()
    span = (until - since).total_seconds() or 1.0

    def on_window(w_since, w_until, count: int) -> None:
        start = 100 * (w_since - since).total_seconds() / span
        end = 100 * (w_until - since).total_seconds() / span
        progress.stage(
            f"Reading EIOS {w_since:%d %b %H:%M}–{w_until:%H:%M} UTC ({count:,} articles)",
            percent=start,
        )
        progress.segment(start, end, count)

    progress.stage(
        f"Connecting to EIOS — reading {since:%d %b %H:%M} to {until:%d %b %H:%M} UTC",
        percent=0,
    )
    items = fetcher.iter_board_items(source.url, since=since, until=until, on_window=on_window)

    with ThreadPoolExecutor(max_workers=_EIOS_LINK_FETCH_WORKERS) as pool:
        for batch in batched(items, MAX_PAGE_SIZE, strict=False):
            pending = []
            for item in batch:
                link = item.get("link") or item.get("eiosUrl")
                if not link:
                    continue
                fp = _uf.fingerprint(link)
                stored = RawArticle.objects.filter(source=source, url_fingerprint=fp)
                if fp in seen or stored.exists():
                    continue  # re-polled in the overlap window — already stored
                seen.add(fp)

                article, matched = _eios_article(source, item, link)
                link_body = None
                if matched:
                    if full := fulltext.get(item["id"]):
                        article.body = full
                        article.metadata["body_source"] = "eios_fulltext"
                    elif _eios_link_fetchable(item):
                        link_body = pool.submit(_fetch_original_body, link)
                pending.append((article, link_body))

            for article, link_body in pending:
                if link_body is not None and len(body := link_body.result()) > len(article.body):
                    article.body = body
                    article.metadata["body_source"] = "original_link"
                yield article


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
        metadata=article.metadata,
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

def _process_article(source: Source, article: _Article, fetched_at) -> str:
    """Dedup, filter, persist and publish one article; returns a progress outcome."""
    from raw_content.models import RawArticle

    # Layer 1: URL fingerprint dedup
    dup_id = _uf.find_duplicate(article.url)
    if dup_id is not None:
        _suppress(source, article, fetched_at, dup_id, RawArticle.SuppressionReason.URL)
        return _progress.SUPPRESSED

    # Layer 2: content hash dedup (skip empty bodies — they would all hash identically)
    dup_id = _ch.find_duplicate(article.body) if article.body else None
    if dup_id is not None:
        _suppress(source, article, fetched_at, dup_id, RawArticle.SuppressionReason.CONTENT)
        return _progress.SUPPRESSED

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
                return _progress.SUPPRESSED

    # Language detection (FR-12)
    lang = _detect_language(article.body, article.language)

    # Keyword filter (FR-08)
    from crawler.filters.keyword import matches_active_diseases
    matched_diseases = matches_active_diseases(
        _join_text(article.body, article.match_text),
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
        metadata=article.metadata,
    )
    log.info(
        "article.persisted",
        id=record.id,
        url=article.url,
        is_matched=is_matched,
        language=lang,
        source=source.name,
    )

    if not is_matched:
        return _progress.STORED

    # Publish to LLM queue (FR-08 ≤60s SLA)
    try:
        from crawler.publisher import publish_matched
        publish_matched(record.id)
        record.processing_status = RawArticle.ProcessingStatus.PUBLISHED
        record.save(update_fields=["processing_status"])
        log.info("article.published", id=record.id, source=source.name)
        return _progress.PUBLISHED
    except NotImplementedError:
        # Publisher not yet wired — article stays MATCHED for retry
        log.warning("publisher.not_implemented", id=record.id, source=source.name)
        return _progress.MATCHED
    except Exception as exc:
        # e.g. Redis unreachable — the article is stored and stays MATCHED for retry
        log.warning("publisher.failed", id=record.id, source=source.name, error=str(exc))
        raise _progress.PublishFailed(f"{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_for_source(source_id: int, progress: _progress.NullProgress | None = None) -> None:
    """Execute the full pipeline for a single source.

    `progress` receives stage, per-article and event updates (crawl dashboard);
    it may raise progress.CrawlCancelled to stop the run, in which case the
    source's crawl state is left untouched.
    """
    progress = progress or _progress.NullProgress()
    source = Source.objects.get(pk=source_id)
    fetched_at = timezone.now()
    bound = log.bind(source=source.name, source_id=source_id)
    bound.info("pipeline.start", content_type=source.content_type)
    progress.begin(fetched_at)

    fetcher = _fetcher_for(source)
    candidates = None
    try:
        # Network errors propagate to the caller (scheduler) for FR-14 back-off.
        if source.content_type == Source.ContentType.EIOS:
            # Streamed: each article is processed as soon as its page is read.
            candidates = _eios_candidates(source, fetcher, fetched_at, progress)
        else:
            progress.stage(f"Fetching {source.url}")
            result = fetcher.fetch(source.url)
            if result.status_code >= 400:
                bound.warning("pipeline.fetch_error", status=result.status_code)
                raise RuntimeError(f"HTTP {result.status_code} fetching {source.url}")
            candidates = _extract_candidates(source, result)
            bound.info("pipeline.candidates", count=len(candidates))
            progress.stage(f"Processing {len(candidates)} articles", percent=0)
            progress.segment(0, 100, len(candidates))

        processed = 0
        publish_warned = False
        for article in candidates:
            processed += 1
            try:
                outcome = _process_article(source, article, fetched_at)
            except _progress.PublishFailed as exc:
                outcome = _progress.UNPUBLISHED
                if not publish_warned:
                    publish_warned = True
                    progress.event(
                        "warning",
                        "Matched articles are stored but not reaching the LLM queue "
                        f"(they stay 'matched' for retry): {exc}",
                    )
            except Exception as exc:
                bound.exception("pipeline.article_error", url=article.url, error=str(exc))
                progress.event("error", f"{article.url} — {type(exc).__name__}: {exc}")
                outcome = _progress.ERROR
            progress.article(outcome)
    finally:
        if hasattr(candidates, "close"):
            candidates.close()  # stopped mid-stream: shut down the EIOS link-fetch pool
        if hasattr(fetcher, "close"):
            fetcher.close()

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
    bound.info("pipeline.done", candidates=processed)
