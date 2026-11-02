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
from typing import TYPE_CHECKING

import structlog
from django.conf import settings
from django.utils import timezone

from crawler import progress as _progress
from crawler.dedup import content_hash as _ch
from crawler.dedup import url_fingerprint as _uf
from sources.models import Source

if TYPE_CHECKING:
    from crawler.extractors.base import ExtractedContent

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Fetcher selection
# ---------------------------------------------------------------------------

def _fetcher_for(source: Source):
    from crawler.fetchers.eios import EIOSFetcher
    from crawler.fetchers.html import HTMLFetcher
    from crawler.fetchers.rss import RSSFetcher

    return {
        Source.ContentType.HTML: HTMLFetcher,
        Source.ContentType.RSS: RSSFetcher,
        Source.ContentType.EIOS: EIOSFetcher,
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
    # Source-stated fields the crawler supplies (the LLM extracts the rest).
    author: str = ""
    source_name: str = ""
    source_type: str = ""


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
                author=item.content.author or "",
                source_name=item.content.site_name or source.name,
                source_type=source.content_type,
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
            author=content.author or "",
            source_name=source.name,
            source_type=source.content_type,
        )
    ]


def _already_stored(source: Source, fp: str) -> bool:
    """Whether this source already stored the article with URL fingerprint `fp`.

    Re-reading a feed or re-polling an EIOS window returns items the source
    already has. They are not new, so they are skipped outright rather than
    recorded as suppressed duplicates; repeats from *other* sources still go
    through the dedup layers (FR-11/FR-23).
    """
    from raw_content.models import RawArticle

    return RawArticle.objects.filter(source=source, url_fingerprint=fp).exists()


def _unseen(source: Source, candidates: list[_Article]) -> list[_Article]:
    """Candidates this source has not stored yet, without repeats within the batch."""
    seen: set[str] = set()
    unseen = []
    for article in candidates:
        fp = _uf.fingerprint(article.url)
        if fp in seen or _already_stored(source, fp):
            continue
        seen.add(fp)
        unseen.append(article)
    return unseen


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

# Original-article fetches: per-request timeout, and failures worth one retry.
_LINK_FETCH_TIMEOUT = 20.0
_LINK_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_LINK_RETRY_DELAY_SECONDS = 1.0


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


def _fetch_original_page(link: str) -> tuple[ExtractedContent | None, str]:
    """Extracted original article page, and why its text is missing ("" if not).

    The content (author, date, site name) is returned even when the page has
    no article text. Timeouts and transient server errors are retried once.
    Sites that refuse the crawler (403 etc.) are not worked around (SRS §2.5).
    """
    import time

    import httpx

    from crawler.extractors.base import extract_html
    from crawler.fetchers.html import HTMLFetcher

    fetcher = HTMLFetcher(timeout=_LINK_FETCH_TIMEOUT)
    reason = ""
    for attempt in (1, 2):
        try:
            result = fetcher.fetch(link)
        except httpx.TimeoutException:
            reason, retry = "timeout", True
        except httpx.HTTPError as exc:
            reason, retry = type(exc).__name__, False
        else:
            if result.status_code < 400:
                content_type = (result.headers.get("content-type") or "").lower()
                if "pdf" in content_type:
                    return None, "PDF (not extracted)"
                content = extract_html(result.body, url=result.final_url)
                return content, "" if content.body else "no article text found on page"
            reason = f"HTTP {result.status_code}"
            retry = result.status_code in _LINK_RETRY_STATUSES
        if not retry or attempt == 2:
            break
        time.sleep(_LINK_RETRY_DELAY_SECONDS)
    log.warning("link_fetch_failed", url=link, reason=reason)
    return None, reason


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


def eios_match_text(item: dict) -> str:
    """Title plus EIOS's English translation, matched alongside the body."""
    return _join_text(
        item.get("title"),
        item.get("translatedTitle"),
        item.get("translatedDescription"),
        item.get("translatedAbstractiveSummary"),
    )


def _eios_article(source: Source, item: dict, link: str) -> _Article:
    """Snippet-bodied article for an EIOS item; the body is replaced if the
    original page yields more text."""
    title = item.get("title") or ""
    language = item.get("languageIso") or source.language_hint or ""
    snippet = _join_text(item.get("description"), item.get("abstractiveSummary"))
    publisher = item.get("source") or {}
    return _Article(
        url=link,
        final_url=link,
        http_status=200,
        title=title,
        body=snippet,
        language=language,
        published_at=item.get("pubDate"),
        match_text=eios_match_text(item),
        source_name=publisher.get("name") or source.name,
        source_type=publisher.get("type") or source.content_type,
        metadata={
            # The complete EIOS record as returned (title, description, translations,
            # summary, countries, locations, categories, source, scores, ...), so
            # nothing EIOS provides is lost when the body is replaced by page text.
            "eios": dict(item),
            "body_source": "eios_snippet",
        },
    )


