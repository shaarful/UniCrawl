"""Store extraction results; shared by the queue worker and the admin button."""
from __future__ import annotations

import hashlib
import time

from django.db import transaction

from llm.gemini import PROMPT_VERSION, ExtractionResult, GeminiExtractor
from llm.models import ArticleExtraction, ExtractedEvent
from raw_content.models import RawArticle


def compute_input_hash(model: str, title: str, body: str) -> str:
    return hashlib.sha256("\0".join((model, PROMPT_VERSION, title, body)).encode()).hexdigest()


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
            ExtractedEvent(
                extraction=extraction,
                # Lowercase so one disease is not counted under two spellings.
                disease=event.disease.strip().lower()[:255],
                country=(event.country or "").strip()[:255],
                location=(event.location or "").strip()[:255],
                number_of_cases=event.number_of_cases,
            )
            for event in extraction_data.events
        )
    return extraction


def process_article(article: RawArticle) -> ArticleExtraction:
    """Extract one article now, bypassing the queue, even if already extracted.

    Raises ArticleError or ServiceError; building the client can also raise
    ImproperlyConfigured or, for a missing or invalid key file, OSError or ValueError.
    """
    extractor = GeminiExtractor()
    result = extractor.extract(article.title, article.body)
    return save_extraction(
        article.pk,
        # Same form as a Redis ID, which is time-based, so a queued message sent
        # before this click cannot later overwrite this result.
        message_id=f"{int(time.time() * 1000)}-0",
        input_hash=compute_input_hash(extractor.model, article.title, article.body),
        model=extractor.model,
        result=result,
    )
