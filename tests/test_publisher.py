"""Publisher tests — Redis Streams xadd payload and error propagation."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import redis
from django.utils import timezone

from crawler.publisher import publish_matched
from raw_content.models import RawArticle
from sources.models import Source


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _source():
    return Source.objects.create(
        name="Test",
        url="https://example.com/feed",
        content_type=Source.ContentType.HTML,
    )


_UNSET = object()


def _article(source, *, matched_diseases=_UNSET):
    from crawler.dedup.url_fingerprint import fingerprint
    from crawler.dedup.content_hash import content_hash

    if matched_diseases is _UNSET:
        matched_diseases = ["dengue"]

    body = "Dengue outbreak reported in the southern district."
    return RawArticle.objects.create(
        source=source,
        url="https://example.com/article",
        final_url="https://example.com/article",
        http_status=200,
        fetched_at=timezone.now(),
        title="Dengue outbreak",
        body=body,
        language="en",
        url_fingerprint=fingerprint("https://example.com/article"),
        content_hash=content_hash(body),
        is_keyword_matched=True,
        matched_diseases=matched_diseases,
        processing_status=RawArticle.ProcessingStatus.MATCHED,
    )


def _mock_client(msg_id="1234567890123-0"):
    client = MagicMock(spec=redis.Redis)
    client.xadd.return_value = msg_id
    return client


# ---------------------------------------------------------------------------
# Payload correctness
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_publish_matched_calls_xadd_on_correct_stream(settings):
    source = _source()
    article = _article(source)
    mock_client = _mock_client()

    with patch("crawler.publisher._get_client", return_value=mock_client):
        publish_matched(article.id)

    mock_client.xadd.assert_called_once()
    stream_key = mock_client.xadd.call_args.args[0]
    assert stream_key == settings.LLM_QUEUE_NAME


@pytest.mark.django_db
def test_publish_matched_payload_contains_required_fields():
    source = _source()
    article = _article(source)
    mock_client = _mock_client()

    with patch("crawler.publisher._get_client", return_value=mock_client):
        publish_matched(article.id)

    fields = mock_client.xadd.call_args.args[1]
    assert fields["article_id"] == str(article.id)
    assert fields["source_id"] == str(source.id)
    assert fields["url"] == article.url
    assert fields["final_url"] == article.final_url
    assert fields["title"] == article.title
    assert fields["body"] == article.body
    assert fields["language"] == "en"
    assert "fetched_at" in fields
    assert "published_at" in fields


@pytest.mark.django_db
def test_publish_matched_encodes_matched_diseases_as_json():
    source = _source()
    article = _article(source, matched_diseases=["dengue", "cholera"])
    mock_client = _mock_client()

    with patch("crawler.publisher._get_client", return_value=mock_client):
        publish_matched(article.id)

    fields = mock_client.xadd.call_args.args[1]
    decoded = json.loads(fields["matched_diseases"])
    assert decoded == ["dengue", "cholera"]


@pytest.mark.django_db
def test_publish_matched_encodes_null_matched_diseases_as_empty_list():
    source = _source()
    article = _article(source, matched_diseases=None)
    mock_client = _mock_client()

    with patch("crawler.publisher._get_client", return_value=mock_client):
        publish_matched(article.id)

    fields = mock_client.xadd.call_args.args[1]
    assert json.loads(fields["matched_diseases"]) == []


@pytest.mark.django_db
def test_publish_matched_language_falls_back_to_empty_string():
    source = _source()
    body = "test body text"
    from crawler.dedup.url_fingerprint import fingerprint
    from crawler.dedup.content_hash import content_hash

    article = RawArticle.objects.create(
        source=source,
        url="https://example.com/nolang",
        final_url="https://example.com/nolang",
        http_status=200,
        fetched_at=timezone.now(),
        title="No language",
        body=body,
        language="",
        url_fingerprint=fingerprint("https://example.com/nolang"),
        content_hash=content_hash(body),
        processing_status=RawArticle.ProcessingStatus.MATCHED,
    )
    mock_client = _mock_client()

    with patch("crawler.publisher._get_client", return_value=mock_client):
        publish_matched(article.id)

    fields = mock_client.xadd.call_args.args[1]
    assert fields["language"] == ""


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_publish_matched_propagates_redis_error():
    """Redis failure must propagate so the pipeline leaves the article in MATCHED."""
    source = _source()
    article = _article(source)
    mock_client = MagicMock(spec=redis.Redis)
    mock_client.xadd.side_effect = redis.ConnectionError("refused")

    with (
        patch("crawler.publisher._get_client", return_value=mock_client),
        pytest.raises(redis.ConnectionError),
    ):
        publish_matched(article.id)


# ---------------------------------------------------------------------------
# Client caching
# ---------------------------------------------------------------------------

def test_get_client_returns_same_instance_on_repeated_calls():
    import crawler.publisher as pub_mod

    original = pub_mod._redis
    try:
        pub_mod._redis = None  # reset cached client
        with patch("redis.Redis.from_url", return_value=MagicMock()) as mock_from_url:
            c1 = pub_mod._get_client()
            c2 = pub_mod._get_client()
        assert c1 is c2
        mock_from_url.assert_called_once()  # connection created only once
    finally:
        pub_mod._redis = original  # restore original state
