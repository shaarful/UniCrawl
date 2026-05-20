"""Semantic dedup tests — cosine similarity math and DB duplicate detection.

The sentence-transformer model is mocked throughout so no model download is needed.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from django.utils import timezone

import crawler.dedup.semantic as sem_mod
from crawler.dedup.semantic import compute_embedding, find_duplicate, similarity_score
from raw_content.models import RawArticle
from sources.models import Source


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vec(*values: float) -> list[float]:
    """Construct a normalised unit vector from raw values."""
    arr = np.array(values, dtype=float)
    return (arr / np.linalg.norm(arr)).tolist()


def _mock_model(embedding: list[float]):
    """Return a mock SentenceTransformer whose encode() always returns `embedding`."""
    m = MagicMock()
    m.encode.return_value = np.array(embedding, dtype=float)
    return m


def _source():
    return Source.objects.create(
        name="Test", url="https://example.com/", content_type=Source.ContentType.HTML
    )


def _article(source, *, embedding: list[float] | None, url="https://example.com/a"):
    body = "article body text"
    from crawler.dedup.url_fingerprint import fingerprint
    from crawler.dedup.content_hash import content_hash

    return RawArticle.objects.create(
        source=source,
        url=url,
        final_url=url,
        http_status=200,
        fetched_at=timezone.now(),
        title="Title",
        body=body,
        url_fingerprint=fingerprint(url),
        content_hash=content_hash(body),
        embedding=embedding,
    )


# ---------------------------------------------------------------------------
# compute_embedding
# ---------------------------------------------------------------------------

def test_compute_embedding_returns_list_of_floats():
    vec = _vec(1, 0, 0)
    with patch.object(sem_mod, "_get_model", return_value=_mock_model(vec)):
        result = compute_embedding("dengue outbreak")
    assert isinstance(result, list)
    assert all(isinstance(v, float) for v in result)


def test_compute_embedding_returns_none_for_empty_string():
    assert compute_embedding("") is None


def test_compute_embedding_returns_none_for_whitespace_only():
    assert compute_embedding("   ") is None


def test_compute_embedding_calls_model_encode():
    vec = _vec(1, 0)
    mock = _mock_model(vec)
    with patch.object(sem_mod, "_get_model", return_value=mock):
        compute_embedding("some text")
    mock.encode.assert_called_once_with("some text", convert_to_numpy=True)


# ---------------------------------------------------------------------------
# similarity_score — pure math, no DB
# ---------------------------------------------------------------------------

def test_identical_texts_have_similarity_one():
    vec = _vec(1, 2, 3)
    mock = _mock_model(vec)
    with patch.object(sem_mod, "_get_model", return_value=mock):
        score = similarity_score("text", "text")
    assert abs(score - 1.0) < 1e-6


def test_orthogonal_embeddings_have_similarity_zero():
    call_count = 0

    def side_effect(text, **_kwargs):
        nonlocal call_count
        call_count += 1
        return np.array([1.0, 0.0] if call_count == 1 else [0.0, 1.0])

    mock = MagicMock()
    mock.encode.side_effect = side_effect
    with patch.object(sem_mod, "_get_model", return_value=mock):
        score = similarity_score("text a", "text b")
    assert abs(score) < 1e-6


def test_opposite_embeddings_have_negative_similarity():
    call_count = 0

    def side_effect(text, **_kwargs):
        nonlocal call_count
        call_count += 1
        return np.array([1.0, 0.0] if call_count == 1 else [-1.0, 0.0])

    mock = MagicMock()
    mock.encode.side_effect = side_effect
    with patch.object(sem_mod, "_get_model", return_value=mock):
        score = similarity_score("text a", "text b")
    assert score < 0


def test_similarity_score_returns_zero_for_empty_text():
    with patch.object(sem_mod, "_get_model", return_value=_mock_model([1.0, 0.0])):
        assert similarity_score("", "text") == 0.0
        assert similarity_score("text", "") == 0.0


# ---------------------------------------------------------------------------
# find_duplicate — DB lookups
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_find_duplicate_returns_none_when_no_articles_stored():
    result = find_duplicate(_vec(1, 0, 0), threshold=0.85)
    assert result is None


@pytest.mark.django_db
def test_find_duplicate_returns_id_when_similarity_meets_threshold():
    source = _source()
    stored_vec = _vec(1, 0, 0)
    article = _article(source, embedding=stored_vec)

    result = find_duplicate(stored_vec, threshold=0.85)
    assert result == article.id


@pytest.mark.django_db
def test_find_duplicate_returns_none_when_similarity_below_threshold():
    source = _source()
    # 45° angle → cosine = ~0.707, below 0.85
    _article(source, embedding=_vec(1, 0, 0))

    result = find_duplicate(_vec(1, 1, 0), threshold=0.85)
    assert result is None


@pytest.mark.django_db
def test_find_duplicate_skips_articles_with_null_embedding():
    source = _source()
    _article(source, embedding=None)

    result = find_duplicate(_vec(1, 0, 0), threshold=0.85)
    assert result is None


@pytest.mark.django_db
def test_find_duplicate_skips_articles_already_marked_duplicate():
    source = _source()
    original = _article(source, embedding=_vec(1, 0, 0), url="https://example.com/orig")
    dup = _article(source, embedding=_vec(1, 0, 0), url="https://example.com/dup")
    dup.is_duplicate = True
    dup.duplicate_of = original
    dup.save(update_fields=["is_duplicate", "duplicate_of"])

    result = find_duplicate(_vec(1, 0, 0), threshold=0.85)
    # Only the non-duplicate original should be found
    assert result == original.id


@pytest.mark.django_db
def test_find_duplicate_respects_max_candidates(settings):
    settings.CRAWLER_SEMANTIC_MAX_CANDIDATES = 1
    source = _source()
    # Two identical articles; with max_candidates=1 only the latest one is checked
    older = _article(source, embedding=_vec(1, 0, 0), url="https://example.com/old")
    newer = _article(source, embedding=_vec(1, 0, 0), url="https://example.com/new")

    result = find_duplicate(_vec(1, 0, 0), threshold=0.85)
    # Should find the newer one (ordered by -created_at, limit 1)
    assert result == newer.id


# ---------------------------------------------------------------------------
# Pipeline integration — semantic dedup enabled/disabled
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_pipeline_semantic_dedup_suppresses_when_enabled(settings):
    """Layer 3 fires when two different URLs have different bodies but the same embedding.

    Layer 1 (URL) won't fire because URLs differ.
    Layer 2 (content hash) won't fire because extracted bodies differ.
    Layer 3 fires because the mocked model returns the same vector for both.
    """
    from unittest.mock import patch as _patch
    from crawler.fetchers.base import FetchResult
    from crawler.pipeline import run_for_source

    settings.CRAWLER_SEMANTIC_DEDUP_ENABLED = True

    url_a = "https://alpha.example.com/dengue-report"
    url_b = "https://beta.example.com/fever-cases"
    source_a = Source.objects.create(
        name="Alpha", url=url_a, content_type=Source.ContentType.HTML, is_active=True,
    )
    source_b = Source.objects.create(
        name="Beta", url=url_b, content_type=Source.ContentType.HTML, is_active=True,
    )

    body_a = b"<html><head><title>Dengue report</title></head><body><article><p>District health officials confirmed dengue cases rising in the southern region last week.</p></article></body></html>"
    body_b = b"<html><head><title>Fever cases</title></head><body><article><p>Local hospital reported a surge in fever cases attributed to mosquito-borne illness this month.</p></article></body></html>"

    fixed_vec = _vec(1, 0, 0)
    mock = _mock_model(fixed_vec)  # always returns the same embedding regardless of text

    result_a = FetchResult(url=url_a, status_code=200, body=body_a, headers={}, final_url=url_a)
    result_b = FetchResult(url=url_b, status_code=200, body=body_b, headers={}, final_url=url_b)

    fetcher_a = MagicMock()
    fetcher_a.fetch.return_value = result_a
    fetcher_b = MagicMock()
    fetcher_b.fetch.return_value = result_b

    with _patch.object(sem_mod, "_get_model", return_value=mock):
        with _patch("crawler.pipeline._fetcher_for", return_value=fetcher_a):
            run_for_source(source_a.id)  # stores original with embedding
        with _patch("crawler.pipeline._fetcher_for", return_value=fetcher_b):
            run_for_source(source_b.id)  # same embedding → semantic duplicate

    original = RawArticle.objects.get(source=source_a)
    dup = RawArticle.objects.get(source=source_b)

    assert not original.is_duplicate
    assert dup.is_duplicate
    assert dup.suppression_reason == RawArticle.SuppressionReason.SEMANTIC
    assert dup.duplicate_of_id == original.id


@pytest.mark.django_db
def test_pipeline_skips_semantic_dedup_when_disabled(settings):
    from unittest.mock import patch as _patch
    from crawler.fetchers.base import FetchResult
    from crawler.pipeline import run_for_source

    settings.CRAWLER_SEMANTIC_DEDUP_ENABLED = False

    source = Source.objects.create(
        name="NoSem", url="https://example.com/nosem",
        content_type=Source.ContentType.HTML, is_active=True,
    )
    body = b"<html><head><title>Test</title></head><body><article><p>Some article content for testing purposes.</p></article></body></html>"
    result = FetchResult(url=source.url, status_code=200, body=body, headers={}, final_url=source.url)
    mock_fetcher = MagicMock()
    mock_fetcher.fetch.return_value = result

    with _patch("crawler.pipeline._fetcher_for", return_value=mock_fetcher), \
         _patch.object(sem_mod, "_get_model") as mock_model_loader:
        run_for_source(source.id)

    # Model must never be loaded when semantic dedup is disabled
    mock_model_loader.assert_not_called()
    article = RawArticle.objects.get(source=source)
    assert article.embedding is None
