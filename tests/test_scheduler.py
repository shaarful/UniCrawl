"""Scheduler tests — record_failure back-off logic and schedule_due_sources filtering."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from crawler.scheduler import record_failure, schedule_due_sources
from sources.models import Source


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _source(*, is_active=True, next_crawl_at=None, is_temporarily_unavailable=False):
    return Source.objects.create(
        name="Test",
        url="https://example.com/feed",
        content_type=Source.ContentType.HTML,
        is_active=is_active,
        next_crawl_at=next_crawl_at,
        is_temporarily_unavailable=is_temporarily_unavailable,
    )


# ---------------------------------------------------------------------------
# record_failure — back-off schedule
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_record_failure_increments_consecutive_failures():
    source = _source()
    record_failure(source.id)
    source.refresh_from_db()
    assert source.consecutive_failures == 1


@pytest.mark.django_db(transaction=True)
def test_first_failure_sets_next_crawl_at_to_one_minute():
    source = _source()
    before = timezone.now()
    record_failure(source.id)
    source.refresh_from_db()
    after = timezone.now()

    assert source.next_crawl_at is not None
    assert before + timedelta(seconds=59) <= source.next_crawl_at <= after + timedelta(minutes=1, seconds=1)
    assert not source.is_temporarily_unavailable


@pytest.mark.django_db(transaction=True)
def test_second_failure_sets_next_crawl_at_to_four_minutes():
    source = _source()
    record_failure(source.id)  # failure 1
    before = timezone.now()
    record_failure(source.id)  # failure 2
    source.refresh_from_db()
    after = timezone.now()

    assert before + timedelta(minutes=3, seconds=59) <= source.next_crawl_at <= after + timedelta(minutes=4, seconds=1)
    assert not source.is_temporarily_unavailable


@pytest.mark.django_db(transaction=True)
def test_third_failure_sets_next_crawl_at_to_sixteen_minutes():
    source = _source()
    record_failure(source.id)  # failure 1
    record_failure(source.id)  # failure 2
    before = timezone.now()
    record_failure(source.id)  # failure 3
    source.refresh_from_db()
    after = timezone.now()

    assert before + timedelta(minutes=15, seconds=59) <= source.next_crawl_at <= after + timedelta(minutes=16, seconds=1)
    assert not source.is_temporarily_unavailable


@pytest.mark.django_db(transaction=True)
def test_fourth_failure_marks_source_temporarily_unavailable():
    source = _source()
    for _ in range(4):
        record_failure(source.id)
    source.refresh_from_db()

    assert source.is_temporarily_unavailable
    assert source.consecutive_failures == 4


@pytest.mark.django_db(transaction=True)
def test_unavailable_source_stays_unavailable_on_further_failures():
    source = _source()
    for _ in range(5):
        record_failure(source.id)
    source.refresh_from_db()

    assert source.is_temporarily_unavailable
    assert source.consecutive_failures == 5


# ---------------------------------------------------------------------------
# schedule_due_sources — filtering
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_schedule_due_sources_enqueues_source_with_null_next_crawl_at():
    source = _source()  # next_crawl_at=None → never crawled, due immediately
    with patch("crawler.tasks.crawl_source") as mock_task:
        count = schedule_due_sources()

    assert count == 1
    mock_task.delay.assert_called_once_with(source.id)


@pytest.mark.django_db
def test_schedule_due_sources_enqueues_overdue_source():
    past = timezone.now() - timedelta(hours=2)
    source = _source(next_crawl_at=past)
    with patch("crawler.tasks.crawl_source") as mock_task:
        count = schedule_due_sources()

    assert count == 1
    mock_task.delay.assert_called_once_with(source.id)


@pytest.mark.django_db
def test_schedule_due_sources_skips_source_not_yet_due():
    future = timezone.now() + timedelta(hours=1)
    _source(next_crawl_at=future)
    with patch("crawler.tasks.crawl_source") as mock_task:
        count = schedule_due_sources()

    assert count == 0
    mock_task.delay.assert_not_called()


@pytest.mark.django_db
def test_schedule_due_sources_skips_inactive_source():
    _source(is_active=False)
    with patch("crawler.tasks.crawl_source") as mock_task:
        count = schedule_due_sources()

    assert count == 0
    mock_task.delay.assert_not_called()


@pytest.mark.django_db
def test_schedule_due_sources_skips_temporarily_unavailable_source():
    _source(is_temporarily_unavailable=True)
    with patch("crawler.tasks.crawl_source") as mock_task:
        count = schedule_due_sources()

    assert count == 0
    mock_task.delay.assert_not_called()


@pytest.mark.django_db
def test_schedule_due_sources_enqueues_multiple_due_sources():
    source_a = _source()
    source_b = Source.objects.create(
        name="Second",
        url="https://other.example.com/",
        content_type=Source.ContentType.RSS,
        is_active=True,
    )
    with patch("crawler.tasks.crawl_source") as mock_task:
        count = schedule_due_sources()

    assert count == 2
    called_ids = {call.args[0] for call in mock_task.delay.call_args_list}
    assert called_ids == {source_a.id, source_b.id}


# ---------------------------------------------------------------------------
# crawl_source Celery task
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_crawl_source_task_calls_record_failure_on_pipeline_error():
    from crawler.tasks import crawl_source

    source = _source()
    with (
        patch("crawler.pipeline.run_for_source", side_effect=RuntimeError("network error")),
        pytest.raises(RuntimeError),
    ):
        crawl_source(source.id)

    source.refresh_from_db()
    assert source.consecutive_failures == 1


@pytest.mark.django_db
def test_crawl_source_task_does_not_call_record_failure_on_success():
    from crawler.tasks import crawl_source

    source = _source()
    with patch("crawler.pipeline.run_for_source"):
        crawl_source(source.id)

    source.refresh_from_db()
    assert source.consecutive_failures == 0
