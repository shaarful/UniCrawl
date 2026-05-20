"""Pipeline integration tests."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from crawler.fetchers.base import FetchResult
from crawler.pipeline import run_for_source
from raw_content.models import RawArticle
from sources.models import Source

# ---------------------------------------------------------------------------
# Sample payloads
# ---------------------------------------------------------------------------

_HTML_BODY = b"""<html>
<head><title>Dengue outbreak in District X</title></head>
<body>
<article>
<h1>Dengue outbreak in District X</h1>
<p>The Ministry of Health reported 50 suspected dengue cases in District X.
Health officials are responding immediately to contain the outbreak.
Local hospitals have been put on alert and additional medical supplies dispatched
to the affected area. Community members urged to eliminate standing water.</p>
</article>
</body>
</html>"""

_RSS_BODY = b"""<?xml version="1.0"?>
<rss version="2.0">
<channel>
  <language>en</language>
  <item>
    <title>Cholera alert in coastal region</title>
    <link>https://example.com/cholera</link>
    <description>Health officials report a surge in cholera cases along the coast.</description>
  </item>
  <item>
    <title>H5N1 detected in poultry</title>
    <link>https://example.com/h5n1</link>
    <description>WHO confirms avian influenza detection in local poultry farms.</description>
  </item>
</channel>
</rss>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _source(content_type=Source.ContentType.HTML, url="https://example.com/news"):
    return Source.objects.create(name="Test", url=url, content_type=content_type)


def _result(url, body, status=200):
    return FetchResult(url=url, status_code=status, body=body, headers={}, final_url=url)


def _mock_fetcher(result: FetchResult):
    m = MagicMock()
    m.fetch.return_value = result
    return m


# ---------------------------------------------------------------------------
# HTML pipeline
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_html_pipeline_creates_one_raw_article():
    source = _source()
    with patch("crawler.pipeline._fetcher_for", return_value=_mock_fetcher(_result(source.url, _HTML_BODY))):
        run_for_source(source.id)

    assert RawArticle.objects.filter(source=source).count() == 1


@pytest.mark.django_db
def test_html_pipeline_persists_url_and_fingerprints():
    from crawler.dedup.url_fingerprint import fingerprint

    source = _source()
    with patch("crawler.pipeline._fetcher_for", return_value=_mock_fetcher(_result(source.url, _HTML_BODY))):
        run_for_source(source.id)

    article = RawArticle.objects.get(source=source)
    assert article.url == source.url
    assert article.http_status == 200
    assert article.url_fingerprint == fingerprint(source.url)
    assert len(article.content_hash) == 64


@pytest.mark.django_db
def test_html_pipeline_updates_source_crawl_state():
    source = _source()
    with patch("crawler.pipeline._fetcher_for", return_value=_mock_fetcher(_result(source.url, _HTML_BODY))):
        run_for_source(source.id)

    source.refresh_from_db()
    assert source.last_crawled_at is not None
    assert source.next_crawl_at > source.last_crawled_at
    assert source.consecutive_failures == 0
    assert not source.is_temporarily_unavailable


# ---------------------------------------------------------------------------
# RSS pipeline
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_rss_pipeline_creates_one_article_per_feed_entry():
    source = _source(content_type=Source.ContentType.RSS, url="https://example.com/feed.xml")
    with patch("crawler.pipeline._fetcher_for", return_value=_mock_fetcher(_result(source.url, _RSS_BODY))):
        run_for_source(source.id)

    assert RawArticle.objects.filter(source=source).count() == 2


@pytest.mark.django_db
def test_rss_pipeline_stores_per_entry_urls():
    source = _source(content_type=Source.ContentType.RSS, url="https://example.com/feed.xml")
    with patch("crawler.pipeline._fetcher_for", return_value=_mock_fetcher(_result(source.url, _RSS_BODY))):
        run_for_source(source.id)

    urls = set(RawArticle.objects.values_list("url", flat=True))
    assert "https://example.com/cholera" in urls
    assert "https://example.com/h5n1" in urls


