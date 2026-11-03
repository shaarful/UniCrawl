"""Listing, viewing and deleting articles in the admin."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone

from llm.models import ArticleExtraction, ExtractedEvent
from raw_content.models import RawArticle
from sources.models import Source

LIST_URL = "admin:raw_content_rawarticle_changelist"
Status = RawArticle.ProcessingStatus


def _article(n=1, **kwargs):
    source, _ = Source.objects.get_or_create(name="Feed", url="https://example.org/feed")
    defaults = {
        "source": source,
        "url": f"https://news.example.com/{n}", "title": f"Article {n}", "body": "Body text.",
        "http_status": 200, "fetched_at": timezone.now(),
        "url_fingerprint": str(n), "content_hash": str(n),
    }
    return RawArticle.objects.create(**{**defaults, **kwargs})


def _change_url(article):
    return reverse("admin:raw_content_rawarticle_change", args=[article.pk])


def _with_events(article):
    extraction = ArticleExtraction.objects.create(
        article=article, status=ArticleExtraction.Status.COMPLETED, summary="A summary.",
    )
    ExtractedEvent.objects.create(
        extraction=extraction, disease="anthrax", host="human", village="Gangni",
        district="Meherpur", country="Bangladesh", confirmed_case_count=62, fatality_count=0,
    )
    return extraction


# --- Adding ----------------------------------------------------------------------------

@pytest.mark.django_db
def test_articles_cannot_be_added_by_hand(admin_client):
    assert admin_client.get(reverse("admin:raw_content_rawarticle_add")).status_code == 403
    assert "Add raw article" not in admin_client.get(reverse(LIST_URL)).content.decode()
    home = admin_client.get(reverse("admin:index")).content.decode()
    assert reverse("admin:raw_content_rawarticle_add") not in home


# --- List ------------------------------------------------------------------------------

@pytest.mark.django_db
def test_list_shows_the_title_and_where_the_article_came_from(admin_client):
    _article(title="Anthrax in Meherpur", source_name="Daily News")

    response = admin_client.get(reverse(LIST_URL))

    content = response.content.decode()
    assert response.context["title"] == "Articles"
    assert "Anthrax in Meherpur" in content
    assert "Daily News · news.example.com" in content


@pytest.mark.django_db
def test_list_falls_back_to_the_url_and_source_name(admin_client):
    _article(title="")

    content = admin_client.get(reverse(LIST_URL)).content.decode()

    assert '<span class="article-title">https://news.example.com/1</span>' in content
    assert "Feed · news.example.com" in content


@pytest.mark.django_db
def test_list_title_is_escaped(admin_client):
    _article(title="<script>alert(1)</script>")

    assert "<script>alert(1)</script>" not in admin_client.get(reverse(LIST_URL)).content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("fields,label", [
    ({}, "No keyword match"),
    ({"is_keyword_matched": True}, "Pending"),
    ({"processing_status": Status.MATCHED, "is_keyword_matched": True}, "Keyword matched"),
    ({"processing_status": Status.PUBLISHED, "is_keyword_matched": True}, "Sent to LLM"),
    ({"processing_status": Status.SUPPRESSED, "is_duplicate": True}, "Duplicate"),
    ({"processing_status": Status.FAILED}, "Failed"),
])
def test_list_status_wording(admin_client, fields, label):
    _article(**fields)

    content = admin_client.get(reverse(LIST_URL)).content.decode()

    assert f">{label}</span></td>" in content


@pytest.mark.django_db
def test_list_shows_diseases_and_dates(admin_client):
    moment = datetime(2026, 10, 27, 12, tzinfo=UTC)
    _article(matched_diseases=["Anthrax", "Rabies"], published_at=moment, fetched_at=moment)

    content = admin_client.get(reverse(LIST_URL)).content.decode()

    assert "Anthrax, Rabies" in content
    assert content.count("Oct 27, 2026") == 2


@pytest.mark.django_db
def test_list_can_be_filtered_by_source_from_the_source_page_link(admin_client):
    article = _article()
    other = Source.objects.create(name="Other", url="https://example.org/other")
    _article(2, source=other)
    url = reverse(LIST_URL) + f"?source__id__exact={article.source_id}"

    assert list(admin_client.get(url).context["cl"].result_list) == [article]


@pytest.mark.django_db
@pytest.mark.parametrize("column", ["article", "status", "published", "llm_status"])
def test_list_can_be_sorted(admin_client, column):
    _article(1)
    _article(2)
    index = list(admin_client.get(reverse(LIST_URL)).context["cl"].list_display).index(column)

    response = admin_client.get(reverse(LIST_URL), {"o": str(index)})

    assert response.status_code == 200  # a column it cannot sort by would redirect to ?e=1


# --- Links from outside sources --------------------------------------------------------

@pytest.mark.django_db
def test_list_and_page_link_web_urls_in_a_new_tab(admin_client):
    article = _article(final_url="https://news.example.com/final")

    listing = admin_client.get(reverse(LIST_URL)).content.decode()
    page = admin_client.get(_change_url(article)).content.decode()

    for html in (listing, page):
        assert 'href="https://news.example.com/1" target="_blank" rel="noopener noreferrer"' in html
    assert 'href="https://news.example.com/final"' in page


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x"])
def test_other_url_schemes_are_never_links(admin_client, url):
    article = _article(url=url, final_url=url)

    listing = admin_client.get(reverse(LIST_URL)).content.decode()
    page = admin_client.get(_change_url(article)).content.decode()

    for html in (listing, page):
        assert f'href="{url}"' not in html
    assert "Open original" not in page
    assert url in page  # still visible as text


def test_run_page_only_links_web_urls():
    template = Path(__file__).resolve().parent.parent / "crawler/templates/crawler/run.html"

    assert "href: a.url" not in template.read_text(encoding="utf-8")


# --- Article page ----------------------------------------------------------------------

@pytest.mark.django_db
def test_page_is_headed_by_the_article_and_shows_its_text(admin_client):
    article = _article(title="Anthrax in Meherpur", body="Sixty-two cases were confirmed.")

    response = admin_client.get(_change_url(article))

    content = response.content.decode()
    assert response.context["title"] == "Article"
    assert response.context["subtitle"] == "Anthrax in Meherpur"
    assert "Change raw article" not in content
    assert "Sixty-two cases were confirmed." in content


@pytest.mark.django_db
def test_page_has_no_add_another_but_can_be_saved_and_deleted(admin_client):
    content = admin_client.get(_change_url(_article())).content.decode()

    assert 'name="_addanother"' not in content
    assert 'name="_save"' in content
    assert "deletelink" in content


@pytest.mark.django_db
def test_only_the_processing_status_can_be_edited(admin_client):
    article = _article(title="Original", body="Original body")

    response = admin_client.post(_change_url(article), {
        "title": "Changed", "body": "Changed", "url": "https://evil.example.com/",
        "processing_status": Status.FAILED,
        "extraction-TOTAL_FORMS": "0", "extraction-INITIAL_FORMS": "0",
    })

    assert response.status_code == 302
    article.refresh_from_db()
    assert article.processing_status == Status.FAILED
    assert article.title == "Original"
    assert article.body == "Original body"
    assert article.url == "https://news.example.com/1"


@pytest.mark.django_db
def test_page_shows_events_as_a_table_and_keeps_the_full_record(admin_client):
    article = _article()
    _with_events(article)

    content = admin_client.get(_change_url(article)).content.decode()

    assert "<th>Disease</th>" in content
    assert "<td>anthrax</td>" in content
    assert "<td>Gangni, Meherpur, Bangladesh</td>" in content
    assert '<td class="num">62</td>' in content
    assert "Full record (JSON)" in content
    assert "&quot;confirmed_case_count&quot;: 62" in content


@pytest.mark.django_db
def test_events_table_keeps_a_stated_zero_distinct_from_not_stated(admin_client):
    article = _article()
    _with_events(article)

    content = admin_client.get(_change_url(article)).content.decode()

    assert '<td class="num">0</td>' in content  # deaths: stated as zero
    assert '<td class="num">–</td>' in content  # suspected: not stated


@pytest.mark.django_db
def test_view_only_user_can_open_the_list_and_article(client, django_user_model):
    article = _article()
    _with_events(article)
    user = django_user_model.objects.create_user("viewer", password="x", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="view_rawarticle"))
    client.force_login(user)

    assert client.get(reverse(LIST_URL)).status_code == 200
    response = client.get(_change_url(article))

    assert response.status_code == 200
    assert 'name="_save"' not in response.content.decode()


@pytest.mark.django_db
def test_page_leaves_out_a_final_url_that_adds_nothing(admin_client):
    same = _article(1, final_url="https://news.example.com/1")
    empty = _article(2, final_url="")
    moved = _article(3, final_url="https://news.example.com/moved")

    assert "URL after redirects" not in admin_client.get(_change_url(same)).content.decode()
    assert "URL after redirects" not in admin_client.get(_change_url(empty)).content.decode()
    assert "URL after redirects" in admin_client.get(_change_url(moved)).content.decode()


@pytest.mark.django_db
def test_llm_section_leaves_out_empty_fields_and_shows_an_error(admin_client):
    plain = _article(1)
    _with_events(plain)
    failed = _article(2)
    ArticleExtraction.objects.create(
        article=failed, status=ArticleExtraction.Status.FAILED, error="Invalid response.",
    )
    translated = _article(3)
    ArticleExtraction.objects.create(
        article=translated, status=ArticleExtraction.Status.COMPLETED,
        translated_title="A title", translated_body="A translated body.",
    )

    plain_html = admin_client.get(_change_url(plain)).content.decode()
    failed_html = admin_client.get(_change_url(failed)).content.decode()
    translated_html = admin_client.get(_change_url(translated)).content.decode()

    assert "Translated body" not in plain_html
    assert "field-error" not in plain_html
    assert "Invalid response." in failed_html
    assert "A translated body." in translated_html
    assert "Processing details" in plain_html


@pytest.mark.django_db
def test_page_for_an_unprocessed_article_opens(admin_client):
    response = admin_client.get(_change_url(_article()))

    assert response.status_code == 200
    assert "LLM result" in response.content.decode()


# --- Deleting --------------------------------------------------------------------------

@pytest.mark.django_db
def test_deleting_an_article_removes_its_llm_result(admin_client):
    article = _article()
    _with_events(article)
    url = reverse("admin:raw_content_rawarticle_delete", args=[article.pk])

    page = admin_client.get(url).content.decode()
    response = admin_client.post(url, {"post": "yes"})

    assert "Article extraction" in page or "article extraction" in page
    assert response.status_code == 302
    assert not RawArticle.objects.exists()
    assert not ArticleExtraction.objects.exists()
    assert not ExtractedEvent.objects.exists()


@pytest.mark.django_db
def test_bulk_delete_is_still_available(admin_client):
    first, second = _article(1), _article(2)

    admin_client.post(reverse(LIST_URL), {
        "action": "delete_selected", "_selected_action": [first.pk, second.pk], "post": "yes",
    })

    assert not RawArticle.objects.exists()
