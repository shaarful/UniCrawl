"""Refetching a stored article from its original link (crawler.refetch + views)."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from crawler.dedup.content_hash import content_hash
from crawler.extractors.base import ExtractedContent
from crawler.refetch import refetch_article
from raw_content.models import RawArticle
from sources.models import Source

LINK = "https://news.example.com/a"
PAGE_TEXT = "Health officials confirmed three Ebola cases in the district. " * 10


def _page(body=PAGE_TEXT, **fields):
    return ExtractedContent(title=None, body=body, published_at=None, language=None, **fields), ""


def _article(*, content_type=Source.ContentType.EIOS, body="Short snippet.", metadata=None,
             status=RawArticle.ProcessingStatus.PENDING, **fields) -> RawArticle:
    source = Source.objects.create(
        name="Feed", url="https://feed.example.org/", content_type=content_type,
        keyword_filter_overrides=["ebola"],
    )
    if metadata is None and content_type == Source.ContentType.EIOS:
        metadata = {"eios": {"id": "a", "title": "Weekly update"}, "body_source": "eios_snippet",
                    "link_error": "timeout"}
    return RawArticle.objects.create(
        source=source, url=LINK, final_url=LINK, http_status=200, fetched_at=timezone.now(),
        title="Weekly update", body=body, language="en", url_fingerprint="f",
        content_hash=content_hash(body), metadata=metadata, processing_status=status, **fields,
    )


def _refetch(article, page=None):
    page = page or _page()
    with patch("crawler.refetch._fetch_original_page", return_value=page) as fetch, \
         patch("crawler.publisher.publish_matched") as publish:
        result = refetch_article(article.pk)
    article.refresh_from_db()
    return result, fetch, publish


@pytest.mark.django_db
def test_snippet_article_gets_page_text_and_is_matched_and_sent():
    article = _article()

    result, fetch, publish = _refetch(article, _page(author="A. Writer"))

    fetch.assert_called_once_with(LINK)
    assert result.ok and result.body_changed and result.published
    assert article.body == PAGE_TEXT
    assert article.content_hash == content_hash(PAGE_TEXT)
    assert article.metadata["body_source"] == "original_link"
    assert article.metadata["crawled_text"] == PAGE_TEXT
    assert "link_error" not in article.metadata
    assert article.metadata["refetch_count"] == 1 and article.metadata["refetched_at"]
    assert article.author == "A. Writer"
    assert article.matched_diseases == ["ebola"]
    assert article.processing_status == RawArticle.ProcessingStatus.PUBLISHED
    publish.assert_called_once_with(article.pk)
    assert "sent to the LLM queue" in result.message


@pytest.mark.django_db
def test_failed_refetch_keeps_the_body_and_records_why():
    article = _article()

    result, _, publish = _refetch(article, (None, "HTTP 503"))

    assert not result.ok and "HTTP 503" in result.message
    assert article.body == "Short snippet."
    assert article.metadata["link_error"] == "HTTP 503"
    assert article.metadata["refetch_count"] == 1
    publish.assert_not_called()


@pytest.mark.django_db
def test_eios_fulltext_body_is_kept_but_page_text_is_stored():
    article = _article(body="EIOS full text.", metadata={"eios": {"id": "a"},
                                                         "body_source": "eios_fulltext"})

    result, _, _ = _refetch(article)

    assert result.ok and not result.body_changed
    assert article.body == "EIOS full text."
    assert article.metadata["crawled_text"] == PAGE_TEXT


@pytest.mark.django_db
def test_shorter_page_does_not_replace_a_longer_feed_summary():
    summary = "A long feed summary about Ebola. " * 20
    article = _article(content_type=Source.ContentType.RSS, body=summary, metadata=None)

    result, _, _ = _refetch(article, _page("Subscribe to read."))

    assert result.ok and not result.body_changed
    assert article.body == summary


@pytest.mark.django_db
def test_html_article_takes_the_current_page_text_and_fills_missing_fields():
    from datetime import UTC, datetime

    article = _article(content_type=Source.ContentType.HTML,
                       body="Old page text about Ebola. " * 30, metadata=None)

    result, _, _ = _refetch(article, _page(site_name="Daily Health Times"))
    article.refresh_from_db()

    assert result.body_changed and article.body == PAGE_TEXT
    assert article.source_name == "Daily Health Times"

    content, _ = _page()
    content.published_at = "2026-05-19"
    _refetch(article, (content, ""))
    assert article.published_at == datetime(2026, 5, 19, tzinfo=UTC)
    assert article.metadata["refetch_count"] == 2


@pytest.mark.django_db
def test_published_article_is_resent_only_when_its_body_changes():
    article = _article(content_type=Source.ContentType.HTML, body=PAGE_TEXT, metadata=None,
                       is_keyword_matched=True, matched_diseases=["ebola"],
                       status=RawArticle.ProcessingStatus.PUBLISHED)

    result, _, publish = _refetch(article)
    assert not result.body_changed and not result.published
    publish.assert_not_called()
    assert article.processing_status == RawArticle.ProcessingStatus.PUBLISHED

    result, _, publish = _refetch(article, _page(PAGE_TEXT + " Update: a fourth case."))
    assert result.body_changed and result.published
    assert "re-sent" in result.message
    publish.assert_called_once()


@pytest.mark.django_db
def test_suppressed_duplicate_is_refreshed_but_not_sent():
    article = _article(is_duplicate=True, status=RawArticle.ProcessingStatus.SUPPRESSED)

    result, _, publish = _refetch(article)

    assert result.body_changed and article.is_keyword_matched
    assert article.processing_status == RawArticle.ProcessingStatus.SUPPRESSED
    publish.assert_not_called()


@pytest.mark.django_db
def test_queue_failure_leaves_the_article_matched():
    article = _article()
    with patch("crawler.refetch._fetch_original_page", return_value=_page()), \
         patch("crawler.publisher.publish_matched", side_effect=ConnectionError("refused")):
        result = refetch_article(article.pk)
    article.refresh_from_db()

    assert result.ok and not result.published
    assert "not sent to the LLM queue" in result.message
    assert article.processing_status == RawArticle.ProcessingStatus.MATCHED


@pytest.mark.django_db
def test_eios_translation_is_matched_on_refetch():
    article = _article(metadata={"eios": {"id": "a", "translatedTitle": "Ebola cases rise"},
                                 "body_source": "eios_snippet"})

    _refetch(article, _page("হিন্দি পাঠ " * 40))

    assert article.matched_diseases == ["ebola"]


# --- Views ----------------------------------------------------------------------

@pytest.mark.django_db
def test_refetch_view_returns_json_for_the_run_page(admin_client):
    article = _article()
    with patch("crawler.refetch._fetch_original_page", return_value=_page()), \
         patch("crawler.publisher.publish_matched"):
        response = admin_client.post(reverse("crawler:article-refetch", args=[article.pk]),
                                     HTTP_ACCEPT="application/json")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "Refetched" in response.json()["message"]


@pytest.mark.django_db
def test_refetch_view_redirects_back_with_a_message(admin_client):
    article = _article()
    change_url = reverse("admin:raw_content_rawarticle_change", args=[article.pk])
    with patch("crawler.refetch._fetch_original_page", return_value=(None, "timeout")):
        response = admin_client.post(reverse("crawler:article-refetch", args=[article.pk]),
                                     {"next": change_url}, follow=True)

    assert response.redirect_chain[-1][0] == change_url
    assert "Could not read the original page: timeout." in response.content.decode()


@pytest.mark.django_db
def test_refetch_view_ignores_offsite_next(admin_client):
    article = _article()
    with patch("crawler.refetch._fetch_original_page", return_value=(None, "timeout")):
        response = admin_client.post(reverse("crawler:article-refetch", args=[article.pk]),
                                     {"next": "https://evil.example.com/"})

    assert response.url == reverse("admin:raw_content_rawarticle_change", args=[article.pk])


@pytest.mark.django_db
def test_refetch_view_requires_post_and_staff(client, admin_client):
    url = reverse("crawler:article-refetch", args=[_article().pk])
    assert admin_client.get(url).status_code == 405
    assert client.post(url).status_code == 302  # to the admin login


@pytest.mark.django_db
def test_admin_change_page_has_refetch_button(admin_client):
    article = _article()
    html = admin_client.get(
        reverse("admin:raw_content_rawarticle_change", args=[article.pk])).content.decode()

    assert reverse("crawler:article-refetch", args=[article.pk]) in html
    assert "Refetch from original link" in html
    assert "Source-stated fields" in html


# --- Bulk refetch (admin action) --------------------------------------------------

def _bulk(admin_client, articles, pages):
    """Run the admin action; `pages` maps URL -> _fetch_original_page result."""
    with patch("crawler.refetch._fetch_original_page",
               side_effect=lambda url: pages[url]) as fetch, \
         patch("crawler.publisher.publish_matched"):
        response = admin_client.post(
            reverse("admin:raw_content_rawarticle_changelist"),
            {"action": "refetch_from_original_link", "_selected_action": [a.pk for a in articles]},
            follow=True,
        )
    return response.content.decode(), fetch


def _more(article, url, **fields):
    """Another article from the same source, at `url`."""
    return RawArticle.objects.create(
        source=article.source, url=url, final_url=url, http_status=200,
        fetched_at=timezone.now(), title="Other", body="Short snippet.", language="en",
        url_fingerprint=url, content_hash=content_hash(url),
        metadata={"eios": {"id": url}, "body_source": "eios_snippet"}, **fields,
    )


@pytest.mark.django_db
def test_bulk_refetch_updates_each_article_and_summarises(admin_client):
    first = _article()
    second = _more(first, "https://news.example.com/b")
    blocked = _more(first, "https://news.example.com/c")
    pages = {LINK: _page(), second.url: _page("Nothing relevant here. " * 30),
             blocked.url: (None, "HTTP 403")}

    html, _ = _bulk(admin_client, [first, second, blocked], pages)

    for a in (first, second, blocked):
        a.refresh_from_db()
    assert first.processing_status == RawArticle.ProcessingStatus.PUBLISHED
    assert second.metadata["body_source"] == "original_link" and not second.is_keyword_matched
    assert blocked.body == "Short snippet." and blocked.metadata["link_error"] == "HTTP 403"
    assert ("Refetched 2 of 3 articles: 2 got new text from the page; "
            "1 sent to the LLM queue.") in html
    assert "1 could not be read: HTTP 403 ×1." in html


@pytest.mark.django_db
def test_bulk_refetch_fetches_a_shared_url_once(admin_client):
    first = _article()
    duplicate = _more(first, LINK, is_duplicate=True,
                      processing_status=RawArticle.ProcessingStatus.SUPPRESSED)

    _, fetch = _bulk(admin_client, [first, duplicate], {LINK: _page()})

    fetch.assert_called_once_with(LINK)
    duplicate.refresh_from_db()
    assert duplicate.body == PAGE_TEXT


@pytest.mark.django_db
def test_bulk_refetch_refuses_oversized_selections(admin_client):
    first = _article()
    others = [_more(first, f"https://news.example.com/{i}") for i in range(3)]

    with patch("raw_content.admin.MAX_BULK_REFETCH", 2):
        html, fetch = _bulk(admin_client, [first, *others], {})

    fetch.assert_not_called()
    assert "4 articles selected — refetch at most 2 at a time" in html
