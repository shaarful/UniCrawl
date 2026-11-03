"""The admin pages for crawl runs, LLM results and extracted events."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone

from crawler.models import CrawlRun
from llm.models import ArticleExtraction, ExtractedEvent
from raw_content.models import RawArticle
from sources.models import Source

RUN_LIST = "admin:crawler_crawlrun_changelist"
RESULT_LIST = "admin:llm_articleextraction_changelist"
EVENT_LIST = "admin:llm_extractedevent_changelist"


def _source():
    return Source.objects.get_or_create(name="Feed", url="https://example.org/feed")[0]


def _run(**kwargs):
    now = timezone.now()
    defaults = {"source": _source(), "status": CrawlRun.Status.SUCCEEDED, "started_at": now,
                "finished_at": now + timedelta(seconds=75)}
    return CrawlRun.objects.create(**{**defaults, **kwargs})


def _article(n=1, **kwargs):
    defaults = {
        "source": _source(), "url": f"https://news.example.com/{n}", "title": f"Article {n}",
        "body": "Body.", "http_status": 200, "fetched_at": timezone.now(),
        "url_fingerprint": str(n), "content_hash": str(n), "source_name": "Daily News",
    }
    return RawArticle.objects.create(**{**defaults, **kwargs})


def _extraction(n=1, **kwargs):
    defaults = {"status": ArticleExtraction.Status.COMPLETED, "is_disease_event": True,
                "summary": "A summary.", "prompt_tokens": 2345, "response_tokens": 120}
    return ArticleExtraction.objects.create(article=_article(n), **{**defaults, **kwargs})


def _event(extraction=None, **kwargs):
    defaults = {"disease": "anthrax", "host": "human", "village": "Gangni", "district": "Meherpur",
                "country": "Bangladesh", "confirmed_case_count": 62, "fatality_count": 0,
                "event_date": date(2023, 12, 1)}
    return ExtractedEvent.objects.create(
        extraction=extraction or _extraction(), **{**defaults, **kwargs},
    )


def _viewer(client, django_user_model, *codenames):
    user = django_user_model.objects.create_user("viewer", password="x", is_staff=True)
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
    client.force_login(user)


# --- Nothing here is added or edited by hand --------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize(
    "model", ["crawler_crawlrun", "llm_articleextraction", "llm_extractedevent"],
)
def test_records_cannot_be_added_by_hand(admin_client, model):
    assert admin_client.get(reverse(f"admin:{model}_add")).status_code == 403


@pytest.mark.django_db
def test_detail_pages_cannot_be_saved(admin_client):
    run, event = _run(), _event()
    urls = (
        reverse("admin:crawler_crawlrun_change", args=[run.pk]),
        reverse("admin:llm_articleextraction_change", args=[event.extraction_id]),
        reverse("admin:llm_extractedevent_change", args=[event.pk]),
    )

    for url in urls:
        response = admin_client.get(url)
        assert response.status_code == 200, url
        assert 'name="_save"' not in response.content.decode(), url
        assert admin_client.post(url, {"status": "failed", "disease": "x"}).status_code == 403, url

    event.refresh_from_db()
    assert event.disease == "anthrax"


# --- Crawl runs --------------------------------------------------------------------------

@pytest.mark.django_db
def test_run_list_shows_the_run_at_a_glance(admin_client, admin_user):
    run = _run(started_by=admin_user, processed=19, new=17, matched=2, published=1, duplicates=3)

    response = admin_client.get(reverse(RUN_LIST))

    content = response.content.decode()
    assert response.context["title"] == "Crawl runs"
    assert f"#{run.pk} Feed" in content
    assert f"Manual by {admin_user.get_username()}" in content
    assert ">Succeeded</span>" in content
    assert "Took 1m 15s" in content
    assert f'href="{reverse("crawler:run", args=[run.pk])}"' in content


@pytest.mark.django_db
@pytest.mark.parametrize("status,kind,label", [
    (CrawlRun.Status.RUNNING, "warn", "Running"),
    (CrawlRun.Status.SUCCEEDED, "ok", "Succeeded"),
    (CrawlRun.Status.FAILED, "bad", "Failed"),
    (CrawlRun.Status.CANCELLED, "off", "Stopped"),
    (CrawlRun.Status.INTERRUPTED, "bad", "Interrupted"),
])
def test_run_status_pill(admin_client, status, kind, label):
    _run(status=status, heartbeat_at=timezone.now(), finished_at=None)

    content = admin_client.get(reverse(RUN_LIST)).content.decode()

    assert f'<span class="status-pill {kind}">{label}</span>' in content


@pytest.mark.django_db
def test_run_list_marks_runs_whose_process_died(admin_client):
    run = _run(status=CrawlRun.Status.RUNNING, finished_at=None)
    CrawlRun.objects.filter(pk=run.pk).update(heartbeat_at=timezone.now() - timedelta(hours=1))

    admin_client.get(reverse(RUN_LIST))

    run.refresh_from_db()
    assert run.status == CrawlRun.Status.INTERRUPTED


@pytest.mark.django_db
def test_run_page_shows_a_readable_timeline(admin_client):
    run = _run(events=[
        {"t": "2026-11-03T07:26:22+00:00", "msg": "Fetching <b>page</b>", "level": "info"},
        {"t": "not a time", "msg": "Slow site", "level": "warning"},
        "unexpected entry",
    ])

    response = admin_client.get(reverse("admin:crawler_crawlrun_change", args=[run.pk]))

    content = response.content.decode()
    assert response.context["title"] == "Crawl run"
    assert response.context["subtitle"] == f"Run #{run.pk} · Feed"
    assert "07:26:22" in content
    assert "Fetching &lt;b&gt;page&lt;/b&gt;" in content
    assert '<span class="status-pill warn">warning</span>' in content
    assert "Slow site" in content
    assert "unexpected entry" not in content
    assert f'href="{reverse("crawler:run", args=[run.pk])}"' in content


@pytest.mark.django_db
def test_run_page_shows_an_error_only_when_there_is_one(admin_client):
    good, bad = _run(), _run(error="RuntimeError: HTTP 500")

    assert 'form-row field-error"' not in admin_client.get(
        reverse("admin:crawler_crawlrun_change", args=[good.pk])).content.decode()
    assert "RuntimeError: HTTP 500" in admin_client.get(
        reverse("admin:crawler_crawlrun_change", args=[bad.pk])).content.decode()


@pytest.mark.django_db
def test_finished_run_can_be_deleted_but_a_running_one_cannot(admin_client):
    finished = _run()
    running = _run(status=CrawlRun.Status.RUNNING, finished_at=None, heartbeat_at=timezone.now())

    assert admin_client.post(
        reverse("admin:crawler_crawlrun_delete", args=[running.pk]), {"post": "yes"},
    ).status_code == 403
    admin_client.post(reverse("admin:crawler_crawlrun_delete", args=[finished.pk]), {"post": "yes"})

    assert list(CrawlRun.objects.all()) == [running]


@pytest.mark.django_db
def test_bulk_delete_leaves_a_running_run(admin_client):
    finished = _run()
    running = _run(status=CrawlRun.Status.RUNNING, finished_at=None, heartbeat_at=timezone.now())

    admin_client.post(reverse(RUN_LIST), {
        "action": "delete_selected", "_selected_action": [finished.pk, running.pk], "post": "yes",
    })

    assert CrawlRun.objects.count() == 2


# --- LLM results -------------------------------------------------------------------------

@pytest.mark.django_db
def test_result_list_shows_the_article_and_outcome(admin_client):
    extraction = _extraction()
    _event(extraction)
    _event(extraction, disease="rabies")

    response = admin_client.get(reverse(RESULT_LIST))

    content = response.content.decode()
    assert response.context["title"] == "LLM results"
    assert "Article 1" in content
    assert "Daily News" in content
    assert ">Completed</span>" in content
    assert ">Disease event</span>" in content
    assert '<td class="field-event_count">2</td>' in content
    assert "A summary." in content
    assert "Tokens: 2,345 in, 120 out" in content


@pytest.mark.django_db
@pytest.mark.parametrize("value,label", [(True, "Disease event"), (False, "Not an event")])
def test_result_relevance_wording(admin_client, value, label):
    _extraction(is_disease_event=value)

    assert f">{label}</span>" in admin_client.get(reverse(RESULT_LIST)).content.decode()


@pytest.mark.django_db
def test_result_list_shows_why_a_result_failed_and_handles_no_tokens(admin_client):
    _extraction(status=ArticleExtraction.Status.FAILED, is_disease_event=None, summary="",
                error="Invalid response.", prompt_tokens=None, response_tokens=None)

    content = admin_client.get(reverse(RESULT_LIST)).content.decode()

    assert "Invalid response." in content
    assert "Tokens:" not in content


@pytest.mark.django_db
@pytest.mark.parametrize("column", ["article_title", "result", "disease_event", "event_count",
                                    "updated"])
def test_result_list_can_be_sorted(admin_client, column):
    _extraction()
    index = list(admin_client.get(reverse(RESULT_LIST)).context["cl"].list_display).index(column)

    assert admin_client.get(reverse(RESULT_LIST), {"o": str(index)}).status_code == 200


@pytest.mark.django_db
def test_result_list_does_not_add_a_query_per_row(admin_client, django_assert_max_num_queries):
    for n in range(20):
        _event(_extraction(n))

    with django_assert_max_num_queries(15):
        admin_client.get(reverse(RESULT_LIST))


@pytest.mark.django_db
def test_result_page_links_the_article_and_lists_events(admin_client):
    event = _event()
    extraction = event.extraction

    response = admin_client.get(reverse("admin:llm_articleextraction_change", args=[extraction.pk]))

    content = response.content.decode()
    assert response.context["title"] == "LLM result"
    assert response.context["subtitle"] == "Article 1"
    article_url = reverse("admin:raw_content_rawarticle_change", args=[extraction.article_id])
    assert f'<a href="{article_url}">Article 1</a>' in content
    assert f'href="{reverse("admin:llm_extractedevent_change", args=[event.pk])}"' in content
    assert "Gangni, Meherpur, Bangladesh" in content
    assert "Dec 1, 2023" in content


@pytest.mark.django_db
def test_result_page_only_has_sections_with_something_to_show(admin_client):
    plain = _extraction(1)
    full = _extraction(
        2, translated_title="Title", translated_body="Translated text.",
        reporter_author="A. Reporter", raw_response='{"raw": "<b>"}',
    )

    plain_html = admin_client.get(
        reverse("admin:llm_articleextraction_change", args=[plain.pk])).content.decode()
    full_html = admin_client.get(
        reverse("admin:llm_articleextraction_change", args=[full.pk])).content.decode()

    assert "Translation" not in plain_html
    assert "Stated in the text" not in plain_html
    assert "Translated text." in full_html
    assert "A. Reporter" in full_html
    assert "&lt;b&gt;" in full_html


@pytest.mark.django_db
def test_deleting_a_result_keeps_the_article(admin_client):
    event = _event()
    article_id = event.extraction.article_id

    admin_client.post(
        reverse("admin:llm_articleextraction_delete", args=[event.extraction_id]), {"post": "yes"},
    )

    assert not ArticleExtraction.objects.exists()
    assert not ExtractedEvent.objects.exists()
    assert RawArticle.objects.filter(pk=article_id).exists()


# --- Extracted events --------------------------------------------------------------------

@pytest.mark.django_db
def test_event_list_shows_place_counts_and_article(admin_client):
    event = _event()

    response = admin_client.get(reverse(EVENT_LIST))

    content = response.content.decode()
    assert response.context["title"] == "Extracted events"
    assert "Gangni, Meherpur, Bangladesh" in content
    assert "Dec 1, 2023" in content
    assert '<td class="field-confirmed">62</td>' in content
    assert '<td class="field-deaths">0</td>' in content  # stated as zero
    assert '<td class="field-suspected">–</td>' in content  # not stated
    article_url = reverse("admin:raw_content_rawarticle_change",
                          args=[event.extraction.article_id])
    assert f'href="{article_url}"' in content


@pytest.mark.django_db
def test_event_list_values_are_escaped(admin_client):
    _event(disease="<script>alert(1)</script>", village="<img src=x>")

    content = admin_client.get(reverse(EVENT_LIST)).content.decode()

    assert "<script>alert(1)</script>" not in content
    assert "<img src=x>" not in content


@pytest.mark.django_db
@pytest.mark.parametrize("column", ["disease", "event_day", "confirmed", "suspected", "deaths",
                                    "article"])
def test_event_list_can_be_sorted(admin_client, column):
    _event()
    index = list(admin_client.get(reverse(EVENT_LIST)).context["cl"].list_display).index(column)

    assert admin_client.get(reverse(EVENT_LIST), {"o": str(index)}).status_code == 200


@pytest.mark.django_db
def test_event_list_does_not_add_a_query_per_row(admin_client, django_assert_max_num_queries):
    for n in range(20):
        _event(_extraction(n))

    with django_assert_max_num_queries(15):
        admin_client.get(reverse(EVENT_LIST))


@pytest.mark.django_db
def test_event_list_can_be_searched_by_article_title(admin_client):
    event = _event()
    _event(_extraction(2))

    response = admin_client.get(reverse(EVENT_LIST), {"q": "Article 1"})

    assert list(response.context["cl"].result_list) == [event]


@pytest.mark.django_db
def test_event_page_shows_the_event_and_its_article(admin_client):
    event = _event(response_action="Vaccination campaign", responding_authority="IEDCR")

    response = admin_client.get(reverse("admin:llm_extractedevent_change", args=[event.pk]))

    content = response.content.decode()
    assert response.context["title"] == "Extracted event"
    assert response.context["subtitle"] == "anthrax in Gangni"
    assert "Vaccination campaign" in content
    assert "Article 1" in content
    assert "A summary." in content
    assert "&quot;disease_name&quot;: &quot;anthrax&quot;" in content


# --- Permissions and the shared layout ---------------------------------------------------

@pytest.mark.django_db
def test_view_only_users_can_open_every_page(client, django_user_model):
    run, event = _run(), _event()
    _viewer(client, django_user_model,
            "view_crawlrun", "view_articleextraction", "view_extractedevent")

    for url in (
        reverse(RUN_LIST), reverse(RESULT_LIST), reverse(EVENT_LIST),
        reverse("admin:crawler_crawlrun_change", args=[run.pk]),
        reverse("admin:llm_articleextraction_change", args=[event.extraction_id]),
        reverse("admin:llm_extractedevent_change", args=[event.pk]),
    ):
        assert client.get(url).status_code == 200, url


@pytest.mark.django_db
def test_view_only_user_cannot_delete(client, django_user_model):
    run = _run()
    _viewer(client, django_user_model, "view_crawlrun")

    url = reverse("admin:crawler_crawlrun_delete", args=[run.pk])

    assert client.post(url, {"post": "yes"}).status_code == 403
    assert CrawlRun.objects.filter(pk=run.pk).exists()


@pytest.mark.django_db
def test_record_pages_share_a_layout_that_other_pages_do_not_get(admin_client):
    _run()

    assert "record-page" in admin_client.get(reverse(RUN_LIST)).content.decode()
    assert "record-page" in admin_client.get(reverse(EVENT_LIST)).content.decode()
    assert "record-page" not in admin_client.get(
        reverse("admin:sources_source_changelist")).content.decode()
