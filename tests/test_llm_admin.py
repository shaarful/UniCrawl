"""The Process with LLM button and the bulk send-to-queue action."""
from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
import redis
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse
from django.utils import timezone

from llm.gemini import ArticleError, Event, Extraction, ExtractionResult, ServiceError
from llm.models import ArticleExtraction
from llm.worker import ExtractionWorker
from raw_content.models import RawArticle
from sources.models import Source


def _article(n=1, **kwargs):
    source, _ = Source.objects.get_or_create(
        name="Test", url="https://example.com/", content_type=Source.ContentType.HTML,
    )
    defaults = {
        "title": "শিরোনাম",
        "body": "ঢাকায় ডেঙ্গু",
        "is_keyword_matched": True,
        "processing_status": RawArticle.ProcessingStatus.MATCHED,
    }
    return RawArticle.objects.create(
        source=source,
        url=f"https://example.com/{n}",
        http_status=200,
        fetched_at=timezone.now(),
        url_fingerprint=str(n),
        content_hash=str(n),
        **{**defaults, **kwargs},
    )


def _result():
    return ExtractionResult(
        extraction=Extraction(
            is_disease_event=True,
            events=[Event(disease="Dengue", country="Bangladesh", district="Dhaka",
                          confirmed_case_count=350)],
            summary="Dengue cases rose in Dhaka.",
            translated_title="Headline",
            translated_body="Dengue in Dhaka",
        ),
        prompt_tokens=11,
        response_tokens=7,
        input_truncated=False,
        raw_response="{}",
    )


def _process_url(article):
    return reverse("llm:article-process", args=[article.pk])


def _change_url(article):
    return reverse("admin:raw_content_rawarticle_change", args=[article.pk])


@pytest.fixture
def extractor():
    with patch("llm.services.GeminiExtractor") as cls:
        instance = cls.return_value
        instance.model = "test-model"
        instance.extract.return_value = _result()
        yield instance


# --- Button -----------------------------------------------------------------------

@pytest.mark.django_db
def test_article_page_has_the_llm_button_and_the_refetch_button(admin_client):
    article = _article()

    html = admin_client.get(_change_url(article)).content.decode()

    assert _process_url(article) in html
    assert "Process with LLM" in html
    assert reverse("crawler:article-refetch", args=[article.pk]) in html


@pytest.mark.django_db
def test_button_stores_the_result_and_reports_it(admin_client, extractor):
    article = _article()

    response = admin_client.post(_process_url(article), {"next": _change_url(article)}, follow=True)

    extraction = ArticleExtraction.objects.get(article=article)
    assert extraction.status == ArticleExtraction.Status.COMPLETED
    assert extraction.translated_body == "Dengue in Dhaka"
    assert extraction.events.get().disease == "dengue"
    extractor.extract.assert_called_once_with(article.title, article.body, "")
    assert response.redirect_chain[-1][0] == _change_url(article)
    html = response.content.decode()
    assert "1 event(s): dengue in Dhaka" in html
    assert "translated to English" in html


@pytest.mark.django_db
def test_article_page_shows_the_result(admin_client, extractor):
    article = _article()
    admin_client.post(_process_url(article))

    html = admin_client.get(_change_url(article)).content.decode()

    assert "LLM result" in html
    assert "&quot;disease_name&quot;: &quot;dengue&quot;" in html
    assert "&quot;confirmed_case_count&quot;: 350" in html
    assert "&quot;host&quot;: null" in html
    assert "Dengue cases rose in Dhaka." in html
    assert "Dengue in Dhaka" in html


@pytest.mark.django_db
def test_extracted_values_are_escaped_on_the_page(admin_client, extractor):
    result = _result()
    result.extraction.events = [Event(disease="<script>alert(1)</script>")]
    extractor.extract.return_value = result
    article = _article()
    admin_client.post(_process_url(article))

    html = admin_client.get(_change_url(article)).content.decode()

    assert "<script>alert(1)</script>" not in html


