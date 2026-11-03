"""Deleting a source's articles and crawling it again."""
from unittest.mock import patch

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone

from crawler.models import CrawlRun
from llm.models import ArticleExtraction, ExtractedEvent
from raw_content.models import RawArticle
from sources.models import Source


@pytest.fixture(autouse=True)
def launch():
    with patch("crawler.runs._launch") as mocked:
        yield mocked


def _source(name="Feed", **kwargs):
    return Source.objects.create(name=name, url=f"https://example.org/{name}", **kwargs)


def _article(source, slug="a", **kwargs):
    return RawArticle.objects.create(
        source=source, url=f"https://example.org/{source.pk}/{slug}", http_status=200,
        fetched_at=timezone.now(), title="T", body=slug,
        url_fingerprint=f"{source.pk}{slug}", content_hash=f"{source.pk}{slug}", **kwargs,
    )


def _with_results(article):
    extraction = ArticleExtraction.objects.create(
        article=article, status=ArticleExtraction.Status.COMPLETED,
    )
    ExtractedEvent.objects.create(extraction=extraction, disease="anthrax")
    return extraction


def _url(source):
    return reverse("crawler:source-recrawl", args=[source.pk])


def _staff(django_user_model, *codenames):
    user = django_user_model.objects.create_user("clerk", password="x", is_staff=True)
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
    return user


@pytest.mark.django_db
def test_confirmation_page_lists_what_goes_and_deletes_nothing(admin_client, launch):
    source = _source()
    _with_results(_article(source, "a"))
    _article(source, "b")

    response = admin_client.get(_url(source))

    content = response.content.decode()
    assert response.status_code == 200
    assert "<strong>2</strong> articles" in content
    assert "<strong>1</strong> article extraction<" in content
    assert "<strong>1</strong> extracted event<" in content
    assert "Delete 2 articles and re-crawl" in content
    assert RawArticle.objects.count() == 2
    assert not CrawlRun.objects.exists()
    launch.assert_not_called()


@pytest.mark.django_db
def test_post_deletes_this_source_only_and_starts_a_crawl(admin_client, admin_user, launch):
    source = _source()
    other = _source("Other")
    _with_results(_article(source, "a"))
    _article(source, "b")
    kept = _article(other)
    _with_results(kept)

    response = admin_client.post(_url(source))

    run = CrawlRun.objects.get()
    assert run.source == source
    assert run.started_by == admin_user
    assert response.status_code == 302
    assert response["Location"] == reverse("crawler:run", args=[run.pk])
    launch.assert_called_once_with(run.pk)
    assert list(RawArticle.objects.all()) == [kept]
    assert ArticleExtraction.objects.get().article == kept
    assert ExtractedEvent.objects.count() == 1


@pytest.mark.django_db
def test_post_reports_the_count_and_keeps_an_audit_entry(admin_client):
    source = _source()
    _article(source)

    response = admin_client.post(_url(source), follow=True)

    assert "Deleted 1 article from Feed." in response.content.decode()
    entry = LogEntry.objects.get(object_id=str(source.pk))
    assert "Articles deleted: 1." in entry.get_change_message()


@pytest.mark.django_db
def test_post_forgets_the_last_crawl_so_eios_reads_its_backfill_again(admin_client):
    source = _source(content_type=Source.ContentType.EIOS, last_crawled_at=timezone.now())

    admin_client.post(_url(source))

    source.refresh_from_db()
    assert source.last_crawled_at is None


@pytest.mark.django_db
def test_nothing_is_deleted_while_the_source_is_being_crawled(admin_client, launch):
    source = _source()
    _article(source)
    running = CrawlRun.objects.create(
        source=source, status=CrawlRun.Status.RUNNING, heartbeat_at=timezone.now(),
    )

    page = admin_client.get(_url(source)).content.decode()
    response = admin_client.post(_url(source))

    assert "being crawled now" in page
    assert 'type="submit"' not in page
    assert response["Location"] == reverse("crawler:run", args=[running.pk])
    assert RawArticle.objects.count() == 1
    assert CrawlRun.objects.count() == 1
    launch.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_a_failed_delete_changes_nothing_and_starts_no_crawl(admin_client, launch):
    source = _source(last_crawled_at=timezone.now())
    _article(source)
    admin_client.raise_request_exception = False

    with patch("django.contrib.admin.models.LogEntry.objects.log_actions", side_effect=OSError):
        response = admin_client.post(_url(source))

    assert response.status_code == 500
    source.refresh_from_db()
    assert source.last_crawled_at is not None
    assert RawArticle.objects.count() == 1
    assert not CrawlRun.objects.exists()
    launch.assert_not_called()


