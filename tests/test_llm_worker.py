"""Worker tests: storing results, redelivery, retries, dead letters and outages."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import redis
from django.core.management import CommandError, call_command
from django.db import OperationalError
from django.utils import timezone

from llm.gemini import ArticleError, Event, Extraction, ExtractionResult, ServiceError
from llm.models import ArticleExtraction, ExtractedEvent
from llm.worker import ExtractionWorker
from raw_content.models import RawArticle
from sources.models import Source

STREAM = "test.stream"
DEAD = "test.stream.dead"
GROUP = "test-group"


@pytest.fixture(autouse=True)
def _settings(settings):
    settings.LLM_QUEUE_NAME = STREAM
    settings.LLM_DEAD_LETTER_QUEUE_NAME = DEAD
    settings.LLM_CONSUMER_GROUP = GROUP
    settings.LLM_MAX_ATTEMPTS = 3


@pytest.fixture
def article(db):
    source = Source.objects.create(
        name="Test", url="https://example.com/", content_type=Source.ContentType.HTML,
    )
    return RawArticle.objects.create(
        source=source,
        url="https://example.com/a",
        http_status=200,
        fetched_at=timezone.now(),
        body="Anthrax cases reported.",
        url_fingerprint="u",
        content_hash="c",
    )


def _client():
    client = MagicMock(spec=redis.Redis)
    client.xautoclaim.return_value = ["0-0", []]
    client.xreadgroup.return_value = []
    return client


def _worker(client=None, extract=None, once=True):
    extractor = MagicMock()
    extractor.model = "test-model"
    extractor.extract.side_effect = extract
    return ExtractionWorker(client or _client(), extractor, once=once)


def _result(*diseases, is_event=True):
    return ExtractionResult(
        extraction=Extraction(
            is_disease_event=is_event,
            events=[
                Event(disease=d, country="Bangladesh", district="Sirajganj",
                      confirmed_case_count=12)
                for d in diseases
            ],
            summary="  Summary of the article.  ",
        ),
        prompt_tokens=11,
        response_tokens=7,
        input_truncated=False,
        raw_response="{}",
    )


def _fields(article, title="Title", body="Body"):
    return {"article_id": str(article.pk), "title": title, "body": body}


# ---------------------------------------------------------------------------
# Storing results
# ---------------------------------------------------------------------------

def test_success_stores_events_then_acks_and_deletes(article):
    worker = _worker(extract=[_result("anthrax")])

    worker._handle("1-0", _fields(article))

    extraction = ArticleExtraction.objects.get(article=article)
    assert extraction.status == ArticleExtraction.Status.COMPLETED
    assert extraction.is_disease_event is True
    assert (extraction.prompt_tokens, extraction.response_tokens) == (11, 7)
    event = extraction.events.get()
    assert (event.disease, event.country, event.district, event.confirmed_case_count) == (
        "anthrax", "Bangladesh", "Sirajganj", 12,
    )
    pipe = worker.client.pipeline.return_value
    pipe.xack.assert_called_once_with(STREAM, GROUP, "1-0")
    pipe.xdel.assert_called_once_with(STREAM, "1-0")
    pipe.execute.assert_called_once()


def test_non_disease_article_is_stored_without_events(article):
    worker = _worker(extract=[_result(is_event=False)])

    worker._handle("1-0", _fields(article))

    extraction = ArticleExtraction.objects.get(article=article)
    assert extraction.is_disease_event is False
    assert not extraction.events.exists()
    assert worker.client.pipeline.return_value.xack.called


def test_message_is_not_acked_if_saving_fails(article):
    worker = _worker(extract=[_result("anthrax")])

    with (
        patch.object(ExtractedEvent.objects, "bulk_create", side_effect=OperationalError("locked")),
        pytest.raises(OperationalError),
    ):
        worker._handle("1-0", _fields(article))

    assert not worker.client.pipeline.called


def test_all_fr09_event_fields_are_stored(article):
    result = _result()
    result.extraction.events = [Event(
        disease="Nipah",
        host="humans",
        affected_population_size=214,
        country="India",
        division="Kerala",
        district="Kozhikode",
        village="Maruthonkara",
        latitude=11.6,
        longitude=75.8,
        location_type="community",
        event_date="2026-10-26",
        confirmed_case_count=3,
        suspected_case_count=18,
        fatality_count=1,
        response_action="Containment zones declared",
        responding_authority="Kerala Health Department",
    )]
    worker = _worker(extract=[result])

    worker._handle("1-0", _fields(article))

    event = ExtractedEvent.objects.get()
    assert event.host == "humans"
    assert event.affected_population_size == 214
    assert (event.division, event.district, event.village) == (
        "Kerala", "Kozhikode", "Maruthonkara",
    )
    assert (event.latitude, event.longitude) == (11.6, 75.8)
    assert event.location_type == "community"
    assert event.event_date.isoformat() == "2026-10-26"
    assert (event.confirmed_case_count, event.suspected_case_count, event.fatality_count) == (
        3, 18, 1,
    )
    assert event.response_action == "Containment zones declared"
    assert event.responding_authority == "Kerala Health Department"


def test_absent_fields_stay_empty(article):
    result = _result()
    result.extraction.events = [Event(disease="rabies")]
    worker = _worker(extract=[result])

    worker._handle("1-0", _fields(article))

    record = ExtractedEvent.objects.get().as_record()
    llm_fields = set(record) - {"disease_name", "source_url"}
    assert all(record[name] is None for name in llm_fields)
    assert record["disease_name"] == "rabies"


def test_publication_date_from_the_message_reaches_the_model(article):
    worker = _worker(extract=[_result("anthrax")])

    worker._handle("1-0", {**_fields(article), "published_at": "2026-10-27T08:00:00+00:00"})

    worker.extractor.extract.assert_called_once_with(
        "Title", "Body", "2026-10-27T08:00:00+00:00",
    )


def _record_with(article, **extraction_values):
    result = _result("anthrax")
    for name, value in extraction_values.items():
        setattr(result.extraction, name, value)
    worker = _worker(extract=[result])
    worker._handle("1-0", _fields(article))
    return ExtractedEvent.objects.get().as_record()


def test_record_prefers_crawler_values_over_llm_values(article):
    article.author = "Crawler Byline"
    article.published_at = timezone.now().replace(year=2026, month=5, day=19)
    article.source_name = "Daily Health"
    article.source_type = "html"
    article.language = "bn"
    article.final_url = "https://example.com/final"
    article.save()

    record = _record_with(article, reporter_author="LLM Name", publication_date="2020-01-01")

    assert record["reporter_author"] == "Crawler Byline"
    assert record["publication_date"] == "2026-05-19"
    assert record["source_name"] == "Daily Health"
    assert record["source_type"] == "html"
    assert record["source_language"] == "bn"
    assert record["source_url"] == "https://example.com/final"


def test_record_falls_back_to_llm_values_when_the_crawler_has_none(article):
    record = _record_with(
        article,
        reporter_author="LLM Name",
        author_affiliation="Reuters",
        publication_date="2026-05-19",
    )

    assert record["reporter_author"] == "LLM Name"
    assert record["author_affiliation"] == "Reuters"
    assert record["publication_date"] == "2026-05-19"
    assert record["source_url"] == article.url


def test_record_keeps_a_stated_zero_distinct_from_not_stated(article):
    result = _result()
    result.extraction.events = [Event(disease="rabies", fatality_count=0)]
    worker = _worker(extract=[result])

    worker._handle("1-0", _fields(article))

    record = ExtractedEvent.objects.get().as_record()
    assert record["fatality_count"] == 0
    assert record["suspected_case_count"] is None


def test_summary_is_stored(article):
    worker = _worker(extract=[_result("anthrax")])

    worker._handle("1-0", _fields(article))

    assert ArticleExtraction.objects.get(article=article).summary == "Summary of the article."


def test_translation_is_stored(article):
    result = _result("anthrax")
    result.extraction.translated_title = "English title"
    result.extraction.translated_body = "English body"
    worker = _worker(extract=[result])

    worker._handle("1-0", _fields(article))

    extraction = ArticleExtraction.objects.get(article=article)
    assert extraction.translated_title == "English title"
    assert extraction.translated_body == "English body"


def test_disease_names_are_stored_lowercase(article):
    worker = _worker(extract=[_result(" Rift Valley Fever ")])

    worker._handle("1-0", _fields(article))

    assert ExtractedEvent.objects.get().disease == "rift valley fever"


def test_overlong_values_are_cut_to_fit(article):
    result = _result("x" * 300)
    worker = _worker(extract=[result])

    worker._handle("1-0", _fields(article))

    assert len(ExtractedEvent.objects.get().disease) == 255


# ---------------------------------------------------------------------------
# Redelivery and re-sent articles
# ---------------------------------------------------------------------------

def test_redelivered_message_is_not_sent_to_gemini_again(article):
    worker = _worker(extract=[_result("anthrax")])

    worker._handle("1-0", _fields(article))
    worker._handle("1-0", _fields(article))

    assert worker.extractor.extract.call_count == 1
    assert worker.stats["skipped"] == 1
    assert worker.client.pipeline.return_value.xack.call_count == 2


def test_changed_text_replaces_the_previous_events(article):
    worker = _worker(extract=[_result("anthrax"), _result("rabies")])

    worker._handle("1-0", _fields(article, body="old"))
    worker._handle("2-0", _fields(article, body="new"))

    assert [e.disease for e in ExtractedEvent.objects.all()] == ["rabies"]
    assert ArticleExtraction.objects.get(article=article).stream_message_id == "2-0"


def test_new_prompt_version_extracts_again(article):
    worker = _worker(extract=[_result("anthrax"), _result("anthrax")])

    worker._handle("1-0", _fields(article))
    with patch("llm.services.PROMPT_VERSION", "next"):
        worker._handle("2-0", _fields(article))

    assert worker.extractor.extract.call_count == 2


def test_late_retry_of_an_old_message_cannot_overwrite_newer_result(article):
    worker = _worker(extract=[_result("rabies")])

    worker._handle("20-0", _fields(article, body="new"))
    worker._handle("3-0", _fields(article, body="old"))

    assert worker.extractor.extract.call_count == 1
    assert [e.disease for e in ExtractedEvent.objects.all()] == ["rabies"]
    assert worker.stats["skipped"] == 1


# ---------------------------------------------------------------------------
# Article-specific failures
# ---------------------------------------------------------------------------

def test_article_error_stays_pending_for_retry(article):
    worker = _worker(extract=[ArticleError("no valid output")])

    worker._handle("1-0", _fields(article))

    record = ArticleExtraction.objects.get(article=article)
    assert record.status == ArticleExtraction.Status.RETRYING
    assert record.attempts == 1
    assert record.error == "no valid output"
    assert not worker.client.pipeline.called
    assert not worker.client.xadd.called


def test_success_after_retry_clears_the_failure(article):
    worker = _worker(extract=[ArticleError("no valid output"), _result("anthrax")])

    worker._handle("1-0", _fields(article))
    worker._handle("1-0", _fields(article))

    record = ArticleExtraction.objects.get(article=article)
    assert record.status == ArticleExtraction.Status.COMPLETED
    assert (record.attempts, record.error) == (0, "")


def test_article_is_dead_lettered_after_max_attempts(article):
    worker = _worker(extract=[ArticleError("no valid output")] * 3)

    for _ in range(3):
        worker._handle("1-0", _fields(article))

    record = ArticleExtraction.objects.get(article=article)
    assert record.status == ArticleExtraction.Status.FAILED
    assert record.attempts == 3
    worker.client.xadd.assert_called_once()
    stream, fields = worker.client.xadd.call_args.args
    assert stream == DEAD
    assert fields["article_id"] == str(article.pk)
    assert fields["failed_message_id"] == "1-0"
    assert "no valid output" in fields["failure_reason"]
    worker.client.pipeline.return_value.xack.assert_called_once_with(STREAM, GROUP, "1-0")


def test_changed_text_gets_fresh_attempts(article):
    worker = _worker(extract=[ArticleError("bad")] * 3)

    worker._handle("1-0", _fields(article, body="old"))
    worker._handle("1-0", _fields(article, body="old"))
    worker._handle("2-0", _fields(article, body="new"))

    assert ArticleExtraction.objects.get(article=article).attempts == 1
    assert not worker.client.xadd.called


# ---------------------------------------------------------------------------
# Bad messages
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize(
    "fields",
    [{}, {"article_id": "abc", "body": "x"}],
    ids=["empty", "non-numeric"],
)
def test_malformed_message_is_dead_lettered(fields):
    worker = _worker()

    worker._handle("1-0", fields)

    assert worker.client.xadd.call_args.args[0] == DEAD
    assert worker.client.pipeline.return_value.xack.called
    assert not worker.extractor.extract.called


def test_message_without_text_is_dead_lettered(article):
    worker = _worker()

    worker._handle("1-0", _fields(article, title=" ", body=""))

    assert worker.client.xadd.call_args.args[0] == DEAD
    assert not worker.extractor.extract.called


@pytest.mark.django_db
def test_message_for_a_missing_article_is_dead_lettered():
    worker = _worker()

    worker._handle("1-0", {"article_id": "999", "title": "T", "body": "B"})

    assert "not found" in worker.client.xadd.call_args.args[1]["failure_reason"]
    assert not worker.extractor.extract.called


# ---------------------------------------------------------------------------
# Reading the stream
# ---------------------------------------------------------------------------

def test_group_creation_reads_from_the_start_of_the_stream():
    worker = _worker()

    worker._ensure_group()

    worker.client.xgroup_create.assert_called_once_with(STREAM, GROUP, id="0", mkstream=True)


def test_existing_group_is_fine():
    worker = _worker()
    worker.client.xgroup_create.side_effect = redis.ResponseError("BUSYGROUP already exists")

    worker._ensure_group()


def test_other_group_errors_are_raised():
    worker = _worker()
    worker.client.xgroup_create.side_effect = redis.ResponseError("WRONGTYPE")

    with pytest.raises(redis.ResponseError):
        worker._ensure_group()


def test_unfinished_messages_are_taken_before_new_ones():
    worker = _worker()
    worker.client.xautoclaim.return_value = ["0-0", [("1-0", {"article_id": "1"})]]

    assert worker._next_message() == ("1-0", {"article_id": "1"})
    assert not worker.client.xreadgroup.called


def test_new_messages_are_read_when_nothing_is_unfinished():
    worker = _worker()
    worker.client.xreadgroup.return_value = [[STREAM, [("2-0", {"article_id": "2"})]]]

    assert worker._next_message() == ("2-0", {"article_id": "2"})


def test_reclaim_wait_uses_the_configured_idle_time(settings):
    settings.LLM_CLAIM_IDLE_SECONDS = 300
    worker = _worker()

    worker._next_message()

    assert worker.client.xautoclaim.call_args.kwargs["min_idle_time"] == 300_000


# ---------------------------------------------------------------------------
# Run loop and outages
# ---------------------------------------------------------------------------

def _serve(client, *messages):
    """New messages one at a time, then nothing."""
    client.xreadgroup.side_effect = [[[STREAM, [m]]] for m in messages] + [[]]


def test_once_mode_exits_when_the_stream_is_empty(article):
    client = _client()
    _serve(client, ("1-0", _fields(article)))
    worker = _worker(client, extract=[_result("anthrax")])

    worker.run()

    assert worker.stats["completed"] == 1


def test_once_mode_reports_an_outage(article):
    client = _client()
    _serve(client, ("1-0", _fields(article)))
    worker = _worker(client, extract=[ServiceError("quota")])

    with pytest.raises(ServiceError):
        worker.run()


def test_service_outage_backs_off_without_touching_the_message(article):
    client = _client()
    worker = _worker(client, extract=[ServiceError("quota")], once=False)
    client.xreadgroup.side_effect = [[[STREAM, [("1-0", _fields(article))]]]]

    def stop_during_backoff(_seconds):
        worker.stop()

    with patch("llm.worker.time.sleep", side_effect=stop_during_backoff) as sleep:
        worker.run()

    assert sleep.called
    assert not client.pipeline.called
    assert not client.xadd.called
    assert not ArticleExtraction.objects.filter(article=article).exists()


@pytest.mark.parametrize(
    "error",
    [redis.ConnectionError("refused"), OperationalError("database is locked")],
    ids=["redis", "database"],
)
@pytest.mark.django_db
def test_redis_and_database_outages_also_back_off(error):
    client = _client()
    worker = _worker(client, once=False)
    client.xautoclaim.side_effect = error

    with patch("llm.worker.time.sleep", side_effect=lambda _s: worker.stop()) as sleep:
        worker.run()

    assert sleep.called


@pytest.mark.django_db
def test_group_is_recreated_after_an_outage():
    """A Redis restart without persistence loses the group (NOGROUP on read)."""
    client = _client()
    worker = _worker(client, once=False)
    client.xautoclaim.side_effect = [redis.ResponseError("NOGROUP no such group"), ["0-0", []]]
    client.xreadgroup.side_effect = lambda *a, **k: worker.stop() or []

    with patch("llm.worker.time.sleep"):
        worker.run()

    assert client.xgroup_create.call_count == 2


def test_backoff_doubles_up_to_a_limit(article):
    client = _client()
    worker = _worker(client, once=False)
    client.xautoclaim.side_effect = redis.ConnectionError("refused")
    delays = []

    def record(seconds):
        delays.append(seconds)
        if len(delays) == 8:
            worker.stop()

    with patch.object(worker, "_sleep", side_effect=record):
        worker.run()

    assert delays == [5, 10, 20, 40, 80, 160, 300, 300]


def test_stop_interrupts_a_long_backoff():
    worker = _worker()
    worker.stop()

    with patch("llm.worker.time.sleep") as sleep:
        worker._sleep(300)

    assert not sleep.called


# ---------------------------------------------------------------------------
# Management command
# ---------------------------------------------------------------------------

def test_command_fails_clearly_without_configuration(settings):
    settings.RZDO_GCP_PROJECT = ""
    settings.RZDO_GCP_CREDENTIALS = ""

    with pytest.raises(CommandError, match="RZDO_GCP_PROJECT"):
        call_command("run_llm_worker", "--once")


def test_command_fails_clearly_when_the_key_file_is_missing(settings, tmp_path):
    settings.RZDO_GCP_PROJECT = "project"
    settings.RZDO_GCP_CREDENTIALS = str(tmp_path / "missing.json")

    with pytest.raises(CommandError, match="Cannot start"):
        call_command("run_llm_worker", "--once")