@pytest.mark.django_db
def test_button_on_an_irrelevant_article_says_so(admin_client, extractor):
    extractor.extract.return_value = ExtractionResult(
        extraction=Extraction(is_disease_event=False, summary="About sport."),
        prompt_tokens=1, response_tokens=1, input_truncated=False, raw_response="{}",
    )

    response = admin_client.post(_process_url(_article()), HTTP_ACCEPT="application/json")

    assert response.json() == {
        "ok": True, "message": "Processed by the LLM: not a disease event.",
    }


@pytest.mark.django_db
def test_button_processes_again_even_if_already_extracted(admin_client, extractor):
    article = _article()

    admin_client.post(_process_url(article))
    admin_client.post(_process_url(article))

    assert extractor.extract.call_count == 2
    assert ArticleExtraction.objects.get(article=article).events.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "error",
    [ArticleError("no output"), ServiceError("quota"), OSError("key file missing")],
    ids=["article", "service", "key-file"],
)
def test_failure_is_reported_and_keeps_the_earlier_result(admin_client, extractor, error):
    article = _article()
    admin_client.post(_process_url(article))
    extractor.extract.side_effect = error

    response = admin_client.post(_process_url(article), HTTP_ACCEPT="application/json")

    assert response.json()["ok"] is False
    assert "LLM processing failed" in response.json()["message"]
    assert ArticleExtraction.objects.get(article=article).events.count() == 1


@pytest.mark.django_db
def test_missing_configuration_is_reported(admin_client):
    with patch("llm.services.GeminiExtractor", side_effect=ImproperlyConfigured("Set RZDO")):
        response = admin_client.post(_process_url(_article()), HTTP_ACCEPT="application/json")

    assert response.json() == {"ok": False, "message": "LLM processing failed: Set RZDO"}


@pytest.mark.django_db
def test_button_requires_post_and_staff(client, admin_client):
    url = _process_url(_article())

    assert admin_client.get(url).status_code == 405
    assert client.post(url).status_code == 302  # to the admin login


@pytest.mark.django_db
def test_button_ignores_an_offsite_next(admin_client, extractor):
    article = _article()

    response = admin_client.post(_process_url(article), {"next": "https://evil.example.com/"})

    assert response.url == _change_url(article)


@pytest.mark.django_db
def test_older_queued_message_cannot_overwrite_a_button_result(admin_client, extractor):
    """Text changed after queueing: the button's newer result must survive the late message."""
    article = _article()
    admin_client.post(_process_url(article))
    worker_extractor = MagicMock()
    worker_extractor.model = "test-model"
    worker = ExtractionWorker(MagicMock(spec=redis.Redis), worker_extractor, once=True)

    worker._handle("1-0", {"article_id": str(article.pk), "title": "Old", "body": "Old text"})

    assert not worker_extractor.extract.called
    assert worker.stats["skipped"] == 1


@pytest.mark.django_db
def test_queued_copy_of_the_same_text_is_skipped_after_the_button(admin_client, extractor):
    article = _article()
    admin_client.post(_process_url(article))
    worker_extractor = MagicMock()
    worker_extractor.model = "test-model"
    worker = ExtractionWorker(MagicMock(spec=redis.Redis), worker_extractor, once=True)
    later_id = f"{int(timezone.now().timestamp() * 1000) + 60_000}-0"

    worker._handle(later_id, {"article_id": str(article.pk), "title": article.title,
                              "body": article.body})

    assert not worker_extractor.extract.called
    assert worker.stats["skipped"] == 1


# --- Run page ---------------------------------------------------------------------

@pytest.mark.django_db
def test_run_page_articles_include_the_llm_url():
    from crawler.views import _article_json

    article = _article()

    assert _article_json(article)["llm_url"] == _process_url(article)


# --- Article list -----------------------------------------------------------------

def _list_page(admin_client, **params):
    return admin_client.get(reverse("admin:raw_content_rawarticle_changelist"), params)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status, label",
    [
        (None, "Not processed"),
        (ArticleExtraction.Status.RETRYING, "Retrying"),
        (ArticleExtraction.Status.COMPLETED, "Completed"),
        (ArticleExtraction.Status.FAILED, "Failed (dead-lettered)"),
    ],
    ids=["none", "retrying", "completed", "failed"],
)
def test_article_list_shows_the_llm_status(admin_client, status, label):
    article = _article()
    if status:
        ArticleExtraction.objects.create(article=article, status=status, input_hash="h")

    html = _list_page(admin_client).content.decode()

    assert "LLM status" in html
    pill = rf'<span class="status-pill \w+">{re.escape(label)}</span>'
    assert re.search(rf'<td class="field-llm_status">{pill}</td>', html)