# ---------------------------------------------------------------------------
# Dedup suppression
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_url_dedup_suppresses_second_crawl_of_same_page():
    source = _source()
    mock_fetcher = _mock_fetcher(_result(source.url, _HTML_BODY))
    with patch("crawler.pipeline._fetcher_for", return_value=mock_fetcher):
        run_for_source(source.id)  # first crawl
        run_for_source(source.id)  # second crawl — same URL

    articles = list(RawArticle.objects.filter(source=source).order_by("id"))
    assert len(articles) == 2
    original, dup = articles
    assert not original.is_duplicate
    assert dup.is_duplicate
    assert dup.duplicate_of_id == original.id
    assert dup.suppression_reason == RawArticle.SuppressionReason.URL
    assert dup.processing_status == RawArticle.ProcessingStatus.SUPPRESSED


@pytest.mark.django_db
def test_content_hash_dedup_suppresses_same_body_at_different_url():
    url_a = "https://alpha.example.com/article"
    url_b = "https://beta.example.com/article"
    source_a = Source.objects.create(name="Alpha", url=url_a, content_type=Source.ContentType.HTML)
    source_b = Source.objects.create(name="Beta", url=url_b, content_type=Source.ContentType.HTML)

    with patch("crawler.pipeline._fetcher_for", return_value=_mock_fetcher(_result(url_a, _HTML_BODY))):
        run_for_source(source_a.id)
    with patch("crawler.pipeline._fetcher_for", return_value=_mock_fetcher(_result(url_b, _HTML_BODY))):
        run_for_source(source_b.id)

    dup = RawArticle.objects.get(source=source_b)
    assert dup.is_duplicate
    assert dup.suppression_reason == RawArticle.SuppressionReason.CONTENT
    assert dup.processing_status == RawArticle.ProcessingStatus.SUPPRESSED


# ---------------------------------------------------------------------------
# Keyword filter and publisher stubs
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_pipeline_tolerates_keyword_filter_not_implemented():
    source = _source()
    with patch("crawler.pipeline._fetcher_for", return_value=_mock_fetcher(_result(source.url, _HTML_BODY))):
        run_for_source(source.id)  # must not raise

    article = RawArticle.objects.get(source=source)
    assert article.processing_status == RawArticle.ProcessingStatus.PENDING
    assert not article.is_keyword_matched


@pytest.mark.django_db
def test_pipeline_marks_article_published_when_filter_and_publisher_available():
    source = _source()
    with (
        patch("crawler.pipeline._fetcher_for", return_value=_mock_fetcher(_result(source.url, _HTML_BODY))),
        patch("crawler.filters.keyword.matches_active_diseases", return_value=["dengue"]),
        patch("crawler.publisher.publish_matched"),
    ):
        run_for_source(source.id)

    article = RawArticle.objects.get(source=source)
    assert article.is_keyword_matched
    assert article.matched_diseases == ["dengue"]
    assert article.processing_status == RawArticle.ProcessingStatus.PUBLISHED


@pytest.mark.django_db
def test_pipeline_stays_matched_when_publisher_not_implemented():
    source = _source()
    with (
        patch("crawler.pipeline._fetcher_for", return_value=_mock_fetcher(_result(source.url, _HTML_BODY))),
        patch("crawler.filters.keyword.matches_active_diseases", return_value=["dengue"]),
    ):
        run_for_source(source.id)

    article = RawArticle.objects.get(source=source)
    assert article.is_keyword_matched
    assert article.processing_status == RawArticle.ProcessingStatus.MATCHED


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_pipeline_raises_on_4xx_response():
    source = _source()
    with (
        patch("crawler.pipeline._fetcher_for", return_value=_mock_fetcher(_result(source.url, b"Not Found", status=404))),
        pytest.raises(RuntimeError, match="HTTP 404"),
    ):
        run_for_source(source.id)


@pytest.mark.django_db
def test_pipeline_does_not_raise_when_article_processing_errors():
    """Per-article failures are swallowed; the whole batch must complete."""
    source = _source(content_type=Source.ContentType.RSS, url="https://example.com/feed.xml")
    with (
        patch("crawler.pipeline._fetcher_for", return_value=_mock_fetcher(_result(source.url, _RSS_BODY))),
        patch("crawler.pipeline._process_article", side_effect=ValueError("boom")),
    ):
        run_for_source(source.id)  # must not raise

    source.refresh_from_db()
    assert source.last_crawled_at is not None  # crawl state still updated
