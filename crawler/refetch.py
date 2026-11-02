"""Refetch stored articles from their original links (the "Refetch" button
and the admin bulk action).

Used when the first crawl could not read the page (timeout, 5xx, snippet-only
EIOS item) or the page has since changed. The page is fetched and extracted
the same way as during a crawl, then:

- body — replaced by the page text when the page is where the body came from
  (HTML sources, earlier original-link bodies) or when the page text is
  longer than the stored snippet / feed summary. EIOS fullText is kept. The
  page text is always kept in metadata["crawled_text"].
- author / published_at / source_name / title — filled from the page when
  missing (author is updated whenever the page states one).
- language, content hash, embedding and the keyword match are recomputed.
- a non-duplicate article that matches is (re-)sent to the LLM queue when it
  was not sent yet or its body changed; downstream upserts on article_id.

fetched_at stays the time of the original crawl; metadata records
refetched_at, refetch_count and the last link_error.
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog
from django.conf import settings
from django.utils import timezone

from crawler.dedup import content_hash as _ch
from crawler.extractors.base import ExtractedContent, parse_published
from crawler.pipeline import _detect_language, _fetch_original_page, _join_text, eios_match_text
from raw_content.models import RawArticle
from sources.models import Source

log = structlog.get_logger(__name__)


@dataclass
class RefetchResult:
    ok: bool
    message: str
    body_changed: bool = False
    published: bool = False
    error: str = ""  # why the page could not be read


def _replaces_body(article: RawArticle, page_text: str) -> bool:
    body_source = (article.metadata or {}).get("body_source")
    if body_source == "eios_fulltext":
        return False
    page_is_body = body_source == "original_link" or (
        not body_source and article.source.content_type == Source.ContentType.HTML
    )
    if page_is_body:
        return page_text != article.body
    return len(page_text) > len(article.body)


def _publish(article: RawArticle) -> str:
    """Send to the LLM queue; returns "" or why it failed (article stays MATCHED)."""
    from crawler.publisher import publish_matched

    try:
        publish_matched(article.pk)
    except NotImplementedError:
        return "publisher not wired"
    except Exception as exc:
        log.warning("refetch.publish_failed", id=article.pk, error=str(exc))
        return f"{type(exc).__name__}: {exc}"
    article.processing_status = RawArticle.ProcessingStatus.PUBLISHED
    article.save(update_fields=["processing_status"])
    return ""


def refetch_articles(article_ids) -> dict[int, RefetchResult]:
    """Refetch several articles (admin bulk action).

    Pages are downloaded in parallel, once per distinct URL; the database
    updates then run one by one in the calling thread.
    """
    from concurrent.futures import ThreadPoolExecutor

    urls = dict(RawArticle.objects.filter(pk__in=article_ids).values_list("pk", "url"))
    workers = max(1, min(settings.EIOS_LINK_FETCH_WORKERS, len(set(urls.values()))))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pages = {url: pool.submit(_fetch_original_page, url) for url in set(urls.values())}
        return {pk: refetch_article(pk, page=pages[url].result()) for pk, url in urls.items()}


def refetch_article(
    article_id: int, page: tuple[ExtractedContent | None, str] | None = None,
) -> RefetchResult:
    """Refetch one article; `page` is an already fetched _fetch_original_page result."""
    article = RawArticle.objects.select_related("source").get(pk=article_id)
    content, reason = page if page is not None else _fetch_original_page(article.url)
    page_text = content.body if content else ""

    metadata = dict(article.metadata or {})
    metadata["refetched_at"] = timezone.now().isoformat()
    metadata["refetch_count"] = metadata.get("refetch_count", 0) + 1
    if reason:
        metadata["link_error"] = reason
    else:
        metadata.pop("link_error", None)
    article.metadata = metadata
    fields = ["metadata"]

    if content is not None:
        if content.author:
            article.author = content.author[:512]
        if not article.published_at:
            article.published_at = parse_published(content.published_at)
        if not article.source_name:
            article.source_name = (content.site_name or article.source.name)[:255]
        if not article.title and content.title:
            article.title = content.title[:1024]
        fields += ["author", "published_at", "source_name", "title"]

    if not page_text:
        article.save(update_fields=fields)
        log.info("article.refetch_failed", id=article.pk, url=article.url, reason=reason)
        return RefetchResult(
            ok=False, message=f"Could not read the original page: {reason}.", error=reason,
        )

    metadata["crawled_text"] = page_text
    old_chars = len(article.body)
    body_changed = _replaces_body(article, page_text)
    if body_changed:
        article.body = page_text
        article.content_hash = _ch.content_hash(page_text)
        article.language = _detect_language(page_text, article.language)
        metadata["body_source"] = "original_link"
        fields += ["body", "content_hash", "language"]
        if settings.CRAWLER_SEMANTIC_DEDUP_ENABLED and not article.is_duplicate:
            from crawler.dedup.semantic import compute_embedding
            article.embedding = compute_embedding(page_text)
            fields.append("embedding")

    from crawler.filters.keyword import matches_active_diseases
    match_text = eios_match_text(metadata["eios"]) if "eios" in metadata else None
    diseases = matches_active_diseases(
        _join_text(article.body, match_text),
        language=article.language,
        keyword_overrides=article.source.keyword_filter_overrides,
    )
    article.is_keyword_matched = bool(diseases)
    article.matched_diseases = diseases or None
    fields += ["is_keyword_matched", "matched_diseases"]

    Status = RawArticle.ProcessingStatus
    was_sent = article.processing_status == Status.PUBLISHED
    send = not article.is_duplicate and bool(diseases) and (body_changed or not was_sent)
    if not article.is_duplicate and not was_sent:
        article.processing_status = Status.MATCHED if diseases else Status.PENDING
    elif send:
        article.processing_status = Status.MATCHED  # new text not sent yet
    fields.append("processing_status")
    article.save(update_fields=fields)

    parts = [
        f"body updated from the original page ({old_chars:,} → {len(page_text):,} chars)"
        if body_changed else "body unchanged"
    ]
    parts.append(f"matched {', '.join(diseases)}" if diseases else "no disease keywords matched")
    published = False
    if send:
        error = _publish(article)
        published = not error
        parts.append("re-sent to the LLM queue" if was_sent and published
                     else "sent to the LLM queue" if published
                     else f"not sent to the LLM queue ({error})")
    log.info(
        "article.refetched", id=article.pk, url=article.url,
        body_changed=body_changed, matched=diseases, published=published,
    )
    return RefetchResult(
        ok=True,
        message="Refetched: " + "; ".join(parts) + ".",
        body_changed=body_changed,
        published=published,
    )
