"""The table behind each number on the crawl run page: articles by type of site."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from crawler import progress as P
from crawler.fetchers.eios import board_url
from crawler.models import CrawlRun
from crawler.pipeline import run_for_source
from crawler.site_types import site_type
from raw_content.models import RawArticle
from sources.models import Source

BOARD_URL = board_url("https://eios.example.org/portal/api", "1.0", "42")
NOT_PUBLIC = "not publicly accessible (GPHIN / social media)"


@pytest.fixture
def run(db):
    source = Source.objects.create(
        name="EIOS", url=BOARD_URL, content_type=Source.ContentType.EIOS,
    )
    started = timezone.now()
    return CrawlRun.objects.create(source=source, started_at=started, fetched_at=started)


def _article(run, source_type="webnews", body_source="original_link", link_error="",
             source_id="news", **fields):
    number = RawArticle.objects.count()
    url = f"https://example.com/{number}"
    metadata = {"body_source": body_source, "eios": {"source": {"id": source_id}}}
    if link_error:
        metadata["link_error"] = link_error
    return RawArticle.objects.create(
        source=run.source, url=url, http_status=200, fetched_at=run.fetched_at, body="Text",
        url_fingerprint=url, content_hash=url, source_type=source_type, metadata=metadata, **fields,
    )


def _summary(client, run, tile):
    return client.get(reverse("crawler:run-summary", args=[run.pk, tile])).json()


def _rows(client, run, tile):
    return {row["site_type"]: row for row in _summary(client, run, tile)["rows"]}


@pytest.mark.parametrize(("source_type", "is_gphin", "expected"), [
    ("Medical", True, "GPHIN"),
    ("Medical", False, "Medical"),
    ("socialmedia-facebook", False, "Social media"),
    ("SocialMedia-Telegram", False, "Social media"),
    ("webnews", False, "Web news"),
    ("wire", False, "Wire"),
    ("News Alert", False, "News Alert"),
    ("html", False, "Website"),
    ("rss", False, "RSS feed"),
    ("eios", False, "No type from EIOS"),
    ("", False, "Unknown"),
    (None, False, "Unknown"),
])
def test_site_type(source_type, is_gphin, expected):
    assert site_type(source_type, is_gphin) == expected


def test_groups_by_type_and_says_why_pages_could_not_be_crawled(admin_client, run):
    for _ in range(3):
        _article(run, "Medical", "eios_snippet", NOT_PUBLIC, source_id="gphin")
    _article(run, "socialmedia-facebook", "eios_snippet", NOT_PUBLIC)
    _article(run, "socialmedia-telegram", "eios_snippet", NOT_PUBLIC)
    _article(run, "webnews", "original_link")
    _article(run, "webnews", "original_link")
    _article(run, "webnews", "eios_fulltext")
    _article(run, "webnews", "eios_snippet", "HTTP 403")
    _article(run, "webnews", "eios_snippet", "HTTP 403")
    _article(run, "webnews", "eios_snippet", "timeout")
    _article(run, "Medical", "original_link", source_id="crossref")

    data = _summary(admin_client, run, "processed")

    assert data["total"] == 12
    assert data["show_text_sources"] is True
    assert data["reason_label"] == "Why not crawled"
    assert data["rows"] == [
        {"site_type": "Web news", "articles": 6, "our_crawl": 2, "eios_text": 1, "snippet": 3,
         "reasons": [{"reason": "HTTP 403", "count": 2}, {"reason": "timeout", "count": 1}]},
        {"site_type": "GPHIN", "articles": 3, "our_crawl": 0, "eios_text": 0, "snippet": 3,
         "reasons": [{"reason": NOT_PUBLIC, "count": 3}]},
        {"site_type": "Social media", "articles": 2, "our_crawl": 0, "eios_text": 0, "snippet": 2,
         "reasons": [{"reason": NOT_PUBLIC, "count": 2}]},
        {"site_type": "Medical", "articles": 1, "our_crawl": 1, "eios_text": 0, "snippet": 0,
         "reasons": []},
    ]


def test_a_link_error_on_an_article_with_full_text_is_not_a_reason(admin_client, run):
    _article(run, "webnews", "eios_fulltext", "HTTP 403")

    assert _rows(admin_client, run, "processed")["Web news"]["reasons"] == []


def test_each_stored_tile_counts_its_own_articles(admin_client, run):
    status = RawArticle.ProcessingStatus
    original = _article(run, "webnews", "original_link", is_keyword_matched=True,
                        processing_status=status.PUBLISHED)
    _article(run, "wire", "eios_snippet", is_keyword_matched=True,
             processing_status=status.MATCHED)
    _article(run, "Medical", "eios_fulltext", is_duplicate=True, duplicate_of=original,
             suppression_reason=RawArticle.SuppressionReason.CONTENT,
             processing_status=status.SUPPRESSED)

    assert set(_rows(admin_client, run, "processed")) == {"Web news", "Wire", "Medical"}
    assert set(_rows(admin_client, run, "full_text")) == {"Web news", "Medical"}
    assert set(_rows(admin_client, run, "new")) == {"Web news", "Wire"}
    assert set(_rows(admin_client, run, "matched")) == {"Web news", "Wire"}
    assert set(_rows(admin_client, run, "published")) == {"Web news"}
    duplicates = _summary(admin_client, run, "duplicates")
    assert duplicates["reason_label"] == "Why duplicate"
    assert duplicates["rows"][0]["site_type"] == "Medical"
    assert duplicates["rows"][0]["reasons"] == [{"reason": "Content hash match", "count": 1}]


def test_only_the_articles_of_this_run_are_counted(admin_client, run):
    other = CrawlRun.objects.create(
        source=run.source, started_at=run.started_at, fetched_at=timezone.now(),
    )
    _article(other, "wire")
    _article(run, "webnews")

    assert set(_rows(admin_client, run, "processed")) == {"Web news"}


def test_many_articles_are_counted_by_the_database_in_one_query(run, admin_client):
    """The number of sites and articles must not change what the page loads."""
    for _ in range(30):
        _article(run, "webnews")

    with CaptureQueriesContext(connection) as few:
        small = _summary(admin_client, run, "processed")
    for _ in range(100):
        _article(run, "webnews")
    with CaptureQueriesContext(connection) as many:
        large = _summary(admin_client, run, "processed")

    assert len(many) == len(few)
    assert len(large["rows"]) == len(small["rows"]) == 1
    assert large["total"] == 130


def test_skipped_and_failed_articles_are_recorded_by_type(admin_client, run):
    progress = P.RunProgress(run)
    progress.skip("Web news")
    progress.skip("Web news")
    progress.skip("GPHIN")
    progress.article_failed("Wire", "ValueError")
    progress.article_failed("Wire", "ValueError")
    progress.article_failed("Wire", "KeyError")
    for _ in range(3):
        progress.article(P.ERROR)
    progress.flush(force=True)

    skipped = _summary(admin_client, run, "skipped")
    assert skipped["recorded"] is True
    assert skipped["total"] == skipped["tile_count"] == 3
    assert [(row["site_type"], row["articles"]) for row in skipped["rows"]] == [
        ("Web news", 2), ("GPHIN", 1),
    ]
    errors = _summary(admin_client, run, "errors")
    assert errors["total"] == errors["tile_count"] == 3
    assert errors["rows"] == [{
        "site_type": "Wire", "articles": 3,
        "reasons": [{"reason": "ValueError", "count": 2}, {"reason": "KeyError", "count": 1}],
    }]


def test_a_crawl_records_the_type_of_skipped_and_failed_articles(run):
    """GPHIN is told apart by its source id, both when skipped and when it fails."""
    def item(id_, source_id, source_type):
        return {
            "id": id_, "title": "Ebola", "description": "Ebola", "languageIso": "en",
            "link": f"https://example.com/{id_}", "source": {"id": source_id, "type": source_type},
        }

    fetcher = MagicMock()
    fetcher.get_item.return_value = None
    fetcher.iter_board_items.return_value = iter([
        item("fb", "facebook", "socialmedia-facebook"),
        item("gp", "gphin", "Medical"),
    ])
    progress = P.RunProgress(run)
    with (
        patch("crawler.pipeline._fetcher_for", return_value=fetcher),
        patch("crawler.pipeline._process_article", side_effect=ValueError("broken")),
    ):
        run_for_source(run.source_id, progress)
    assert run.errors_by_type == {
        "Social media": {"ValueError": 1}, "GPHIN": {"ValueError": 1},
    }

    fetcher.iter_board_items.return_value = iter([
        item("fb", "facebook", "socialmedia-facebook"),
        item("gp", "gphin", "Medical"),
    ])
    with (
        patch("crawler.pipeline._fetcher_for", return_value=fetcher),
        patch("crawler.pipeline._already_stored", return_value=True),
    ):
        run_for_source(run.source_id, progress)
    assert run.skipped_by_type == {"Social media": 1, "GPHIN": 1}


def test_an_older_run_says_its_types_were_not_recorded(admin_client, run):
    CrawlRun.objects.filter(pk=run.pk).update(skipped=22)

    assert _summary(admin_client, run, "skipped")["recorded"] is False
    assert _summary(admin_client, run, "errors")["recorded"] is True  # none happened


def test_unknown_tile_is_not_found(admin_client, run):
    url = reverse("crawler:run-summary", args=[run.pk, "nope"])

    assert admin_client.get(url).status_code == 404


def test_summary_is_staff_only(client, run):
    response = client.get(reverse("crawler:run-summary", args=[run.pk, "processed"]))

    assert response.status_code == 302
