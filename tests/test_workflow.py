"""The "what to do next" guide on the crawl dashboard and admin home page."""
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from crawler.models import CrawlRun
from disease_keywords.models import DiseaseKeyword
from llm.models import ArticleExtraction, ExtractedEvent
from raw_content.models import RawArticle
from sources.models import Source
from unicrawl.templatetags.workflow import build_steps


def _source(**kwargs):
    return Source.objects.create(name="Feed", url="https://example.com/feed", **kwargs)


def _article(source, url="https://example.com/a", **kwargs):
    return RawArticle.objects.create(
        source=source, url=url, http_status=200, fetched_at=timezone.now(),
        title="Title", body=url, url_fingerprint=url, content_hash=url, **kwargs,
    )


def _next_title():
    steps = build_steps()
    upcoming = [step["title"] for step in steps if step["next"]]
    assert len(upcoming) <= 1
    return upcoming[0] if upcoming else None


def _step(title):
    return next(step for step in build_steps() if step["title"] == title)


@pytest.mark.django_db
def test_next_step_moves_along_the_flow():
    assert _next_title() == "Disease keywords"

    DiseaseKeyword.objects.create(disease_name="Anthrax", keyword="anthrax")
    assert _next_title() == "Sources"

    source = _source()
    assert _next_title() == "Run a crawl"

    CrawlRun.objects.create(source=source)
    assert _next_title() == "Raw articles"

    article = _article(source, is_keyword_matched=True)
    assert _next_title() == "LLM processing"

    extraction = ArticleExtraction.objects.create(
        article=article, status=ArticleExtraction.Status.COMPLETED,
    )
    assert _next_title() == "Results"

    ExtractedEvent.objects.create(extraction=extraction, disease="Anthrax")
    assert _next_title() is None


@pytest.mark.django_db
def test_keywords_come_first_even_when_later_steps_have_data():
    _article(_source())

    assert _next_title() == "Disease keywords"
    assert _step("Raw articles")["done"]


@pytest.mark.django_db
def test_source_keyword_list_counts_as_having_keywords():
    _source(keyword_filter_overrides=["anthrax"])

    assert _step("Disease keywords")["done"]


@pytest.mark.django_db
def test_empty_source_keyword_list_does_not_count_as_having_keywords():
    _source(keyword_filter_overrides=[])

    assert not _step("Disease keywords")["done"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status", [ArticleExtraction.Status.RETRYING, ArticleExtraction.Status.FAILED],
)
def test_retrying_and_failed_extractions_are_not_processed(status):
    source = _source()
    article = _article(source, is_keyword_matched=True)
    ArticleExtraction.objects.create(article=article, status=status)

    step = _step("LLM processing")

    assert not step["done"]
    assert step["status"] == "1 of 1 matched not processed"


@pytest.mark.django_db
def test_llm_step_counts_only_matched_articles_that_are_not_duplicates():
    source = _source()
    original = _article(source, url="https://example.com/1", is_keyword_matched=True)
    _article(source, url="https://example.com/2", is_keyword_matched=True)
    _article(source, url="https://example.com/3")
    _article(
        source, url="https://example.com/4",
        is_keyword_matched=True, is_duplicate=True, duplicate_of=original,
    )

    assert _step("LLM processing")["status"] == "2 of 2 matched not processed"


@pytest.mark.django_db
def test_llm_step_without_matches_says_so_and_stays_next():
    DiseaseKeyword.objects.create(disease_name="Anthrax", keyword="anthrax")
    source = _source()
    CrawlRun.objects.create(source=source)
    _article(source)

    assert _next_title() == "LLM processing"
    assert _step("LLM processing")["status"] == "Nothing matched yet"


@pytest.mark.django_db
@pytest.mark.parametrize("url_name", ["crawler:dashboard", "admin:index"])
def test_guide_appears_on_both_pages(admin_client, url_name):
    content = admin_client.get(reverse(url_name)).content.decode()

    assert "What to do next" in content
    assert "Next step:" in content
    assert "LLM processing" in content


@pytest.mark.django_db
def test_every_step_link_opens(admin_client):
    for step in build_steps():
        response = admin_client.get(step["url"])

        # An admin filter it rejects would redirect to ?e=1, so a plain 200 proves it is valid.
        assert response.status_code == 200, step["url"]


def test_crawler_python_code_does_not_import_llm():
    root = Path(__file__).resolve().parent.parent / "crawler"
    offenders = [
        str(path)
        for path in root.rglob("*.py")
        if "import llm" in path.read_text(encoding="utf-8")
        or "from llm" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