@pytest.mark.django_db
def test_article_list_shows_20_per_page(admin_client):
    for n in range(21):
        _article(n)

    response = _list_page(admin_client)

    assert len(response.context["cl"].result_list) == 20
    assert response.context["cl"].paginator.num_pages == 2


@pytest.mark.django_db
def test_article_list_can_show_all_articles_on_one_page(admin_client):
    for n in range(21):
        _article(n)

    assert "Show all" in _list_page(admin_client).content.decode()
    response = _list_page(admin_client, all="")

    assert len(response.context["cl"].result_list) == 21


@pytest.mark.django_db
def test_show_all_is_offered_for_more_than_the_old_200_row_limit():
    from django.contrib import admin

    assert admin.site._registry[RawArticle].list_max_show_all == 10_000


@pytest.mark.django_db
def test_show_all_is_not_offered_above_the_limit(admin_client):
    from django.contrib import admin

    for n in range(21):
        _article(n)

    with patch.object(admin.site._registry[RawArticle], "list_max_show_all", 20):
        html = _list_page(admin_client).content.decode()

    assert "Show all" not in html


@pytest.mark.django_db
def test_article_list_can_sort_by_llm_status(admin_client):
    done = _article(1)
    ArticleExtraction.objects.create(
        article=done, status=ArticleExtraction.Status.COMPLETED, input_hash="h",
    )
    failed = _article(2)
    ArticleExtraction.objects.create(
        article=failed, status=ArticleExtraction.Status.FAILED, input_hash="h",
    )
    column = list(_list_page(admin_client).context["cl"].list_display).index("llm_status")

    response = _list_page(admin_client, o=str(column))

    assert [a.pk for a in response.context["cl"].result_list] == [done.pk, failed.pk]


@pytest.mark.django_db
def test_article_list_status_does_not_add_a_query_per_article(
    admin_client, django_assert_max_num_queries,
):
    for n in range(20):
        article = _article(n)
        ArticleExtraction.objects.create(
            article=article, status=ArticleExtraction.Status.COMPLETED, input_hash="h",
        )

    with django_assert_max_num_queries(15):
        _list_page(admin_client)


# --- Bulk action ------------------------------------------------------------------

def _bulk(admin_client, articles, **publish):
    with patch("crawler.publisher.publish_matched", **publish) as publish_matched:
        response = admin_client.post(
            reverse("admin:raw_content_rawarticle_changelist"),
            {"action": "send_to_llm_queue", "_selected_action": [a.pk for a in articles]},
            follow=True,
        )
    return response.content.decode(), publish_matched


@pytest.mark.django_db
def test_bulk_action_sends_stuck_articles_and_marks_them_published(admin_client):
    articles = [_article(1), _article(2)]

    html, publish_matched = _bulk(admin_client, articles)

    assert publish_matched.call_count == 2
    assert "Sent 2 articles to the LLM queue." in html
    assert set(RawArticle.objects.values_list("processing_status", flat=True)) == {
        RawArticle.ProcessingStatus.PUBLISHED,
    }


@pytest.mark.django_db
def test_bulk_action_skips_duplicates_and_unmatched(admin_client):
    good = _article(1)
    duplicate = _article(2, is_duplicate=True)
    unmatched = _article(3, is_keyword_matched=False)

    html, publish_matched = _bulk(admin_client, [good, duplicate, unmatched])

    publish_matched.assert_called_once_with(good.pk)
    assert "Skipped 2 duplicate or unmatched." in html


@pytest.mark.django_db
def test_bulk_action_reports_redis_failure_and_leaves_articles_matched(admin_client):
    article = _article()

    html, _ = _bulk(admin_client, [article], side_effect=redis.ConnectionError("refused"))

    assert "LLM queue became unavailable" in html
    article.refresh_from_db()
    assert article.processing_status == RawArticle.ProcessingStatus.MATCHED