@pytest.mark.django_db
def test_other_sources_duplicates_lose_their_link_and_the_page_warns(admin_client):
    source = _source()
    original = _article(source)
    copy = _article(
        _source("Other"), is_duplicate=True, duplicate_of=original,
        processing_status=RawArticle.ProcessingStatus.SUPPRESSED,
    )

    content = admin_client.get(_url(source)).content.decode()
    admin_client.post(_url(source))

    assert "1 duplicate article from other sources points to these" in content
    copy.refresh_from_db()
    assert copy.duplicate_of is None
    assert copy.is_duplicate is True


@pytest.mark.django_db
@pytest.mark.parametrize("fields,phrase", [
    ({"content_type": Source.ContentType.EIOS}, "last 24 hours"),
    ({"content_type": Source.ContentType.RSS}, "what the feed lists today"),
    ({"crawl_depth": 1}, "this one page"),
    ({"crawl_depth": 2}, "up to 50 pages"),
])
def test_page_says_what_cannot_come_back(admin_client, settings, fields, phrase):
    settings.EIOS_BACKFILL_HOURS = 24
    settings.CRAWLER_MAX_PAGES_PER_RUN = 50
    source = _source(**fields)
    _article(source)

    content = admin_client.get(_url(source)).content.decode()

    assert phrase in content
    assert "the articles stay deleted" in content


@pytest.mark.django_db
def test_source_without_articles_only_offers_a_crawl(admin_client):
    content = admin_client.get(_url(_source())).content.decode()

    assert "no articles to delete" in content
    assert "Start crawl" in content


@pytest.mark.django_db
def test_anonymous_user_is_sent_to_login_and_nothing_is_deleted(client, launch):
    source = _source()
    _article(source)

    response = client.post(_url(source))

    assert "/admin/login/" in response["Location"]
    assert RawArticle.objects.count() == 1
    launch.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "post"])
def test_staff_without_delete_permission_are_refused(client, django_user_model, launch, method):
    source = _source()
    _article(source)
    client.force_login(_staff(django_user_model, "view_source", "view_rawarticle"))

    response = getattr(client, method)(_url(source))

    assert response.status_code == 403
    assert RawArticle.objects.count() == 1
    launch.assert_not_called()


@pytest.mark.django_db
def test_get_never_needs_a_post_token_but_post_does(django_user_model, launch):
    from django.test import Client

    source = _source()
    _article(source)
    client = Client(enforce_csrf_checks=True)
    client.force_login(django_user_model.objects.create_superuser("root", password="x"))

    assert client.post(_url(source)).status_code == 403
    assert RawArticle.objects.count() == 1
    launch.assert_not_called()


@pytest.mark.django_db
def test_buttons_appear_for_users_who_can_delete(admin_client):
    source = _source()
    url = _url(source)

    listing = admin_client.get(reverse("admin:sources_source_changelist")).content.decode()
    change = admin_client.get(
        reverse("admin:sources_source_change", args=[source.pk]),
    ).content.decode()
    status = admin_client.get(reverse("crawler:dashboard-status")).json()

    assert f'href="{url}"' in listing
    assert f'href="{url}"' in change
    assert status["sources"][0]["recrawl_url"] == url


@pytest.mark.django_db
def test_buttons_are_hidden_from_users_who_cannot_delete(client, django_user_model):
    source = _source()
    url = _url(source)
    client.force_login(_staff(django_user_model, "view_source", "change_source"))

    listing = client.get(reverse("admin:sources_source_changelist")).content.decode()
    change = client.get(reverse("admin:sources_source_change", args=[source.pk])).content.decode()
    status = client.get(reverse("crawler:dashboard-status")).json()

    assert url not in listing
    assert "Run now" in listing
    assert url not in change
    assert status["sources"][0]["recrawl_url"] is None
