"""Store extraction results; shared by the queue worker and the admin button."""
from __future__ import annotations

import hashlib
import time
from datetime import date

from django.db import transaction

from llm.gemini import PROMPT_VERSION, Event, ExtractionResult, GeminiExtractor
from llm.models import ArticleExtraction, ExtractedEvent
from raw_content.models import RawArticle


def compute_input_hash(model: str, title: str, body: str) -> str:
    return hashlib.sha256("\0".join((model, PROMPT_VERSION, title, body)).encode()).hexdigest()


def _text(value: str | None, limit: int = 255) -> str:
    return (value or "").strip()[:limit]


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _event_row(extraction: ArticleExtraction, event: Event) -> ExtractedEvent:
    return ExtractedEvent(
        extraction=extraction,
        # Lowercase so one disease is not counted under two spellings.
        disease=_text(event.disease).lower(),
        host=_text(event.host),
        affected_population_size=event.affected_population_size,
        country=_text(event.country),
        division=_text(event.division),
        district=_text(event.district),
        village=_text(event.village),
        latitude=event.latitude,
        longitude=event.longitude,
        location_type=event.location_type or "",
        event_date=_date(event.event_date),
        confirmed_case_count=event.confirmed_case_count,
        suspected_case_count=event.suspected_case_count,
        fatality_count=event.fatality_count,
        response_action=(event.response_action or "").strip(),
        responding_authority=_text(event.responding_authority),
    )


def save_extraction(
    article_id: int, *, message_id: str, input_hash: str, model: str, result: ExtractionResult,
) -> ArticleExtraction:
    """Replace the article's stored extraction and events with this result."""
    extraction_data = result.extraction
    with transaction.atomic():
        extraction, _ = ArticleExtraction.objects.update_or_create(
            article_id=article_id,
            defaults={
                "status": ArticleExtraction.Status.COMPLETED,
                "is_disease_event": extraction_data.is_disease_event,
                "summary": extraction_data.summary.strip(),
                "reporter_author": _text(extraction_data.reporter_author, limit=512),
                "author_affiliation": _text(extraction_data.author_affiliation, limit=512),
                "publication_date": _date(extraction_data.publication_date),
                "translated_title": extraction_data.translated_title or "",
                "translated_body": extraction_data.translated_body or "",
                "input_hash": input_hash,
                "stream_message_id": message_id,
                "attempts": 0,
                "error": "",
                "llm_model": model,
                "prompt_version": PROMPT_VERSION,
                "prompt_tokens": result.prompt_tokens,
                "response_tokens": result.response_tokens,
                "input_truncated": result.input_truncated,
                "raw_response": result.raw_response,
            },
        )
        extraction.events.all().delete()
        ExtractedEvent.objects.bulk_create(
            _event_row(extraction, event) for event in extraction_data.events
        )
    return extraction


def process_article(article: RawArticle) -> ArticleExtraction:
    """Extract one article now, bypassing the queue, even if already extracted.

    Raises ArticleError or ServiceError; building the client can also raise
    ImproperlyConfigured or, for a missing or invalid key file, OSError or ValueError.
    """
    extractor = GeminiExtractor()
    published_at = article.published_at.isoformat() if article.published_at else ""
    result = extractor.extract(article.title, article.body, published_at)
    return save_extraction(
        article.pk,
        # Same form as a Redis ID, which is time-based, so a queued message sent
        # before this click cannot later overwrite this result.
        message_id=f"{int(time.time() * 1000)}-0",
        input_hash=compute_input_hash(extractor.model, article.title, article.body),
        model=extractor.model,
        result=result,
    )