def _eios_candidates(
    source: Source, fetcher, fetched_at, progress: _progress.NullProgress,
) -> Iterator[_Article]:
    """Board articles since the last crawl, each with the fullest body available.

    The list endpoint only carries a snippet, so every article's body comes from
    EIOS fullText if the account is granted it, else the original article page
    (fetched in parallel), else the snippet. Keyword matching then runs on that
    full body plus the English translation, so diseases mentioned only deep in
    an article are still caught. `metadata.body_source` records which body was
    used and `metadata.link_error` why the original page was not.

    Articles are yielded as their pages arrive, so matches are persisted and
    published while the rest of the board is still being read (FR-08 ≤60s
    hand-off).
    """
    from concurrent.futures import (
        FIRST_COMPLETED,
        Future,
        ThreadPoolExecutor,
        as_completed,
        wait,
    )

    workers = settings.EIOS_LINK_FETCH_WORKERS
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

    def complete(article: _Article, page: Future | None = None) -> _Article:
        # Both versions are kept: EIOS's own text in metadata["eios"] and our
        # crawled page text in metadata["crawled_text"]. `body` is the best of
        # them: EIOS fullText, else the crawled text if longer than the snippet.
        # EIOS has no byline, so the author comes from the page.
        if page is not None:
            content, reason = page.result()
            body = content.body if content else ""
            if content is not None:
                article.author = content.author or ""
                article.published_at = article.published_at or content.published_at
            if body:
                article.metadata["crawled_text"] = body
            if article.metadata["body_source"] == "eios_fulltext":
                if reason:
                    article.metadata["link_error"] = reason
            elif len(body) > len(article.body):
                article.body = body
                article.metadata["body_source"] = "original_link"
            else:
                article.metadata["link_error"] = reason or "page text shorter than snippet"
        if article.metadata["body_source"] != "eios_snippet":
            progress.full_text()
        return article

    # Up to `in_flight` page fetches are outstanding so every worker stays busy.
    # Articles are released as soon as their own page arrives, so one slow site
    # never holds up the rest.
    in_flight = workers * 2
    pending: dict[Future, _Article] = {}
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for item in items:
            link = item.get("link") or item.get("eiosUrl")
            if not link:
                continue
            fp = _uf.fingerprint(link)
            if fp in seen or _already_stored(source, fp):
                progress.skip()  # re-polled in the overlap window — already stored
                continue
            seen.add(fp)

            article = _eios_article(source, item, link)
            if full := fulltext.get(item["id"]):
                article.body = full
                article.metadata["eios"]["fullText"] = full
                article.metadata["body_source"] = "eios_fulltext"
            if _eios_link_fetchable(item):
                pending[pool.submit(_fetch_original_page, link)] = article
            else:
                article.metadata["link_error"] = "not publicly accessible (GPHIN / social media)"
                yield complete(article)

            if len(pending) >= in_flight:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
            else:
                done = [page for page in pending if page.done()]
            for page in done:
                yield complete(pending.pop(page), page)

        for page in as_completed(list(pending)):
            yield complete(pending.pop(page), page)
    finally:
        # Stopped mid-stream: don't wait for pages still downloading.
        pool.shutdown(wait=False, cancel_futures=True)


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

def _raw_fields(source: Source, article: _Article, fetched_at) -> dict:
    """RawArticle fields shared by stored and suppressed records (FR-13)."""
    from crawler.extractors.base import parse_published

    return {
        "source": source,
        "url": article.url,
        "final_url": article.final_url,
        "http_status": article.http_status,
        "fetched_at": fetched_at,
        "title": article.title,
        "body": article.body,
        "published_at": parse_published(article.published_at),
        "author": article.author[:512],
        "source_name": article.source_name[:255],
        "source_type": article.source_type[:64],
        "url_fingerprint": _uf.fingerprint(article.url),
        "content_hash": _ch.content_hash(article.body),
        "metadata": article.metadata,
    }


def _suppress(
    source: Source,
    article: _Article,
    fetched_at,
    dup_id: int,
    reason: str,
) -> None:
    from raw_content.models import RawArticle

    RawArticle.objects.create(
        **_raw_fields(source, article, fetched_at),
        language=article.language,
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
        **_raw_fields(source, article, fetched_at),
        language=lang,
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
            extracted = _extract_candidates(source, result)
            candidates = _unseen(source, extracted)
            skipped = len(extracted) - len(candidates)
            bound.info("pipeline.candidates", count=len(candidates), already_stored=skipped)
            progress.skip(skipped)
            progress.stage(
                f"Processing {len(candidates)} new articles ({skipped} already stored)",
                percent=0,
            )
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
