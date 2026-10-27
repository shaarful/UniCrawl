"""Crawl run tracking, stop requests, and the crawl dashboard views."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from django.utils import timezone

from crawler import progress as P
from crawler.fetchers.eios import board_url
from crawler.models import CrawlRun
from crawler.runs import AlreadyRunning, create_run, execute_run, mark_stale_runs
from sources.models import Source

BOARD_URL = board_url("https://eios.example.org/portal/api", "1.0", "42")


def _source(**kwargs) -> Source:
    return Source.objects.create(
        name="EIOS", url=BOARD_URL, content_type=Source.ContentType.EIOS,
        keyword_filter_overrides=["ebola"], **kwargs,
    )


def _item(id_, title, description=""):
    return {
        "id": id_, "title": title, "description": description,
        "link": f"https://news.example.com/{id_}", "languageIso": "en",
        "source": {"id": "news", "type": "webnews"},
    }


def _fetcher(items):
    fetcher = MagicMock()
    fetcher.iter_board_items.return_value = iter(items)
    fetcher.get_item.return_value = None
    return fetcher


def _patched_pipeline(fetcher):
    return (
        patch("crawler.pipeline._fetcher_for", return_value=fetcher),
        patch("crawler.publisher.publish_matched"),
        patch("crawler.pipeline._fetch_original_body", return_value="Full Ebola text. " * 20),
    )


def _execute(run, fetcher):
    a, b, c = _patched_pipeline(fetcher)
    with a, b, c:
        execute_run(run.pk)
    run.refresh_from_db()
    return run


# ---------------------------------------------------------------------------
# RunProgress
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_progress_counts_outcomes_and_interpolates_segment():
    run = create_run(_source().pk)
    progress = P.RunProgress(run)
    progress.segment(20, 60, 4)
    for outcome in (P.STORED, P.PUBLISHED, P.SUPPRESSED, P.ERROR):
        progress.article(outcome)
    progress.flush(force=True)

    run.refresh_from_db()
    assert (run.processed, run.new, run.matched, run.published, run.duplicates, run.errors) == (
        4, 2, 1, 1, 1, 1)
    assert run.percent == 60


@pytest.mark.django_db
def test_progress_raises_when_stop_requested():
    run = create_run(_source().pk)
    CrawlRun.objects.filter(pk=run.pk).update(cancel_requested=True)
    with pytest.raises(P.CrawlCancelled):
        P.RunProgress(run).flush(force=True)


# ---------------------------------------------------------------------------
# execute_run
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_execute_run_records_counts_and_success():
    source = _source()
    run = create_run(source.pk)
    run = _execute(run, _fetcher([_item("a", "Ebola outbreak"), _item("b", "Football")]))

    assert run.status == CrawlRun.Status.SUCCEEDED
    assert run.finished_at is not None
    assert run.percent == 100
    assert (run.processed, run.new, run.matched, run.published) == (2, 2, 1, 1)
    assert run.fetched_at is not None
    assert any("EIOS" in e["msg"] for e in run.events)


@pytest.mark.django_db
def test_queue_failure_counts_as_matched_not_error():
    from raw_content.models import RawArticle

    run = create_run(_source().pk)
    fetcher = _fetcher([_item("a", "Ebola outbreak"), _item("b", "Ebola again")])
    with (
        patch("crawler.pipeline._fetcher_for", return_value=fetcher),
        patch("crawler.pipeline._fetch_original_body", side_effect=lambda u: f"Ebola {u} " * 20),
        patch("crawler.publisher.publish_matched", side_effect=ConnectionError("refused")),
    ):
        execute_run(run.pk)
    run.refresh_from_db()

    assert run.status == CrawlRun.Status.SUCCEEDED
    assert (run.matched, run.published, run.errors) == (2, 0, 0)
    warnings = [e for e in run.events if e["level"] == "warning"]
    assert len(warnings) == 1 and "LLM queue" in warnings[0]["msg"]
    assert set(RawArticle.objects.values_list("processing_status", flat=True)) == {"matched"}


@pytest.mark.django_db
def test_execute_run_reports_eios_windows_as_progress():
    source = _source()
    run = create_run(source.pk)
    fetcher = _fetcher([])

    def iter_items(url, *, since, until, on_window):
        on_window(since, since + (until - since) / 2, 3)
        return iter([])

    fetcher.iter_board_items.side_effect = iter_items
    run = _execute(run, fetcher)
    assert any("Reading EIOS" in e["msg"] and "(3 articles)" in e["msg"] for e in run.events)


@pytest.mark.django_db
def test_execute_run_records_failure_and_reraises():
    run = create_run(_source().pk)
    fetcher = _fetcher([])
    fetcher.iter_board_items.side_effect = RuntimeError("EIOS down")
    with pytest.raises(RuntimeError):
        _execute(run, fetcher)
    run.refresh_from_db()
    assert run.status == CrawlRun.Status.FAILED
    assert "EIOS down" in run.error


@pytest.mark.django_db
def test_stopped_run_is_cancelled_and_leaves_crawl_state_untouched():
    source = _source()
    run = create_run(source.pk)
    CrawlRun.objects.filter(pk=run.pk).update(cancel_requested=True)
    run = _execute(run, _fetcher([_item("a", "Ebola outbreak")]))

    assert run.status == CrawlRun.Status.CANCELLED
    source.refresh_from_db()
    assert source.last_crawled_at is None


@pytest.mark.django_db
def test_second_run_for_same_source_is_refused():
    source = _source()
    first = create_run(source.pk)
    with pytest.raises(AlreadyRunning) as exc:
        create_run(source.pk)
    assert exc.value.run == first


@pytest.mark.django_db
def test_stale_running_runs_are_marked_interrupted():
    source = _source()
    run = create_run(source.pk)
    CrawlRun.objects.filter(pk=run.pk).update(heartbeat_at=timezone.now() - timedelta(hours=1))
    assert mark_stale_runs() == 1
    run.refresh_from_db()
    assert run.status == CrawlRun.Status.INTERRUPTED
    create_run(source.pk)  # no longer blocks a new run


@pytest.mark.django_db
def test_scheduled_task_skips_source_already_running():
    from crawler.tasks import crawl_source

    source = _source()
    create_run(source.pk)
    with patch("crawler.pipeline.run_for_source") as pipeline:
        crawl_source(source.pk)
    pipeline.assert_not_called()


@pytest.mark.django_db
def test_scheduled_task_is_tracked_as_scheduled_run():
    from crawler.tasks import crawl_source

    source = _source()
    with patch("crawler.pipeline.run_for_source"):
        crawl_source(source.pk)
    run = CrawlRun.objects.get(source=source)
    assert run.trigger == CrawlRun.Trigger.SCHEDULED
    assert run.status == CrawlRun.Status.SUCCEEDED


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_dashboard_requires_staff(client):
    response = client.get(reverse("crawler:dashboard"))
    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]


@pytest.mark.django_db
def test_dashboard_and_status(admin_client):
    source = _source()
    assert admin_client.get(reverse("crawler:dashboard")).status_code == 200
    data = admin_client.get(reverse("crawler:dashboard-status")).json()
    assert data["sources"][0]["name"] == source.name
    assert data["sources"][0]["run"] is None


@pytest.mark.django_db
def test_start_creates_run_and_redirects_to_live_page(admin_client):
    source = _source()
    with patch("crawler.runs._launch") as launch:
        response = admin_client.post(reverse("crawler:start", args=[source.pk]))
    run = CrawlRun.objects.get(source=source)
    assert response.status_code == 302
    assert response["Location"] == reverse("crawler:run", args=[run.pk])
    launch.assert_called_once_with(run.pk)
    assert run.started_by.is_superuser


@pytest.mark.django_db
def test_start_while_running_redirects_to_existing_run(admin_client):
    source = _source()
    existing = create_run(source.pk)
    with patch("crawler.runs._launch") as launch:
        response = admin_client.post(reverse("crawler:start", args=[source.pk]))
    assert response["Location"] == reverse("crawler:run", args=[existing.pk])
    launch.assert_not_called()


@pytest.mark.django_db
def test_run_status_lists_articles_of_the_run(admin_client):
    source = _source()
    items = [_item("a", "Ebola outbreak"), _item("b", "Football")]
    run = _execute(create_run(source.pk), _fetcher(items))

    assert admin_client.get(reverse("crawler:run", args=[run.pk])).status_code == 200
    data = admin_client.get(reverse("crawler:run-status", args=[run.pk])).json()
    assert data["run"]["status"] == "succeeded"
    assert {a["title"] for a in data["latest"]} == {"Ebola outbreak", "Football"}
    assert [a["title"] for a in data["matched"]] == ["Ebola outbreak"]
    assert data["matched"][0]["body_source"] == "original_link"


@pytest.mark.django_db
def test_stop_requests_cancellation(admin_client):
    run = create_run(_source().pk)
    response = admin_client.post(
        reverse("crawler:run-stop", args=[run.pk]), HTTP_ACCEPT="application/json",
    )
    assert response.json() == {"ok": True}
    run.refresh_from_db()
    assert run.cancel_requested
